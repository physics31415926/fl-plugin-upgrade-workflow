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

    def metadata(self, *, read_only=False):
        return {"id": self.id, "label": self.label, "engine": self.engine_name,
                "default_branch": self.default_branch, "github_repo": self.github_repo,
                "sessions": not read_only and self.sessions is not None,
                "workflow": not read_only and self.workflow is not None}

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
        from codex_sessions import SessionError
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

    def _get(self, session_id):
        session_id = self._session_id(session_id)
        for session in self.allocation_sessions():
            if session.get("id") == session_id:
                return session
        self._fail("Unknown session")

    def _require_free_machine(self, machine, excluding=None):
        from codex_sessions import require_free_machine
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
        managers = [project.sessions for project in self.projects if project.sessions is not None]
        machine_lock = threading.RLock()
        for manager in managers:
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
                    sessions_factory=None, workflow_factory=None):
        # Imports are lazy: a public share never imports or creates control sessions.
        if monitor_factory is None:
            from branch_monitor import BranchMonitor
            monitor_factory = BranchMonitor
        if not read_only and sessions_factory is None:
            from codex_sessions import CodexSessionManager
            sessions_factory = CodexSessionManager
        if not read_only and workflow_factory is None:
            from flflow import Engine
            workflow_factory = Engine
        path = Path(path).expanduser().resolve()
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
                if project.engine_name not in ("vllm", "sglang"):
                    raise ProjectError("engine must be vllm or sglang")
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
                    try:
                        project.monitor = monitor_factory(config)
                    except Exception as exc:
                        project.monitor_error = f"Project monitor unavailable: {exc}"
                    if not read_only and project.monitor is not None:
                        try:
                            project.sessions = (RemoteSessionManager(config, base) if "sessions_api_url" in config
                                                else sessions_factory(config, base))
                        except Exception as exc:
                            project.sessions_error = f"Project sessions unavailable: {exc}"
                if workflow_path is not None and not read_only:
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
