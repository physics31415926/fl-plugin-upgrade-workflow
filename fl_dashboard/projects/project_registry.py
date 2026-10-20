"""Project-scoped dashboard contexts, with isolated state and failure reporting."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from http.client import HTTPConnection, HTTPException
import json
from pathlib import Path
import re
import threading
from urllib.parse import parse_qs, urlencode, urlsplit
from fl_dashboard.monitoring.registry import create_monitor, monitor_type


class ProjectError(ValueError):
    pass


@dataclass
class ProjectContext:
    id: str
    label: str
    engine_name: str = "vllm"
    default_branch: str = ""
    github_repo: str = ""
    monitor: object | None = None
    sessions: object | None = None
    workflow: object | None = None
    monitor_error: str | None = None
    sessions_error: str | None = None
    workflow_error: str | None = None
    native_tasks: object | None = None
    native_tasks_error: str | None = None
    agent_settings: object | None = None
    readonly_sessions: object | None = None
    flagscale_settings: object | None = None

    def metadata(self, *, read_only=False):
        return {"id": self.id, "label": self.label, "engine": self.engine_name,
                "default_branch": self.default_branch, "github_repo": self.github_repo,
                "sessions": not read_only and self.sessions is not None,
                "workflow": not read_only and self.workflow is not None,
                "agent_settings": not read_only and self.agent_settings is not None,
                "flagscale_settings": not read_only and self.flagscale_settings is not None,
                "adaptation_tasks": self.native_tasks is not None or self.readonly_sessions is not None
                                    or (not read_only and self.sessions is not None)}

    def adaptation_tasks(self, *, read_only=False, session_rows=None):
        native = self.native_tasks.snapshot(read_only=read_only) if self.native_tasks is not None else []
        if read_only:
            persisted = self.readonly_sessions.snapshot() if self.readonly_sessions is not None else []
            replacements = {(task.get("backend"), task.get("branch")) for task in native}
            for task in persisted:
                task["historical"] = (task.get("provider") == "codex"
                                      and (task.get("backend"), task.get("branch")) in replacements)
            return native + [task for task in persisted if not task["historical"]] + [task for task in persisted if task["historical"]]
        if session_rows is None:
            session_rows = self.sessions.snapshot(compact=True).get("sessions", []) if self.sessions is not None else []
        replacements = {(task.get("backend"), task.get("branch")) for task in native}
        dashboard = [{**session, "source": "dashboard", "source_label": "本机任务服务", "controllable": True,
                      "historical": session.get("provider", "codex") == "codex" and
                                    (session.get("backend"), session.get("branch")) in replacements}
                     for session in session_rows]
        current = [task for task in dashboard if not task["historical"]]
        historical = [task for task in dashboard if task["historical"]]
        return native + current + historical

    def status(self):
        result = {"id": self.id, "label": self.label, "engine": self.engine_name,
                  "status": None, "error": self.monitor_error, "sessions": []}
        if self.monitor is not None:
            try:
                result["status"] = self.monitor.status()
            except Exception as exc:
                result["error"] = str(exc)
        if self.sessions is not None:
            try:
                result["sessions"] = self.sessions.snapshot(compact=True).get("sessions", [])
            except Exception as exc:
                result["error"] = "; ".join(filter(None, (result["error"], f"Sessions unavailable: {exc}")))
        elif self.sessions_error:
            result["error"] = "; ".join(filter(None, (result["error"], self.sessions_error)))
        result["adaptation_tasks"] = self.adaptation_tasks(session_rows=result["sessions"])
        if self.native_tasks_error:
            result["error"] = "; ".join(filter(None, (result["error"], self.native_tasks_error)))
        return result


class RemoteSessionManager:
    """Forward sessions to an existing local owner without touching its files."""
    ACTIVE = {"starting", "running", "waiting_approval", "waiting_input", "interrupting"}

    def __init__(self, config, base_dir):
        self.config = config
        self.base_dir = Path(base_dir).resolve()
        self.root = Path(config["sessions_dir"])
        self.evidence_dir = Path(config["evidence_dir"])
        self.machine_lock = threading.RLock()
        self.peer_managers = [self]
        self.default_branch = config.get("branch", "")
        value = config.get("sessions_api_url")
        if not isinstance(value, str) or len(value) > 200 or any(ord(char) <= 32 for char in value):
            raise ProjectError("sessions_api_url must be a loopback HTTP base URL")
        try:
            parsed = urlsplit(value)
            if (parsed.scheme != "http" or parsed.hostname not in ("127.0.0.1", "localhost", "::1")
                    or parsed.port is None or not 1 <= parsed.port <= 65535
                    or parsed.username is not None or parsed.password is not None
                    or parsed.path not in ("", "/") or parsed.query or parsed.fragment):
                raise ValueError()
            self._host, self._port = parsed.hostname, parsed.port
        except ValueError:
            raise ProjectError("sessions_api_url must be a loopback HTTP base URL") from None

    @staticmethod
    def _fail(message):
        from fl_dashboard.agents.codex_sessions import SessionError
        raise SessionError(message)

    @staticmethod
    def _session_id(value):
        if not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{32}", value):
            RemoteSessionManager._fail("Invalid session ID")
        return value

    def _request(self, path, body=None):
        # HTTPConnection does not follow redirects or read proxy credentials.
        connection = HTTPConnection(self._host, self._port, timeout=30)
        try:
            headers = {"Content-Type": "application/json"} if body is not None else {}
            encoded = json.dumps(body, ensure_ascii=False).encode("utf-8") if body is not None else None
            connection.request("POST" if body is not None else "GET", path, encoded, headers)
            response = connection.getresponse()
            data = response.read(16 * 1024 * 1024 + 1)
            if len(data) > 16 * 1024 * 1024:
                self._fail("Existing local session response is too large")
            try:
                value = json.loads(data)
            except (ValueError, UnicodeError):
                self._fail("Existing local session response is invalid")
            if not isinstance(value, dict):
                self._fail("Existing local session response must be an object")
            if response.status < 200 or response.status >= 300:
                message = value.get("error")
                self._fail(message if isinstance(message, str) else f"Existing local session API returned HTTP {response.status}")
            return value
        except (OSError, HTTPException) as exc:
            self._fail(f"Existing local session service unavailable: {exc}")
        finally:
            connection.close()

    def _policy(self, session):
        policy = self.config.get("backend_task_policies", {}).get(session.get("backend"))
        if isinstance(policy, dict) and policy.get("branch", self.default_branch) == session.get("branch", self.default_branch):
            return policy
        return {}

    def _require_adapt(self, session):
        if self._policy(session).get("mode") == "monitor":
            self._fail("This backend is monitor-only; active adaptation is disabled.")

    def snapshot(self, compact=False):
        value = self._request("/api/codex-sessions" + ("?view=cards" if compact else ""))
        value.setdefault("engine", self.config.get("engine", "vllm"))
        value.setdefault("project_label", self.config.get("project_label", "vLLM-Plugin-FL"))
        for session in value.get("sessions", []):
            policy = self._policy(session)
            if policy:
                session["task_policy"] = {**session.get("task_policy", {}), **policy}
        return value

    def allocation_sessions(self):
        return self.snapshot(compact=True).get("sessions", [])

    def _get(self, session_id, *, include_deleted=False):
        session_id = self._session_id(session_id)
        snapshot = self.snapshot(compact=True)
        rows = snapshot.get("sessions", [])
        if include_deleted:
            rows = rows + snapshot.get("deleted_sessions", [])
        for session in rows:
            if session.get("id") == session_id:
                return session
        self._fail("Unknown session")

    def _require_free_machine(self, machine, excluding=None):
        from fl_dashboard.agents.codex_sessions import require_free_machine
        require_free_machine(machine, self.peer_managers,
                             excluding_manager=self, excluding_session=excluding)

    def health(self):
        return self._request("/api/codex-health")

    def event_history(self, session_id, cursor=None, limit=80):
        session_id = self._session_id(session_id)
        if type(limit) is not int or not 1 <= limit <= 80:
            self._fail("History limit must be between 1 and 80")
        query = {"limit": str(limit)}
        if cursor is not None:
            if not isinstance(cursor, str) or len(cursor) > 512:
                self._fail("History cursor is invalid")
            query["cursor"] = cursor
        return self._request(f"/api/codex-sessions/{session_id}/events?" + urlencode(query))

    def save_profile(self, body):
        return self._request("/api/codex-machines", body)

    def check_machine(self, body):
        return self._request("/api/codex-machines/check", body)

    def create(self, body):
        if body.get("provider", "codex") != "codex":
            self._fail("This existing Codex owner cannot run another agent provider")
        with self.machine_lock:
            machine = dict(body)
            if body.get("profile_id"):
                profile = next((profile for profile in self.snapshot(compact=True).get("machine_profiles", [])
                                if profile.get("id") == body["profile_id"]), None)
                if profile is None:
                    self._fail("Unknown machine profile")
                machine = {**profile, **body}
            if not isinstance(machine.get("ssh_alias"), str) or not machine["ssh_alias"]:
                self._fail("SSH alias is required")
            self._require_adapt(machine)
            self._require_free_machine(machine)
            # create is preparation only; flflow separately invokes start.
            return self._request("/api/codex-sessions", {**body, "start": False})

    def delete(self, session_id):
        session_id = self._session_id(session_id)
        with self.machine_lock:
            return self._request(f"/api/codex-sessions/{session_id}/delete", {})

    def restore(self, session_id):
        session_id = self._session_id(session_id)
        with self.machine_lock:
            session = self._get(session_id, include_deleted=True)
            if session.get("deleted_at") and (session.get("status") in
                    {"prepared", "starting", "running", "waiting_approval", "waiting_input", "interrupting"}
                    or (session.get("execution_owner") == "codex_desktop"
                        and not session.get("desktop_lease_released"))):
                self._require_free_machine(session, excluding=session_id)
            return self._request(f"/api/codex-sessions/{session_id}/restore", {})

    def start(self, session_id):
        with self.machine_lock:
            session = self._get(session_id)
            self._require_adapt(session)
            self._require_free_machine(session, excluding=session_id)
            return self._request(f"/api/codex-sessions/{session_id}/start", {})

    def message(self, session_id, text):
        with self.machine_lock:
            session = self._get(session_id)
            self._require_adapt(session)
            self._require_free_machine(session, excluding=session_id)
            return self._request(f"/api/codex-sessions/{session_id}/message", {"text": text})

    def interrupt(self, session_id):
        return self._request(f"/api/codex-sessions/{self._session_id(session_id)}/interrupt", {})

    def desktop(self, session_id):
        # The original service releases its own client and opens the same persisted thread.
        return self._request(f"/api/codex-sessions/{self._session_id(session_id)}/desktop", {})

    def respond(self, session_id, request_id, response):
        session = self._get(session_id)
        if self._policy(session).get("mode") == "monitor":
            if not isinstance(response, dict) or not (response.get("decision") in ("cancel", "decline") or response.get("permissions") == {}):
                self._fail("Monitor-only tasks allow only cancel/decline or empty permissions")
        return self._request(f"/api/codex-sessions/{session_id}/respond", {"request_id": request_id, "response": response})

    def close(self):
        # The original server retains ownership and execution of all sessions.
        pass


class ProjectRegistry:
    def __init__(self, projects, *, multi_project=True):
        self.projects = list(projects)
        if not self.projects:
            raise ProjectError("projects must contain at least one project")
        self._by_id = {}
        for project in self.projects:
            if not isinstance(project.id, str) or not re.fullmatch(r"[a-z][a-z0-9_-]{0,63}", project.id):
                raise ProjectError("Project IDs must be simple lowercase identifiers")
            if project.id in self._by_id:
                raise ProjectError(f"Duplicate project ID: {project.id}")
            self._by_id[project.id] = project
        self.multi_project = multi_project
        composites = [project.sessions for project in self.projects if project.sessions is not None]
        managers = [owner for manager in composites for owner in getattr(manager, "managers", [manager])]
        machine_lock = threading.RLock()
        for manager in composites + managers:
            manager.machine_lock = machine_lock
            manager.peer_managers = managers

    @classmethod
    def single(cls, workflow=None, monitor=None, sessions=None):
        config = getattr(monitor, "config", {})
        return cls([ProjectContext("vllm", "vLLM-Plugin-FL", config.get("engine", "vllm"),
                                   getattr(monitor, "branch", config.get("branch", "")),
                                   getattr(monitor, "github_repo", config.get("github_repo", "")),
                                   monitor, sessions, workflow)], multi_project=False)

    @classmethod
    def from_config(cls, path, *, read_only=False, monitor_factory=None,
                    sessions_factory=None, workflow_factory=None,
                    claude_sessions_factory=None, settings_factory=None,
                    flagscale_sessions_factory=None, flagscale_settings_factory=None):
        # Imports are lazy: a public share never imports or creates control sessions.
        if monitor_factory is None:
            monitor_factory = create_monitor
        if not read_only and sessions_factory is None:
            from fl_dashboard.agents.codex_sessions import CodexSessionManager
            sessions_factory = CodexSessionManager
        if not read_only and workflow_factory is None:
            from fl_dashboard.services.flflow import Engine
            workflow_factory = Engine
        path = Path(path).expanduser().resolve()
        settings_store = flagscale_settings_store = None
        if not read_only:
            if settings_factory is None:
                from fl_dashboard.agents.agent_settings import AgentSettingsStore
                settings_factory = AgentSettingsStore
            if claude_sessions_factory is None:
                from fl_dashboard.agents.claude_agent import ClaudeSessionManager
                claude_sessions_factory = ClaudeSessionManager
            if flagscale_settings_factory is None:
                from fl_dashboard.agents.agent_settings import FlagScaleSettingsStore
                flagscale_settings_factory = FlagScaleSettingsStore
            if flagscale_sessions_factory is None:
                from fl_dashboard.agents.flagscale_adapter import FlagScaleSessionManager
                flagscale_sessions_factory = FlagScaleSessionManager
            settings_directory = path.parent / "runs" / "agent-settings"
            settings_store = settings_factory(settings_directory)
            flagscale_settings_store = flagscale_settings_factory(settings_directory)
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise ProjectError(f"Cannot read projects config: {exc}") from exc
        rows = raw.get("projects") if isinstance(raw, dict) else None
        if not isinstance(rows, list) or not rows:
            raise ProjectError("projects must be a nonempty array")
        projects, prepared, directories = [], [], []
        for row in rows:
            if not isinstance(row, dict):
                raise ProjectError("Each project must be an object")
            project_id, label = row.get("id"), row.get("label")
            if not isinstance(project_id, str) or not re.fullmatch(r"[a-z][a-z0-9_-]{0,63}", project_id):
                raise ProjectError("Project IDs must be simple lowercase identifiers")
            if not isinstance(label, str) or not label.strip() or len(label) > 200:
                raise ProjectError("Each project label must be a nonempty string of at most 200 characters")
            project = ProjectContext(project_id, label, "sglang" if project_id == "sglang" else "vllm")
            projects.append(project)
            config_path = cls._path(path.parent, row.get("monitor_config"), "monitor_config")
            workflow_path = (cls._path(path.parent, row["workflow_config"], "workflow_config")
                             if row.get("workflow_config") is not None else None)
            try:
                monitor_raw = json.loads(config_path.read_text(encoding="utf-8"))
                config = dict(monitor_raw.get("monitor", monitor_raw))
                for key in ("repo", "plugin_repo"):
                    if key in config:
                        config[key] = str(cls._path(config_path.parent, config[key], key))
                project.engine_name = config.get("engine", project.engine_name)
                adapter = monitor_type(project.engine_name)
                config["tasks_enabled"] = adapter.supports_tasks and config.get("tasks_enabled", True) is True
                config["engine"] = project.engine_name
                project.default_branch = config.get("branch", "0.4.0-dev" if project.engine_name == "vllm" else "main")
                config.setdefault("branch", project.default_branch)
                project.github_repo = config.get("github_repo", "")
                defaults = {"sessions_dir": "runs/codex-sessions", "evidence_dir": "runs/gate-evidence"}
                for key, default in defaults.items():
                    if project_id != "vllm":
                        default = f"runs/{project_id}/" + default.split("/")[-1]
                    config[key] = str(cls._path(config_path.parent, config.get(key, default), key))
                    directories.append((project_id, key, Path(config[key])))
                config["claude_sessions_dir"] = str(cls._path(config_path.parent,
                    config.get("claude_sessions_dir", f"runs/{project_id}/claude-sessions"), "claude_sessions_dir"))
                claude_directory, codex_directory = Path(config["claude_sessions_dir"]), Path(config["sessions_dir"])
                if claude_directory.is_relative_to(codex_directory) or codex_directory.is_relative_to(claude_directory):
                    raise ProjectError("Claude and Codex session directories must be separate")
                directories.append((project_id, "claude_sessions_dir", claude_directory))
                config["flagscale_sessions_dir"] = str(cls._path(config_path.parent,
                    config.get("flagscale_sessions_dir", f"runs/{project_id}/flagscale-sessions"),
                    "flagscale_sessions_dir"))
                flagscale_directory = Path(config["flagscale_sessions_dir"])
                if any(flagscale_directory.is_relative_to(other) or other.is_relative_to(flagscale_directory)
                       for other in (codex_directory, claude_directory)):
                    raise ProjectError("FlagScale session directory must be separate from Claude and Codex")
                directories.append((project_id, "flagscale_sessions_dir", flagscale_directory))
                prepared.append((project, config, config_path.parent, workflow_path))
            except (OSError, ValueError, TypeError, AttributeError) as exc:
                project.monitor_error = f"Project monitor unavailable: {exc}"
                prepared.append((project, None, config_path.parent, workflow_path))
        registry = cls(projects)
        for index, (project_id, kind, directory) in enumerate(directories):
            for other_id, other_kind, other in directories[:index]:
                if project_id != other_id and (directory.is_relative_to(other) or other.is_relative_to(directory)):
                    raise ProjectError(f"Project runtime directories overlap: {other_id}.{other_kind} and {project_id}.{kind}")
        try:
            for project, config, base, workflow_path in prepared:
                if config is not None:
                    if config.get("external_tasks"):
                        try:
                            from fl_dashboard.tasks.native_task_observer import NativeTaskObserver
                            project.native_tasks = NativeTaskObserver(config["external_tasks"], base)
                        except (ValueError, OSError) as exc:
                            project.native_tasks_error = f"Native task observer unavailable: {exc}"
                    try:
                        project.monitor = monitor_factory(config)
                    except Exception as exc:
                        project.monitor_error = f"Project monitor unavailable: {exc}"
                    if read_only:
                        from fl_dashboard.tasks.readonly_session_observer import ReadonlySessionObserver
                        project.readonly_sessions = ReadonlySessionObserver([
                            {"provider": "codex", "directory": config["sessions_dir"]},
                            {"provider": "claude", "directory": config["claude_sessions_dir"]},
                            {"provider": "flagscale", "directory": config["flagscale_sessions_dir"]}])
                    else:
                        project.agent_settings = settings_store
                        project.flagscale_settings = flagscale_settings_store
                    if not read_only and project.monitor is not None and config.get("tasks_enabled", True):
                        from fl_dashboard.agents.composite_sessions import CompositeSessionManager
                        codex, claude, flagscale, provider_errors = None, None, None, {}
                        try:
                            codex = (RemoteSessionManager(config, base) if "sessions_api_url" in config
                                     else sessions_factory(config, base))
                        except Exception:
                            provider_errors["codex"] = "The Codex session owner is unavailable"
                        # Every added executor is local; existing owner routing is Codex-only.
                        local_config = {key: value for key, value in config.items()
                                        if not key.startswith("sessions_api_")}
                        claude_config = dict(local_config)
                        claude_config["sessions_dir"] = config["claude_sessions_dir"]
                        try:
                            claude = claude_sessions_factory(claude_config, base, settings_store=settings_store)
                        except Exception:
                            provider_errors["claude"] = "The Claude session owner is unavailable"
                        flagscale_config = dict(local_config)
                        flagscale_config["sessions_dir"] = config["flagscale_sessions_dir"]
                        try:
                            flagscale = flagscale_sessions_factory(
                                flagscale_config, base, settings_store=flagscale_settings_store)
                        except Exception:
                            provider_errors["flagscale"] = "The FlagScale session owner is unavailable"
                        if codex is not None or claude is not None or flagscale is not None:
                            project.sessions = CompositeSessionManager(
                                codex, claude, settings_store, flagscale=flagscale,
                                flagscale_settings=flagscale_settings_store, errors=provider_errors)
                        else:
                            project.sessions_error = "Project agent session providers are unavailable"
                if workflow_path is not None and not read_only and config is not None and config.get("tasks_enabled", True):
                    try:
                        project.workflow = workflow_factory(workflow_path)
                    except Exception as exc:
                        project.workflow_error = f"Project workflow unavailable: {exc}"
            # Share one allocation lock after every manager is available.
            return cls(projects)
        except BaseException:
            registry.close()
            raise

    @staticmethod
    def _path(base, value, name):
        if not isinstance(value, str) or not value.strip():
            raise ProjectError(f"{name} must be a nonempty path")
        candidate = Path(value).expanduser()
        return (candidate if candidate.is_absolute() else base / candidate).resolve()

    def select(self, query=""):
        params = parse_qs(query, keep_blank_values=True, max_num_fields=30)
        values = params.get("project")
        if values is None:
            return self.projects[0]
        if len(values) != 1 or values[0] not in self._by_id:
            raise ProjectError("Unknown or duplicate project")
        return self._by_id[values[0]]

    def metadata(self, *, read_only=False):
        return {"projects": [project.metadata(read_only=read_only) for project in self.projects],
                "read_only": read_only}

    def statuses(self):
        with ThreadPoolExecutor(max_workers=min(8, len(self.projects))) as pool:
            return {"projects": list(pool.map(lambda project: project.status(), self.projects))}

    def close(self):
        for project in self.projects:
            if project.sessions is not None:
                project.sessions.close()
