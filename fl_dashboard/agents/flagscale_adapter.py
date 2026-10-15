"""FlagScale Agent provider: private worker processes and explicit fresh follow-ups."""
from __future__ import annotations

import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import threading
import uuid

from fl_dashboard.agents.agent_settings import FlagScaleSettingsStore, validate_flagscale_settings, FLAGSCALE_DEFAULTS
from fl_dashboard.agents.claude_agent import process_environment
from fl_dashboard.agents.codex_sessions import CodexSessionManager, SessionError

KEY_ENV = "FLFLOW_FLAGSCALE_API_KEY"
CAPABILITIES = {"supports_resume": False, "supports_mid_turn_steer": False,
                "follow_up_mode": "fresh_turn_with_context"}
SAFE_REASONS = {"native_error", "missing_kernel_result", "llm_error", "task_complete",
                "need_user_input", "unverified_completion_signal", "max_continuations",
                "context_pressure", "completion_gate_livelock", "blocked_by_guard_at_max_iter",
                "empty_output_max_retries", "iteration_limit", "unrecognized_stop", "worker_unavailable", "interrupted"}


def find_flagscale_python(config=None):
    candidate = (config or {}).get("flagscale_python") or sys.executable
    return shutil.which(str(candidate)) or (str(candidate) if Path(str(candidate)).is_file() else None)


def flagscale_availability(python, executable=None, *, runner=subprocess.run):
    """Import/version probes only: no credential loading, agent construction or API calls."""
    result = {"provider": "flagscale", "available": False, "version": None, "error": None, **CAPABILITIES}
    if not python:
        result["error"] = "FlagScale Python executable was not found"
        return result
    probe = ("import contextlib,io,json,importlib.metadata; "
             "f=io.StringIO(); "
             "exec('with contextlib.redirect_stdout(f), contextlib.redirect_stderr(f):\\n"
             " from flagscale_agent.react.config import AgentConfig\\n"
             " from flagscale_agent.react.agent import WorkerAgent\\n"
             " from flagscale_agent.react.kernel import AgentKernel'); "
             "names=('provider','model','api_key','base_url','session_dir','shell_env','max_iterations','max_continuations',"
             "'max_context_tokens','max_output_tokens','thinking_budget','time_budget_sec'); "
             "ok=all(n in AgentConfig.__dataclass_fields__ for n in names) and all(callable(getattr(WorkerAgent,n,None)) "
             "for n in ('run','_react_loop','_save_conversation','_get_last_assistant_text')) and callable(getattr(AgentKernel,'run_turn',None)); "
             "print(json.dumps({'compatible':ok,'version':importlib.metadata.version('flagscale-agent')}))")
    flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
    try:
        if executable:
            cli = runner([str(executable), "--version"], capture_output=True, text=True,
                         encoding="utf-8", errors="replace", timeout=25, creationflags=flags, env=process_environment())
            if cli.returncode != 0:
                raise ValueError()
        checked = runner([python, "-B", "-c", probe], capture_output=True, text=True,
                         encoding="utf-8", errors="replace", timeout=25, creationflags=flags, env=process_environment())
        value = json.loads(checked.stdout or "")
        result["available"] = checked.returncode == 0 and value.get("compatible") is True
        version = value.get("version")
        result["version"] = version if isinstance(version, str) and re.fullmatch(r"[A-Za-z0-9.+-]{1,80}", version) else None
        if not result["available"]:
            raise ValueError()
    except (OSError, subprocess.TimeoutExpired, ValueError, TypeError, AttributeError):
        result["available"] = False
        result["error"] = "The configured Python must have a compatible FlagScale Agent installation"
    return result


class _ConnectionProcess:
    def __init__(self, client):
        self.client = client

    def poll(self):
        return 0 if self.client.closed else None


