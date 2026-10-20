"""Claude Code headless adapter, isolated from existing Codex app-server owners."""
from __future__ import annotations

import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import threading
import uuid

from fl_dashboard.agents.codex_sessions import CodexSessionManager, SessionError
from fl_dashboard.agents.agent_settings import AgentSettingsStore, validate_public_settings


def find_claude_executable(config=None):
    configured = (config or {}).get("claude_executable")
    candidate = configured or shutil.which("claude")
    if candidate:
        return str(candidate)
    fallback = Path.home() / ".local" / "bin" / ("claude.exe" if os.name == "nt" else "claude")
    return str(fallback) if fallback.is_file() else None


def process_environment():
    """Minimal process/SSH-agent plumbing without inherited authentication."""
    allowed = {"PATH", "PATHEXT", "SYSTEMROOT", "WINDIR", "COMSPEC", "SYSTEMDRIVE",
               "TEMP", "TMP", "TMPDIR", "USERPROFILE", "HOMEDRIVE", "HOMEPATH",
               "HOME", "APPDATA", "LOCALAPPDATA", "PROGRAMFILES", "PROGRAMFILES(X86)",
               "PROGRAMW6432", "PROGRAMDATA", "SSH_AUTH_SOCK", "SSH_AGENT_PID",
               "LANG", "LC_ALL", "LC_CTYPE"}
    return {key: value for key, value in os.environ.items() if key.upper() in allowed}


def cli_availability(executable, *, runner=subprocess.run):
    """Version/help probes only. Never authenticate or make a model request."""
    result = {"provider": "claude", "available": False, "version": None,
              "supports_bare": False, "error": None}
    if not executable:
        result["error"] = "Claude Code CLI was not found"
        return result
    flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
    try:
        version = runner([executable, "--version"], capture_output=True, text=True,
                         encoding="utf-8", errors="replace", timeout=25, creationflags=flags, env=process_environment())
        help_result = runner([executable, "--help"], capture_output=True, text=True,
                             encoding="utf-8", errors="replace", timeout=25, creationflags=flags, env=process_environment())
        match = re.search(r"\b(\d+\.\d+\.\d+)\b", version.stdout or "")
        result["version"] = match.group(1) if match else None
        result["supports_bare"] = "--bare" in (help_result.stdout or "")
        result["available"] = version.returncode == 0 and help_result.returncode == 0 and result["supports_bare"]
        if not result["available"]:
            result["error"] = "Claude CLI must support --bare headless mode"
    except (OSError, subprocess.TimeoutExpired):
        result["error"] = "Could not check the local Claude CLI"
    return result


class _ClientProcess:
    """Stable connection facade; each CLI turn owns a separate real process."""
    def __init__(self, client):
        self.client = client

    def poll(self):
        return 0 if self.client.closed else None


class ClaudeClient:
    """Map a bounded Claude stream to dashboard lifecycle/events.

    Keys enter only the child environment. Prompts enter stdin rather than argv.
    No permission host is installed: unapproved tools are denied by print mode.
    """
    def __init__(self, executable, cwd, callback, *, settings, config_dir,
                 popen=subprocess.Popen, stop_process=None):
        self.executable = executable
        self.cwd = str(cwd)
        self.callback = callback
        self.config_dir = Path(config_dir)
        self._settings = {**validate_public_settings(settings), "api_key": settings["api_key"]}
        self._popen = popen
        self._stop_process = stop_process
        self._lock = threading.RLock()
        self._child = None
        self._reader = None
        self._thread_id = None
        self._turn_id = None
        self._resume = False
        self._interrupted = False
        self.closed = False
        self.process = _ClientProcess(self)
        self.info = {"userAgent": "Claude Code headless"}
        self.stderr = []

    @property
    def active(self):
        with self._lock:
            return bool(self._reader and self._reader.is_alive())

    def _safe(self, value):
        key = self._settings["api_key"]
        if isinstance(value, str):
            return value.replace(key, "[redacted]")[:24000] if key else value[:24000]
        if isinstance(value, dict):
            # Never retain auth/config payload fields from arbitrary stream objects.
            return {self._safe(str(k)): self._safe(v) for k, v in list(value.items())[:100]
                    if str(k).lower() not in {"api_key", "apikey", "authorization", "headers", "env"}}
        if isinstance(value, list):
            return [self._safe(item) for item in value[:100]]
        return value if value is None or isinstance(value, (bool, int, float)) else str(type(value).__name__)

    def _emit(self, method, params):
        value = {"method": method, "params": self._safe({"threadId": self._thread_id, **params})}
        try:
            self.callback(value)
        except Exception:
            # Callback exception text can contain a key/path; never log it.
            pass

    def _environment(self):
        env = process_environment()
        env.update(ANTHROPIC_API_KEY=self._settings["api_key"],
                   ANTHROPIC_BASE_URL=self._settings["base_url"],
                   CLAUDE_CONFIG_DIR=str(self.config_dir),
                   CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC="1")
        return env

    def _command(self):
        settings = self._settings
        command = [self.executable, "--bare", "-p", "--output-format", "stream-json",
                   "--verbose", "--include-partial-messages",
                   "--permission-mode", settings["permission_mode"],
                   "--model", settings["model"], "--max-turns", str(settings["max_turns"])]
        command += ["--resume" if self._resume else "--session-id", self._thread_id]
        if settings["allowed_tools"]:
            command += ["--allowedTools", *settings["allowed_tools"]]
        return command

    def request(self, method, params):
        with self._lock:
            if self.closed:
                raise SessionError("Claude connection is closed")
            if method in {"thread/start", "thread/resume"}:
                if self.active:
                    raise SessionError("Claude already has an active turn")
                thread_id = params.get("threadId") if method == "thread/resume" else str(uuid.uuid4())
                try:
                    self._thread_id = str(uuid.UUID(thread_id))
                except (ValueError, TypeError, AttributeError):
                    raise SessionError("Invalid stored Claude session ID") from None
                self._resume = method == "thread/resume"
                return {"thread": {"id": self._thread_id}, "model": self._settings["model"]}
            if method == "turn/start":
                return self._start(params)
            if method == "turn/interrupt":
                if params.get("threadId") != self._thread_id or params.get("turnId") != self._turn_id:
                    raise SessionError("Claude interruption identity mismatch")
                self._interrupted = True
                child = self._child
            elif method == "turn/steer":
                raise SessionError("Claude is running; wait for this turn or interrupt it before sending a follow-up")
            else:
                raise SessionError("Unsupported Claude headless operation")
        if child is not None and child.poll() is None:
            self._stop(child)
        elif not self.active:
            self._emit("turn/completed", {"turn": {"id": self._turn_id, "status": "interrupted", "error": None}})
        return {}

    def _start(self, params):
        if self.active or self._child is not None and self._child.poll() is None:
            raise SessionError("Claude already has an active turn")
        if not self._thread_id:
            raise SessionError("Claude thread is not initialized")
        if params.get("threadId") != self._thread_id:
            raise SessionError("Claude thread identity mismatch")
        parts = params.get("input", [])
        if not isinstance(parts, list) or any(not isinstance(item, dict) or item.get("type") != "text"
                                             or not isinstance(item.get("text"), str) for item in parts):
            raise SessionError("Claude input must contain text")
        prompt = "\n".join(item["text"] for item in parts)
        if not prompt.strip() or len(prompt.encode("utf-8")) > 1024 * 1024:
            raise SessionError("Claude prompt is empty or too large")
        self.config_dir.mkdir(parents=True, exist_ok=True)
        flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
        try:
            child = self._popen(self._command(), cwd=self.cwd, env=self._environment(),
                                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                text=True, encoding="utf-8", errors="replace", bufsize=1,
                                creationflags=flags)
        except OSError:
            raise SessionError("Could not start the local Claude CLI") from None
        self._child = child
        self._interrupted = False
        self._turn_id = uuid.uuid4().hex
        self._emit("turn/started", {"turn": {"id": self._turn_id}})
        self._reader = threading.Thread(target=self._read, args=(child, self._turn_id), daemon=True)
        threading.Thread(target=self._read_stderr, args=(child,), daemon=True).start()
        try:
            child.stdin.write(prompt)
            child.stdin.close()
        except (OSError, BrokenPipeError):
            # Reader determines the actual CLI error/result and retains identity.
            pass
        self._reader.start()
        self._resume = True
        return {"turn": {"id": self._turn_id, "status": "inProgress"}}

    def _read_stderr(self, child):
        while True:
            line = child.stderr.readline(16385)
            if not line:
                break
            # Stderr has no framed messages; retain only generic diagnostics
            # so secrets spanning chunk/line boundaries cannot be reconstructed.
            self.stderr.append("Claude CLI emitted diagnostic output")
            self.stderr[:] = self.stderr[-20:]
        child.stderr.close()

    def _stream_event(self, value):
        kind = value.get("type")
        if kind == "stream_event":
            # A key split across chunks cannot be redacted per chunk. Only
            # persist complete assistant/result text after whole-message scrub.
            return
        elif kind == "assistant":
            message = value.get("message")
            blocks = message.get("content", []) if isinstance(message, dict) else []
            texts = []
            for block in blocks[:100] if isinstance(blocks, list) else []:
                if isinstance(block, dict) and block.get("type") == "text" and isinstance(block.get("text"), str):
                    texts.append(block["text"])
                elif isinstance(block, dict) and block.get("type") == "tool_use":
                    self._emit("claude/tool", {"tool": block})
            if texts:
                self._emit("item/completed", {"item": {"type": "agentMessage", "text": "\n".join(texts)}})
        elif kind == "user":
            message = value.get("message")
            blocks = message.get("content", []) if isinstance(message, dict) else []
            for block in blocks[:100] if isinstance(blocks, list) else []:
                if isinstance(block, dict) and block.get("type") == "tool_result":
                    self._emit("claude/tool_result", {"result": block})
        elif kind == "system":
            if value.get("subtype") == "init":
                if value.get("session_id") != self._thread_id:
                    raise SessionError("Claude stream session identity mismatch")
                self._emit("claude/init", {"model": value.get("model"), "permission_mode": self._settings["permission_mode"]})
            else:
                self._emit("claude/system", {key: value[key] for key in
                           ("subtype", "error", "error_status", "attempt", "max_retries") if key in value})

    def _read(self, child, turn_id):
        result = None
        protocol_error = None
        try:
            while True:
                line = child.stdout.readline(1024 * 1024 + 1)
                if not line:
                    break
                if len(line) > 1024 * 1024:
                    raise SessionError("Claude stream record exceeded its size limit")
                try:
                    value = json.loads(line)
                except json.JSONDecodeError:
                    protocol_error = "Claude emitted an invalid stream record"
                    continue
                if not isinstance(value, dict):
                    protocol_error = "Claude emitted an invalid stream record"
                    continue
                if value.get("session_id") and value["session_id"] != self._thread_id:
                    raise SessionError("Claude stream session identity mismatch")
                if value.get("type") == "result":
                    if result is not None:
                        raise SessionError("Claude emitted multiple final results")
                    if value.get("session_id") != self._thread_id:
                        raise SessionError("Claude final result session identity is missing")
                    result = self._safe(value)
                else:
                    self._stream_event(value)
            code = child.wait()
        except (OSError, SessionError):
            protocol_error = "Claude stream could not be verified"
            self._stop(child)
            code = child.poll()
        if self._interrupted or self.closed:
            status, error = "interrupted", None
        elif protocol_error:
            status, error = "failed", protocol_error
        elif result is None:
            status, error = "failed", "Claude exited without a final result"
        elif code != 0 or result.get("is_error") or result.get("subtype") != "success":
            status = "failed"
            error = result.get("result") or "Claude did not complete this turn (" + str(result.get("subtype", "error")) + ")"
        else:
            status, error = "completed", None
        try:
            child.stdout.close()
        except OSError:
            pass
        if result:
            text = result.get("result")
            if isinstance(text, str):
                self._emit("item/completed", {"item": {"type": "agentMessage", "text": text}})
            self._emit("claude/result", {key: result[key] for key in
                       ("subtype", "permission_denials", "num_turns", "duration_ms", "total_cost_usd") if key in result})
        self._emit("turn/completed", {"turn": {"id": turn_id, "status": status, "error": self._safe(error)}})

    def _stop(self, child):
        if child.poll() is not None:
            return
        if self._stop_process is not None:
            self._stop_process(child)
            return
        try:
            if os.name == "nt":
                # Only the process launched by this adapter and its descendants.
                subprocess.run(["taskkill", "/PID", str(child.pid), "/T", "/F"],
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=10,
                               creationflags=subprocess.CREATE_NO_WINDOW)
            else:
                child.terminate()
            child.wait(timeout=5)
        except (OSError, subprocess.TimeoutExpired):
            child.kill()

    def respond(self, request_id, response):
        raise SessionError("Claude headless does not support interactive approvals; configure explicit tool rules and resume")

    def close(self):
        with self._lock:
            self.closed = True
            self._interrupted = True
            child = self._child
        if child is not None:
            self._stop(child)