class FlagScaleClient:
    """Dashboard logical thread; every turn launches a new native FlagScale session."""
    def __init__(self, python, cwd, callback, *, settings, state_dir,
                 popen=subprocess.Popen, stop_process=None, worker_path=None):
        self.python, self.cwd, self.callback = python, str(cwd), callback
        self.state_dir = Path(state_dir)
        self.worker_path = Path(worker_path or Path(__file__).with_name("flagscale_worker.py"))
        self._settings = {**validate_flagscale_settings(settings), "api_key": settings["api_key"]}
        self._popen, self._stop_process = popen, stop_process
        self._lock = threading.RLock()
        self._thread_id = self._turn_id = self._child = self._reader = None
        self._interrupted, self.closed = False, False
        self.process, self.stderr = _ConnectionProcess(self), []
        self.info = {"userAgent": "FlagScale Agent private single-shot worker", **CAPABILITIES}

    @property
    def active(self):
        return bool(self._reader and self._reader.is_alive())

    def _safe(self, value):
        key = self._settings["api_key"]
        if isinstance(value, str):
            return value.replace(key, "[redacted]")[:24000] if key else value[:24000]
        if isinstance(value, dict):
            return {self._safe(str(k)): self._safe(v) for k, v in list(value.items())[:100]
                    if str(k).lower() not in {"api_key", "apikey", "authorization", "headers", "env"}}
        if isinstance(value, list):
            return [self._safe(item) for item in value[:100]]
        return value if value is None or isinstance(value, (bool, int, float)) else None

    def _emit(self, method, params):
        try:
            self.callback({"method": method, "params": self._safe({"threadId": self._thread_id, **params})})
        except Exception:
            pass

    def request(self, method, params):
        with self._lock:
            if self.closed:
                raise SessionError("FlagScale connection is closed")
            if method in {"thread/start", "thread/resume"}:
                if self.active:
                    raise SessionError("FlagScale already has an active turn")
                # Restore only the dashboard context identity, never native --resume.
                try:
                    self._thread_id = str(uuid.UUID(params["threadId"])) if method == "thread/resume" else str(uuid.uuid4())
                except (KeyError, TypeError, ValueError, AttributeError):
                    raise SessionError("Invalid stored FlagScale dashboard thread ID") from None
                return {"thread": {"id": self._thread_id}, "model": self._settings["model"], **CAPABILITIES}
            if method == "turn/start":
                return self._start(params)
            if method == "turn/steer":
                raise SessionError("FlagScale is running; wait or interrupt before starting a follow-up")
            if method != "turn/interrupt":
                raise SessionError("Unsupported FlagScale operation")
            if params.get("threadId") != self._thread_id or params.get("turnId") != self._turn_id:
                raise SessionError("FlagScale interruption identity mismatch")
            self._interrupted = True
            child = self._child
        if child is not None:
            self._stop(child)
        return {}

    def _start(self, params):
        if self.active or self._child is not None and self._child.poll() is None:
            raise SessionError("FlagScale already has an active turn")
        if not self._thread_id or params.get("threadId") != self._thread_id:
            raise SessionError("FlagScale dashboard thread identity mismatch")
        parts = params.get("input", [])
        if not isinstance(parts, list) or any(not isinstance(item, dict) or item.get("type") != "text"
                                             or not isinstance(item.get("text"), str) for item in parts):
            raise SessionError("FlagScale input must contain text")
        prompt = "\n".join(item["text"] for item in parts)
        if not prompt.strip() or len(prompt.encode("utf-8")) > 900000:
            raise SessionError("FlagScale prompt is empty or too large")
        if self._settings["api_key"] and self._settings["api_key"] in prompt:
            raise SessionError("API key must not be included in task text")
        state = self.state_dir.resolve()
        cwd = Path(self.cwd).resolve()
        if state == cwd or state.is_relative_to(cwd):
            raise SessionError("FlagScale private state must be outside the task worktree")
        state.mkdir(parents=True, exist_ok=True)
        env = process_environment()
        env[KEY_ENV] = self._settings["api_key"]
        # Windows pipe defaults can differ from the dashboard's UTF-8 writer.
        env.update(PYTHONUTF8="1", PYTHONIOENCODING="utf-8")
        turn_id = uuid.uuid4().hex
        payload = {"schema_version": 1, "thread_id": self._thread_id, "turn_id": turn_id,
                   "prompt": prompt, "settings": {name: self._settings[name] for name in FLAGSCALE_DEFAULTS}, "state_dir": str(state)}
        flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
        try:
            child = self._popen([self.python, "-B", str(self.worker_path.resolve())], cwd=self.cwd, env=env,
                                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                text=True, encoding="utf-8", errors="replace", bufsize=1, creationflags=flags)
        except OSError:
            raise SessionError("Could not start the private FlagScale worker") from None
        self._child, self._turn_id, self._interrupted = child, turn_id, False
        self._emit("turn/started", {"turn": {"id": turn_id}})
        self._reader = threading.Thread(target=self._read, args=(child, turn_id), daemon=True)
        threading.Thread(target=self._read_stderr, args=(child,), daemon=True).start()
        try:
            child.stdin.write(json.dumps(payload, ensure_ascii=False))
            child.stdin.close()
        except (OSError, BrokenPipeError):
            pass
        self._reader.start()
        return {"turn": {"id": turn_id, "status": "inProgress"}}

    def _read_stderr(self, child):
        while child.stderr.readline(16385):
            self.stderr.append("FlagScale worker emitted diagnostic output")
            self.stderr[:] = self.stderr[-20:]
        child.stderr.close()

    def _read(self, child, turn_id):
        result, invalid, initialized, native_id = None, False, False, None
        try:
            while True:
                line = child.stdout.readline(1024 * 1024 + 1)
                if not line:
                    break
                if len(line) > 1024 * 1024:
                    raise ValueError()
                value = json.loads(line)
                if (not isinstance(value, dict) or value.get("thread_id") != self._thread_id
                        or value.get("turn_id") != turn_id or result is not None):
                    raise ValueError()
                kind = value.get("type")
                if kind == "init":
                    if initialized or not re.fullmatch(r"[0-9a-f]{8}", str(value.get("native_session_id", ""))):
                        raise ValueError()
                    native_id, initialized = value["native_session_id"], True
                    self._emit("flagscale/init", {"native_session_id": native_id, **CAPABILITIES})
                elif kind == "result":
                    if (value.get("status") not in {"completed", "waiting_input", "failed", "interrupted"}
                            or value.get("stop_reason") not in SAFE_REASONS):
                        raise ValueError()
                    if value.get("status") != "failed" and (not initialized or value.get("native_session_id") != native_id):
                        raise ValueError()
                    if value.get("status") == "completed" and value.get("stop_reason") != "task_complete":
                        raise ValueError()
                    result = self._safe(value)
                elif initialized and kind == "assistant" and isinstance(value.get("text"), str):
                    self._emit("item/completed", {"item": {"type": "agentMessage", "text": value["text"]}})
                elif initialized and kind == "tools" and isinstance(value.get("names"), list):
                    names = [name for name in value["names"][:100] if isinstance(name, str) and re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{0,79}", name)]
                    self._emit("flagscale/tools", {"names": names})
                else:
                    raise ValueError()
            code = child.wait()
        except (OSError, ValueError, TypeError):
            invalid = True
            self._stop(child)
            code = child.poll()
        if self._interrupted or self.closed:
            status, reason, error = "interrupted", "interrupted", None
        elif invalid or result is None or code != 0:
            status, reason, error = "failed", "worker_unavailable", "FlagScale worker exited without a verified final result"
        else:
            status, reason = result["status"], result["stop_reason"]
            error = "FlagScale execution failed (" + reason + ")" if status == "failed" else None
        try:
            child.stdout.close()
        except OSError:
            pass
        if result and isinstance(result.get("text"), str):
            self._emit("item/completed", {"item": {"type": "agentMessage", "text": result["text"]}})
        self._emit("flagscale/result", {"status": status, "stop_reason": reason, "native_session_id": native_id,
                    **({name: result[name] for name in ("iterations", "input_tokens", "output_tokens") if type(result.get(name)) is int} if result else {})})
        # Shared lifecycle accepts three terminal states. The manager projects
        # native waiting-input after this event; it cannot become completed.
        self._emit("turn/completed", {"turn": {"id": turn_id, "status": "failed" if status == "waiting_input" else status, "error": error}})

    def _stop(self, child):
        if child.poll() is not None:
            return
        if self._stop_process:
            self._stop_process(child)
            return
        try:
            if os.name == "nt":
                subprocess.run(["taskkill", "/PID", str(child.pid), "/T", "/F"], stdout=subprocess.DEVNULL,
                               stderr=subprocess.DEVNULL, timeout=10, creationflags=subprocess.CREATE_NO_WINDOW)
            else:
                child.terminate()
            child.wait(timeout=5)
        except (OSError, subprocess.TimeoutExpired):
            child.kill()

    def respond(self, request_id, response):
        raise SessionError("FlagScale single-shot execution does not support dashboard interactive approvals")

    def close(self):
        with self._lock:
            self.closed, self._interrupted = True, True
            child = self._child
        if child is not None:
            self._stop(child)


class FlagScaleSessionManager(CodexSessionManager):
    def __init__(self, config, base_dir, settings_store=None, *, flagscale_client_factory=FlagScaleClient,
                 availability_checker=flagscale_availability):
        if config.get("sessions_api_url"):
            raise SessionError("FlagScale sessions require a separate local sessions directory")
        self.agent_settings = settings_store or FlagScaleSettingsStore(Path(base_dir) / "runs" / "agent-settings")
        self.flagscale_client_factory, self.availability_checker = flagscale_client_factory, availability_checker
        self.flagscale_executable = config.get("flagscale_executable")
        super().__init__(config, base_dir)
        self.executable = find_flagscale_python(config)
        for session in self._sessions.values():
            if session.get("status") == "disconnected":
                session["error"] = "Dashboard restarted; follow up starts a fresh FlagScale run with stored task context."
            session.update(provider="flagscale", **CAPABILITIES)

    def _prompt(self, session):
        return super()._prompt(session) + (
            "\n本任务由 FlagScale Agent single-shot API 执行。主动读取 worktree 中的 AGENTS.md 和相关技能，持续写 .flflow-progress.json。"
            "不得读取或输出 API Key、其他 agent 认证文件或本机密钥。不得把执行轮次结束当作 Gate 验收通过，必须保存实际测试证据。"
            "遇到权限拒绝或缺少必要输入时报告阻塞；长时间远程任务遵循已有 tmux 约定。"
        )

    def _reject_secret(self, value):
        key = self.agent_settings.runtime()["api_key"]
        if key in json.dumps(value, ensure_ascii=False):
            raise SessionError("API key must not be included in task text or public task fields")

    def create(self, payload):
        if payload.get("provider") != "flagscale":
            raise SessionError("FlagScale manager accepts provider=flagscale only")
        if any(name in payload for name in set(FLAGSCALE_DEFAULTS) | {"api_key", "authorization", "env", "headers"}):
            raise SessionError("Save FlagScale API settings through local agent settings")
        public = self.agent_settings.snapshot()
        if not public["configured"]:
            raise SessionError("Configure the local FlagScale API key before creating a FlagScale task")
        self._reject_secret(payload)
        result = super().create({key: value for key, value in payload.items() if key != "provider"})
        with self._lock:
            session = self._sessions[result["id"]]
            session.update(provider="flagscale", model=public["model"], **CAPABILITIES)
            self._save(session)
            return self._session_view(session)

    def health(self):
        result = self.availability_checker(self.executable, self.flagscale_executable)
        public = self.agent_settings.snapshot()
        result.update(configured=public["configured"], authenticated=None, account_type="api_key")
        if not result.get("error") and not public["configured"]:
            result["error"] = "Configure a local FlagScale API key; no API request has been made"
        with self._lock:
            self._health = result
        return result

    def snapshot(self, compact=False):
        result = super().snapshot(compact=compact)
        result.update(provider="flagscale", agent_settings=self.agent_settings.snapshot(), **CAPABILITIES)
        for session in result["sessions"]:
            session.update(provider="flagscale", **CAPABILITIES)
        return result

    def _client(self, session):
        prior = self._clients.get(session["id"])
        if prior and prior.active:
            return prior
        if not self.executable or not self.availability_checker(self.executable, self.flagscale_executable)["available"]:
            raise SessionError("The configured Python must have a compatible FlagScale Agent installation")
        if prior:
            prior.close()
        runtime = self.agent_settings.runtime()
        client = self.flagscale_client_factory(self.executable, session["worktree"],
                   lambda value: self._event(session["id"], value), settings=runtime,
                   state_dir=self.root / "native" / session["id"])
        self._clients[session["id"]] = client
        method = "thread/resume" if session.get("thread_id") else "thread/start"
        result = client.request(method, {"threadId": session.get("thread_id")})
        with self._lock:
            session.update(thread_id=result["thread"]["id"], model=runtime["model"], **CAPABILITIES)
            session.pop("flagscale_result", None)
            self._save(session)
        return client

    def _prepare_followup(self, session_id):
        with self.machine_lock, self._lock:
            session = self._get(session_id)
            client = self._clients.get(session_id)
            if session["status"] == "waiting_input" and not (client and client.active):
                session.update(status="interrupted", turn_id=None)
                self._save(session)

    def start(self, session_id):
        self._prepare_followup(session_id)
        return super().start(session_id)

    def message(self, session_id, text):
        self._reject_secret(text)
        self._prepare_followup(session_id)
        result = super().message(session_id, text)
        # The shared manager only records mid-turn steer messages; FlagScale
        # accepts idle follow-ups and must retain them for subsequent fresh runs.
        with self._lock:
            thread_id = self._get(session_id).get("thread_id")
        self._event(session_id, {"method": "dashboard/message", "params": {"threadId": thread_id, "text": text}})
        return result

    def _launch(self, session_id, text):
        with self._lock:
            session = self._get(session_id)
            if session.get("initial_prompt_sent"):
                history = [event.get("params", {}).get("text", "") for event in session.get("events", [])
                           if event.get("method") == "dashboard/message"][-8:]
                prior = str(session.get("last_agent_message", ""))[:12000]
                text = (session["prompt"] + "\n\nFlagScale 跟进说明：以下是已有任务上下文，本轮为新的原生执行，不是原生会话续接。"
                        "先核对工作目录、进展文件、远程 tmux 状态和证据，避免重复执行已完成工作。"
                        + "\n此前用户补充：\n" + "\n".join(history)[:12000]
                        + "\n上一轮结果：\n" + prior + "\n本轮用户要求：\n" + text)
        return super()._launch(session_id, text)

    def _event(self, session_id, message):
        with self._lock:
            # Project native waiting-input and the shared terminal state under
            # one lock, so another provider cannot observe a freed machine lease.
            super()._event(session_id, message)
            params = message.get("params", {})
            session = self._sessions.get(session_id)
            if not session or params.get("threadId") != session.get("thread_id"):
                return
            if message.get("method") == "flagscale/init":
                session["flagscale_session_id"] = params.get("native_session_id")
                self._save(session)
            elif message.get("method") == "flagscale/result":
                session["flagscale_result"] = {key: params.get(key) for key in ("status", "stop_reason", "native_session_id", "iterations", "input_tokens", "output_tokens")}
                self._save(session)
            elif message.get("method") == "turn/completed":
                result = session.get("flagscale_result", {})
                if session["status"] == "failed" and result.get("status") == "waiting_input":
                    session.update(status="waiting_input", error="FlagScale 本轮需要后续输入或达到运行限制（" + str(result.get("stop_reason")) + "）。跟进会在同一工作目录启动新一轮执行。")
                    self._save(session)