class ClaudeSessionManager(CodexSessionManager):
    """Separate local store and CLI for Claude; never forwards to a Codex owner."""
    def __init__(self, config, base_dir, settings_store=None, *, claude_client_factory=ClaudeClient):
        if config.get("sessions_api_url"):
            raise SessionError("Claude sessions require a separate local sessions directory")
        self.agent_settings = settings_store or AgentSettingsStore(Path(base_dir) / "runs" / "agent-settings")
        self.claude_client_factory = claude_client_factory
        super().__init__(config, base_dir)
        self.executable = find_claude_executable(config)

    def _prompt(self, session):
        return super()._prompt(session) + (
            "\n本任务由 Claude Code bare CLI 执行。先主动读取并遵循 worktree 中的 AGENTS.md 和相关升级技能；"
            "bare 模式不自动载入这些文件，不可假定已经读取。持续写 .flflow-progress.json。"
            "权限未被明确许可时应报告阻塞，不绕过拒绝，不改为 bypassPermissions。"
            "只在界面明确配置的 API 与工具许可范围执行；不得读取或输出 API Key、CLI认证文件或本机其他密钥。"
        )

    def _reject_secret(self, value):
        key = self.agent_settings.runtime()["api_key"]
        if key in json.dumps(value, ensure_ascii=False):
            raise SessionError("API key must not be included in task text or public task fields")

    def create(self, payload):
        if payload.get("provider") != "claude":
            raise SessionError("Claude manager accepts provider=claude only")
        if any(key in payload for key in ("api_key", "base_url", "allowed_tools", "permission_mode")):
            raise SessionError("Save Claude credentials and permissions through local agent settings")
        public = self.agent_settings.snapshot()
        if not public["configured"]:
            raise SessionError("Configure the local Claude API key before creating a Claude task")
        self._reject_secret(payload)
        result = super().create({key: value for key, value in payload.items() if key != "provider"})
        with self._lock:
            session = self._sessions[result["id"]]
            session.update(provider="claude", model=public["model"], permission_mode=public["permission_mode"])
            self._save(session)
            return self._session_view(session)

    def health(self):
        result = cli_availability(self.executable)
        public = self.agent_settings.snapshot()
        result.update(configured=public["configured"], authenticated=None, account_type="api_key",
                      permission_mode=public["permission_mode"])
        if not result["error"] and not public["configured"]:
            result["error"] = "Configure a local Claude API key; no API request has been made"
        with self._lock:
            self._health = result
        return result

    def snapshot(self, compact=False):
        result = super().snapshot(compact=compact)
        result["provider"] = "claude"
        result["agent_settings"] = self.agent_settings.snapshot()
        for session in result["sessions"] + result["deleted_sessions"]:
            session["provider"] = "claude"
        return result

    def _client(self, session):
        prior = self._clients.get(session["id"])
        if prior and prior.active:
            return prior
        if not self.executable:
            raise SessionError("Claude CLI is unavailable")
        if prior:
            prior.close()
        runtime = self.agent_settings.runtime()
        client = self.claude_client_factory(self.executable, session["worktree"],
                    lambda message: self._event(session["id"], message), settings=runtime,
                    config_dir=self.root / "cli" / session["id"])
        self._clients[session["id"]] = client
        method = "thread/resume" if session.get("thread_id") else "thread/start"
        result = client.request(method, {"threadId": session.get("thread_id")})
        with self._lock:
            session.update(thread_id=result["thread"]["id"], model=runtime["model"],
                           permission_mode=runtime["permission_mode"], claude_permission_denials=[])
            self._save(session)
        return client

    def _resume_denied(self, session_id):
        with self.machine_lock, self._lock:
            session = self._get(session_id)
            client = self._clients.get(session_id)
            if (session["status"] == "waiting_input" and session.get("claude_permission_denials")
                    and not (client and client.active)):
                session["status"] = "interrupted"
                self._save(session)

    def start(self, session_id):
        self._resume_denied(session_id)
        return super().start(session_id)

    def message(self, session_id, text):
        self._reject_secret(text)
        self._resume_denied(session_id)
        return super().message(session_id, text)

    def _event(self, session_id, message):
        super()._event(session_id, message)
        method, params = message.get("method"), message.get("params", {})
        with self._lock:
            session = self._sessions.get(session_id)
            if not session or session.get("deleted_at"):
                return
            if method == "claude/result":
                denials = params.get("permission_denials", [])
                session["claude_permission_denials"] = denials if isinstance(denials, list) else []
                self._save(session)
            elif method == "turn/completed" and session["status"] == "completed" and session.get("claude_permission_denials"):
                session.update(status="waiting_input", error="Claude 操作被工具权限拒绝。请核对本机 Claude 权限配置，随后继续原任务。")
                self._save(session)
