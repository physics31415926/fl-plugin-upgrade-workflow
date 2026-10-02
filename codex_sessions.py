"""Local Codex app-server sessions for backend adaptation.

Uses the documented JSONL app-server protocol. No desktop database access or
automatic approval is used. Machine profiles contain SSH aliases, never keys.
"""
from __future__ import annotations

import atexit
from collections import deque
from concurrent.futures import Future, TimeoutError as FutureTimeout
from datetime import datetime, timedelta, timezone
import json
import logging
import os
from pathlib import Path, PurePosixPath
import re
import shlex
import shutil
import subprocess
import threading
import time
import uuid


logger = logging.getLogger(__name__)


class SessionError(ValueError):
    pass


def now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def atomic_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix("." + uuid.uuid4().hex + ".tmp")
    try:
        tmp.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        # Windows readers (including antivirus/indexing) can briefly prevent
        # replacement. Other failures, and a lock that persists, still raise.
        for attempt in range(4):
            try:
                os.replace(tmp, path)
                break
            except OSError as exc:
                if getattr(exc, "winerror", None) not in {5, 32, 33} or attempt == 3:
                    raise
                time.sleep(0.025 * (2 ** attempt))
    finally:
        tmp.unlink(missing_ok=True)


def bounded(value):
    """Keep UI/event records bounded even when a tool emits a large result."""
    if isinstance(value, str):
        return value[:24000] + ("\n[truncated]" if len(value) > 24000 else "")
    if isinstance(value, dict):
        return {str(k): bounded(v) for k, v in list(value.items())[:150]}
    if isinstance(value, list):
        return [bounded(v) for v in value[:150]]
    return value


class AppServerClient:
    """One stdio connection. Reader never blocks on user approvals."""

    def __init__(self, executable, cwd, callback, timeout=20):
        self.callback = callback
        self.timeout = timeout
        self._lock = threading.Lock()
        self._pending = {}
        self._next_id = 0
        self._closed = False
        self._reader_stopped = False
        self.last_callback_error = None
        self.stderr = deque(maxlen=20)
        flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
        self.process = subprocess.Popen(
            [executable, "app-server", "--listen", "stdio://"], cwd=str(cwd),
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, encoding="utf-8", errors="replace", bufsize=1,
            creationflags=flags,
        )
        threading.Thread(target=self._read, daemon=True).start()
        threading.Thread(target=self._read_stderr, daemon=True).start()
        try:
            self.info = self.request("initialize", {
                "clientInfo": {"name": "fl_backend_dashboard", "title": "FL Backend Dashboard", "version": "0.2.0"},
                "capabilities": {"experimentalApi": True},
            })
            self.notify("initialized", {})
        except Exception:
            self.close()
            raise

    def _write(self, value):
        with self._lock:
            if self._closed or self._reader_stopped or self.process.poll() is not None:
                raise SessionError("Codex app-server connection is closed")
            try:
                self.process.stdin.write(json.dumps(value, ensure_ascii=False) + "\n")
                self.process.stdin.flush()
            except (BrokenPipeError, OSError) as exc:
                raise SessionError("Codex app-server connection was lost") from exc

    def request(self, method, params):
        with self._lock:
            self._next_id += 1
            request_id = self._next_id
            future = Future()
            self._pending[request_id] = future
        try:
            self._write({"id": request_id, "method": method, "params": params})
            return future.result(timeout=self.timeout)
        except FutureTimeout as exc:
            raise SessionError(f"Codex app-server timed out: {method}") from exc
        finally:
            with self._lock:
                self._pending.pop(request_id, None)

    def notify(self, method, params):
        self._write({"method": method, "params": params})

    def respond(self, request_id, result):
        self._write({"id": request_id, "result": result})

    def _read_stderr(self):
        for line in self.process.stderr:
            self.stderr.append(line.rstrip()[:2000])

    def _dispatch(self, message):
        """A notification consumer must not terminate the protocol reader."""
        try:
            self.callback(message)
        except Exception as exc:
            diagnostic = {"at": now(), "method": str(message.get("method", ""))[:200],
                          "exception_type": type(exc).__name__,
                          "detail": "Notification callback failed; protocol reader continued."}
            for field in ("errno", "winerror"):
                value = getattr(exc, field, None)
                if isinstance(value, int):
                    diagnostic[field] = value
            # Exception text may include a private path or command. Keep only
            # safe error metadata, including when the diagnostic callback fails.
            self.last_callback_error = diagnostic
            self.stderr.append(diagnostic["detail"] + " " + diagnostic["exception_type"])
            logger.warning("App-server callback failed (%s); reader continued", diagnostic["exception_type"])
            try:
                self.callback({"method": "connection/callbackError", "params": diagnostic})
            except Exception as report_exc:
                logger.warning("App-server callback diagnostic could not be recorded (%s)", type(report_exc).__name__)

    def _read(self):
        failure = None
        try:
            for line in self.process.stdout:
                try:
                    message = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if "method" not in message and "id" in message:
                    with self._lock:
                        future = self._pending.get(message["id"])
                    if future and not future.done():
                        if "error" in message:
                            future.set_exception(SessionError(str(message["error"].get("message", message["error"]))))
                        else:
                            future.set_result(message.get("result", {}))
                elif "method" in message:
                    self._dispatch(message)
        except Exception as exc:
            failure = {"reason": "reader_error", "exception_type": type(exc).__name__,
                       "detail": "Codex app-server reader failed (" + type(exc).__name__ + ")"}
        finally:
            with self._lock:
                self._reader_stopped = True
                pending = list(self._pending.values())
            if failure is None:
                exit_code = self.process.poll()
                if self._closed:
                    failure = {"reason": "closed", "detail": "Codex app-server connection was closed"}
                elif exit_code is not None:
                    failure = {"reason": "process_exit", "exit_code": exit_code,
                               "detail": "Codex app-server exited (code " + str(exit_code) + ")"}
                else:
                    failure = {"reason": "stdout_closed", "detail": "Codex app-server stdout closed while its process was still running"}
            for future in pending:
                if not future.done():
                    future.set_exception(SessionError(failure["detail"]))
            if not self._closed:
                self._dispatch({"method": "connection/closed", "params": failure})

    def close(self):
        self._closed = True
        if self.process.poll() is None:
            try:
                self.process.stdin.close()
                self.process.wait(timeout=3)
            except (OSError, subprocess.TimeoutExpired):
                self.process.terminate()
                try:
                    self.process.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    self.process.kill()


class CodexSessionManager:
    ACTIVE = {"starting", "running", "waiting_approval", "waiting_input", "interrupting"}
    BACKENDS = {"cuda", "ascend", "metax", "corex", "hygon", "musa", "enflame", "kunlunxin", "thead"}

    def __init__(self, config, base_dir, client_factory=AppServerClient):
        self.config = config
        self.base_dir = Path(base_dir).resolve()
        self.repo = self._path(config["repo"])
        self.remote = self._name(config.get("remote", "upstream"), "remote")
        self.default_branch = config.get("branch", "0.4.0-dev")
        self.root = self._path(config.get("sessions_dir", "runs/codex-sessions"))
        self.evidence_dir = self._path(config.get("evidence_dir", "runs/gate-evidence"))
        self.executable = config.get("codex_executable") or shutil.which("codex")
        self.backends = set(config.get("session_backends", self.BACKENDS))
        self.backend_task_policies = self._task_policies(config.get("backend_task_policies", {}))
        self.client_factory = client_factory
        self._lock = threading.RLock()
        self._clients = {}
        self._workers = {}
        self._sessions = {}
        self._profiles = {}
        self._saved_at = {}
        self._dirty_sessions = set()
        self._save_timers = {}
        self._event_backlogs = {}
        self._event_log_retry_at = {}
        self._closed = False
        self._health = None
        self.root.mkdir(parents=True, exist_ok=True)
        profile_file = self.root / "machines.json"
        if profile_file.exists():
            try:
                self._profiles = json.loads(profile_file.read_text(encoding="utf-8"))
            except (OSError, ValueError) as exc:
                raise SessionError(f"Cannot load machine profiles: {exc}") from exc
        for file in self.root.glob("*/session.json"):
            try:
                session = json.loads(file.read_text(encoding="utf-8"))
                if not re.fullmatch(r"[0-9a-f]{32}", session["id"]) or file.parent.name != session["id"]:
                    continue
                if session.get("status") in self.ACTIVE:
                    session.update(status="disconnected", error="Dashboard restarted; resume the persisted Codex thread.")
                session["pending_requests"] = []
                self._sessions[session["id"]] = session
            except (OSError, ValueError, KeyError):
                continue
        atexit.register(self.close)

    def _path(self, value):
        path = Path(value)
        return (path if path.is_absolute() else self.base_dir / path).resolve()

    @staticmethod
    def _name(value, field):
        if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,99}", value):
            raise SessionError(f"Invalid {field}; use a configured SSH alias or simple identifier")
        return value

    @staticmethod
    def _text(value, field, maximum=2000, required=False):
        if not isinstance(value, str) or len(value) > maximum or "\x00" in value:
            raise SessionError(f"Invalid {field}")
        value = value.strip()
        if required and not value:
            raise SessionError(f"{field} is required")
        return value

    def _ssh_connection(self, value):
        """Validate OpenSSH config values; private keys are only referenced by path."""
        if value in (None, {}):
            return None
        if not isinstance(value, dict):
            raise SessionError("ssh_connection must be an object")
        if set(value) - {"host", "user", "port", "identity_file", "jump_alias", "jump_host"}:
            raise SessionError("SSH connection accepts host, user, port, identity_file and jump_alias only; do not send credentials")
        host = self._text(value.get("host", ""), "SSH host", 253, True)
        user = self._text(value.get("user", ""), "SSH user", 200, True)
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:-]*", host):
            raise SessionError("Invalid SSH hostname or IP address")
        if not re.fullmatch(r"[A-Za-z0-9_][A-Za-z0-9_.@+-]*", user):
            raise SessionError("Invalid SSH username")
        port = value.get("port", 22)
        if isinstance(port, bool) or not re.fullmatch(r"[0-9]{1,5}", str(port)) or not 1 <= int(port) <= 65535:
            raise SessionError("SSH port must be between 1 and 65535")
        identity = self._text(value.get("identity_file", ""), "identity_file", 2000)
        if identity:
            if any(c in identity for c in '\r\n\x00"%$`'):
                raise SessionError("Invalid identity_file path")
            identity_path = Path(identity).expanduser()
            if not identity_path.is_absolute() or not identity_path.is_file():
                raise SessionError("identity_file must reference an existing absolute private-key file on this computer")
            identity = str(identity_path.resolve())
        jump = value.get("jump_alias") or value.get("jump_host") or ""
        if jump:
            jump = self._name(jump, "jump host alias")
        return {"host": host, "user": user, "port": int(port), "identity_file": identity, "jump_alias": jump}

    def _write_ssh_config(self, path, alias, connection):
        # The user config is included for existing jump aliases, without reading
        # it or copying any credential. All managed values precede defaults.
        lines = [f"Host {alias}", f"  HostName {connection['host']}", f"  User {connection['user']}",
                 f"  Port {connection['port']}", "  ServerAliveInterval 60", "  ServerAliveCountMax 3"]
        if connection["identity_file"]:
            lines += ['  IdentityFile "' + Path(connection["identity_file"]).as_posix() + '"', "  IdentitiesOnly yes"]
        if connection["jump_alias"]:
            lines += ["  ProxyJump " + connection["jump_alias"]]
        # A Host block must be reset before Include, or existing alias blocks
        # would inherit the managed host's HostName/User values.
        lines += ["Host *", "  BatchMode yes", "  StrictHostKeyChecking yes",
                  'Include "' + (Path.home() / ".ssh" / "config").as_posix() + '"', ""]
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix("." + uuid.uuid4().hex + ".tmp")
        try:
            temporary.write_text("\n".join(lines), encoding="utf-8")
            if os.name != "nt":
                temporary.chmod(0o600)
            os.replace(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)

    def _machine(self, payload, require_ready=True):
        role = payload.get("role", "compute")
        if role not in ("compute", "gateway", "development"):
            raise SessionError("Machine role must be compute, gateway or development")
        if require_ready and role != "compute":
            raise SessionError(f"A {role} machine cannot be used as a backend validation machine")
        backend = payload.get("backend", "")
        if role != "compute":
            backend = ""
        if backend == "nvidia":
            backend = "cuda"
        if not isinstance(backend, str) or (backend not in self.backends and (require_ready or backend != "")):
            raise SessionError("Choose a configured backend")
        connection = self._ssh_connection(payload.get("ssh_connection"))
        alias = self._name(payload.get("ssh_alias", ""), "SSH alias")
        workspace = self._text(payload.get("remote_workspace", ""), "remote_workspace", required=require_ready)
        if workspace and (not workspace.startswith("/") or any(c in workspace for c in "\r\n\x00")):
            raise SessionError("remote_workspace must be an absolute Linux path")
        models = payload.get("model_paths", {})
        if not isinstance(models, dict) or len(models) > 20:
            raise SessionError("model_paths must map model names to machine paths")
        models = {self._text(k, "model name", 200, True): self._text(v, "model path", 2000, True) for k, v in models.items()}
        return {"backend": backend, "ssh_alias": alias, "remote_workspace": workspace, "model_paths": models, "role": role,
                "ssh_connection": connection}

    def save_profile(self, payload):
        profile_id = self._name(payload.get("id") or uuid.uuid4().hex, "profile id")
        with self._lock:
            previous = self._profiles.get(profile_id, {})
            values = {**previous, **payload}
            if not values.get("ssh_alias") and values.get("ssh_connection"):
                values["ssh_alias"] = "flflow-" + profile_id[:16]
            profile = self._machine(values, require_ready=False)
            profile.update(id=profile_id, label=self._text(values.get("label") or profile["ssh_alias"], "label", 200, True))
            for field in ("hardware", "source", "backend_source", "notes"):
                profile[field] = self._text(values.get(field, ""), field, 2000)
            if profile["ssh_connection"]:
                config_path = self.root / "ssh" / (profile_id + ".conf")
                self._write_ssh_config(config_path, profile["ssh_alias"], profile["ssh_connection"])
                profile["ssh_config_path"] = str(config_path)
            same_connection = all(previous.get(field) == profile.get(field) for field in ("ssh_alias", "ssh_connection"))
            same_readiness = same_connection and all(previous.get(field) == profile.get(field)
                for field in ("backend", "role", "remote_workspace", "model_paths"))
            profile["last_check"] = previous.get("last_check") if same_readiness else None
            if "last_connection_check" in previous:
                profile["last_connection_check"] = previous["last_connection_check"] if same_connection else None
            self._profiles[profile_id] = profile
            atomic_json(self.root / "machines.json", self._profiles)
        return dict(profile)

    def _check_connection(self, machine):
        """Run a short, read-only SSH probe. Never return raw remote stderr."""
        result = {"ok": False, "checked_at": now(), "ssh_alias": machine["ssh_alias"],
                  "profile_id": machine.get("id") or machine.get("profile_id"), "checks": [], "remote": {}, "error": None}

        def check(name, status, detail):
            result["checks"].append({"name": name, "status": status, "detail": detail})

        executable = shutil.which("ssh")
        if not executable:
            result["error"] = "未找到 OpenSSH 客户端，请先安装 ssh。"
            check("ssh", "failed", result["error"])
            return result
        arguments = [executable]
        if machine.get("ssh_config_path"):
            config_path = Path(machine["ssh_config_path"]).resolve()
            if not config_path.is_relative_to(self.root) or not config_path.is_file():
                raise SessionError("Managed SSH configuration is missing; save the machine profile again")
            arguments += ["-F", str(config_path)]
        arguments += ["-o", "BatchMode=yes", "-o", "ConnectTimeout=8", "-o", "ConnectionAttempts=1",
                      "-o", "ServerAliveInterval=5", "-o", "ServerAliveCountMax=1", "-o", "StrictHostKeyChecking=yes",
                      "-o", "ClearAllForwardings=yes", "-o", "ForwardAgent=no", "-o", "ForwardX11=no", machine["ssh_alias"]]
        result["terminal_command_argv"] = [executable]
        if machine.get("ssh_config_path"):
            result["terminal_command_argv"] += ["-F", machine["ssh_config_path"]]
        result["terminal_command_argv"] += ["-o", "StrictHostKeyChecking=ask", "-o", "BatchMode=no", machine["ssh_alias"]]
        paths = [machine["remote_workspace"], *machine["model_paths"].values()]
        # SSH passes its final argument through a remote shell. Each variable
        # is quoted once for that shell; the actual probe is constant stdin.
        arguments.append("sh -s -- " + " ".join(shlex.quote(path) for path in paths))
        script = """printf 'FLFLOW_PROBE_READY=1\\n'
workspace=$1
shift
if [ ! -d "$workspace" ]; then printf 'WORKSPACE=missing\\n'; exit 0; fi
printf 'WORKSPACE=present\\n'
if ! git -C "$workspace" rev-parse --is-inside-work-tree >/dev/null 2>&1; then
  printf 'REPOSITORY=missing\\n'
else
  printf 'REPOSITORY=present\\n'
  printf 'COMMIT=%s\\n' "$(git -C "$workspace" rev-parse HEAD 2>/dev/null)"
  printf 'BRANCH=%s\\n' "$(git -C "$workspace" symbolic-ref --short -q HEAD 2>/dev/null)"
  if [ -n "$(git -C "$workspace" status --porcelain 2>/dev/null)" ]; then printf 'DIRTY=yes\\n'; else printf 'DIRTY=no\\n'; fi
fi
if [ -d "$workspace/tools/adaptation-gate-cases" ]; then printf 'GATE=present\\n'; else printf 'GATE=missing\\n'; fi
if command -v tmux >/dev/null 2>&1; then printf 'TMUX=present\\n'; else printf 'TMUX=missing\\n'; fi
index=0
for model in "$@"; do
  if [ -e "$model" ]; then printf 'MODEL_%s=present\\n' "$index"; else printf 'MODEL_%s=missing\\n' "$index"; fi
  index=$((index + 1))
done
"""
        try:
            # Windows TextIOWrapper translates LF to CRLF when communicate()
            # writes text stdin. A POSIX shell must receive the original LF
            # bytes, otherwise commands such as shift and if gain a trailing CR.
            probe = subprocess.run(arguments, input=script.encode("utf-8"), capture_output=True, timeout=20,
                                   creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
        except subprocess.TimeoutExpired:
            result["error"] = "SSH 连接检查超时，请检查网络、入口端口和跳板机。"
            check("ssh", "failed", result["error"])
            return result
        except OSError:
            result["error"] = "无法运行 SSH 客户端，请检查本机安装。"
            check("ssh", "failed", result["error"])
            return result
        stdout = probe.stdout.decode("utf-8", errors="replace") if isinstance(probe.stdout, bytes) else probe.stdout
        stderr = probe.stderr.decode("utf-8", errors="replace") if isinstance(probe.stderr, bytes) else probe.stderr
        remote_started = "FLFLOW_PROBE_READY=1" in stdout.splitlines()
        if probe.returncode and remote_started:
            check("ssh", "passed", "已通过 SSH 连接并执行远端只读预检。")
            result["error"] = "SSH 已连通，但远端只读预检执行失败（退出码 " + str(probe.returncode) + "）；请检查远端 shell 和命令执行策略。"
            check("environment_probe", "failed", result["error"])
            return result
        if probe.returncode:
            error = stderr.lower()
            if "host key verification failed" in error or "authenticity of host" in error or "no host key is known" in error:
                detail = "主机尚未信任或主机密钥已变更。请在本机终端确认主机指纹并完成一次 SSH 登录，然后重试。"
            elif any(value in error for value in ("permission denied", "authentication failed", "no more authentication methods")):
                detail = "SSH 认证失败或需要交互密码。请在本机配置可用的密钥或 ssh-agent；网页不会保存密码。"
            elif "timed out" in error or "no route to host" in error:
                detail = "SSH 网络不可达或连接超时，请检查开发机、VPN、端口和跳板机。"
            elif "could not resolve hostname" in error:
                detail = "SSH 别名或主机名无法解析，请检查本机 SSH 配置。"
            else:
                detail = "SSH 连接检查失败，请在本机终端使用同一配置检查连接。"
            result["error"] = detail
            check("ssh", "failed", detail)
            return result
        if not remote_started:
            result["error"] = "未收到远端预检启动标记。请确认 SSH 入口支持非交互命令，且没有 RemoteCommand 替换预检命令；仅支持交互终端的入口需配置可执行命令的连接。"
            check("environment_probe", "failed", result["error"])
            return result
        values = {}
        for line in stdout[:12000].splitlines():
            key, separator, value = line.partition("=")
            if separator and re.fullmatch(r"WORKSPACE|REPOSITORY|COMMIT|BRANCH|DIRTY|GATE|TMUX|MODEL_[0-9]+", key):
                values[key] = value[:200]
        check("ssh", "passed", "已通过非交互 SSH 认证。")
        workspace_ok = values.get("WORKSPACE") == "present"
        check("workspace", "passed" if workspace_ok else "failed", "远端工作目录存在。" if workspace_ok else "远端工作目录不存在，请填写已存在的绝对目录。")
        if not workspace_ok:
            result["error"] = result["checks"][-1]["detail"]
            return result
        repository_ok = values.get("REPOSITORY") == "present" and re.fullmatch(r"[0-9a-f]{40}", values.get("COMMIT", "")) is not None
        tmux_ok = values.get("TMUX") == "present"
        gate_ok = values.get("GATE") == "present"
        dirty = values.get("DIRTY") == "yes"
        result["remote"] = {"workspace": machine["remote_workspace"], "git_branch": values.get("BRANCH") or None,
                            "git_commit": values.get("COMMIT") if repository_ok else None, "git_dirty": dirty,
                            "gate_cases": gate_ok, "tmux": tmux_ok}
        check("repository", "passed" if repository_ok else "failed",
              ("Git 仓库已确认；现有未提交修改将由 agent 保留。" if dirty else "Git 仓库已确认，工作区干净。") if repository_ok else "该目录不是可读取 HEAD 的 Git 仓库。")
        check("gate_cases", "passed" if gate_ok else "warning", "原仓库验收 case 目录存在。" if gate_ok else "远端尚无验收 case，agent 需从所选基线复制原始 case 后再验收。")
        check("tmux", "passed" if tmux_ok else "failed", "tmux 可用，长任务可持续运行。" if tmux_ok else "远端缺少 tmux，请先安装后再下发长任务。")
        models_ok = True
        for index, name in enumerate(machine["model_paths"]):
            present = values.get("MODEL_" + str(index)) == "present"
            models_ok = models_ok and present
            check("model:" + name, "passed" if present else "failed", "模型路径存在。" if present else "配置的模型路径不存在，请修正后再启动。")
        result["ok"] = bool(repository_ok and tmux_ok and models_ok)
        if not result["ok"]:
            result["error"] = "；".join(c["detail"] for c in result["checks"] if c["status"] == "failed")
        return result

    def check_machine(self, payload):
        values = dict(payload)
        profile_id = values.get("profile_id")
        with self._lock:
            profile = self._profiles.get(profile_id) if profile_id else None
            if profile_id and not profile:
                raise SessionError("Unknown machine profile")
            if profile:
                values = {**profile, **values}
            machine = self._machine(values)
            if machine["ssh_connection"]:
                if not profile or machine["ssh_connection"] != profile.get("ssh_connection") or machine["ssh_alias"] != profile["ssh_alias"]:
                    raise SessionError("Save this SSH connection before checking or starting it")
                machine["ssh_config_path"] = profile["ssh_config_path"]
            machine["profile_id"] = profile_id
        result = self._check_connection(machine)
        if profile_id:
            with self._lock:
                self._profiles[profile_id]["last_check"] = result
                atomic_json(self.root / "machines.json", self._profiles)
        return result

    def _git(self, *args):
        try:
            result = subprocess.run(["git", "-c", "safe.directory=" + self.repo.as_posix(), "-C", str(self.repo), *args],
                                    capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=45)
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise SessionError(f"Git failed: {exc}") from exc
        if result.returncode:
            raise SessionError(result.stderr.strip()[:3000] or "Git command failed")
        return result.stdout.strip()

    def _save(self, session):
        session_id = session["id"]
        self._dirty_sessions.add(session_id)
        session["updated_at"] = now()
        previous_error = session.pop("persistence_error", None)
        try:
            atomic_json(self.root / session_id / "session.json", session)
        except OSError as exc:
            diagnostic = {"at": now(), "exception_type": type(exc).__name__,
                          "attempts": (previous_error or {}).get("attempts", 0) + 1,
                          "detail": "Session state could not be saved; retrying."}
            for field in ("errno", "winerror"):
                value = getattr(exc, field, None)
                if isinstance(value, int):
                    diagnostic[field] = value
            session["persistence_error"] = diagnostic
            if previous_error is None:
                logger.warning("Session state persistence failed (%s); save will be retried", type(exc).__name__)
            self._schedule_save(session_id, 1)
            raise
        self._saved_at[session_id] = time.monotonic()
        self._dirty_sessions.discard(session_id)
        timer = self._save_timers.pop(session_id, None)
        if timer:
            timer.cancel()
        if previous_error:
            logger.info("Session state persistence recovered")
        if self._event_backlogs.get(session_id):
            self._dirty_sessions.add(session_id)
            self._schedule_save(session_id, 1)

    def _schedule_save(self, session_id, delay):
        if self._closed or session_id in self._save_timers:
            return
        timer = threading.Timer(max(0.01, delay), lambda: self._flush_session(session_id, timer))
        timer.daemon = True
        self._save_timers[session_id] = timer
        timer.start()

    def _flush_session(self, session_id, expected_timer=None):
        with self._lock:
            if expected_timer is not None and self._save_timers.get(session_id) is not expected_timer:
                return
            timer = self._save_timers.pop(session_id, None)
            if timer:
                timer.cancel()
            if self._closed or session_id not in self._dirty_sessions:
                return
            session = self._sessions.get(session_id)
            if session:
                self._flush_event_log(session, force=True)
                try:
                    self._save(session)
                except OSError:
                    # _save records the failure and schedules another attempt.
                    pass

    def _flush_event_log(self, session, force=False):
        backlog = self._event_backlogs.get(session["id"])
        if not backlog:
            return
        if not force and time.monotonic() < self._event_log_retry_at.get(session["id"], 0):
            return
        try:
            path = self.root / session["id"] / "events.jsonl"
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("a", encoding="utf-8") as file:
                file.write("".join(json.dumps(record, ensure_ascii=False) + "\n" for record in backlog))
        except OSError as exc:
            if not session.get("event_log_error"):
                logger.warning("Session event log persistence failed (%s); save will be retried", type(exc).__name__)
            session["event_log_error"] = {"at": now(), "exception_type": type(exc).__name__,
                                          "detail": "Event log could not be saved; retrying retained records."}
            self._event_log_retry_at[session["id"]] = time.monotonic() + 1
        else:
            backlog.clear()
            session.pop("event_log_error", None)
            self._event_log_retry_at.pop(session["id"], None)

    def _save_event(self, session, method):
        session_id = session["id"]
        self._dirty_sessions.add(session_id)
        elapsed = time.monotonic() - self._saved_at.get(session_id, 0)
        if method.endswith(("/delta", "/outputDelta")) and elapsed < 1:
            self._schedule_save(session_id, 1 - elapsed)
            return
        try:
            self._save(session)
        except OSError:
            # Keep lifecycle/approval state in memory and expose the failure;
            # retrying persistence must not close an active protocol connection.
            pass

    def _get(self, session_id):
        if session_id not in self._sessions:
            raise SessionError("Unknown session")
        return self._sessions[session_id]

    def _task_policies(self, value):
        if not isinstance(value, dict):
            raise SessionError("backend_task_policies must map backend names to policies")
        policies = {}
        for backend, policy in value.items():
            backend = "cuda" if backend == "nvidia" else backend
            if backend not in self.backends or backend in policies or not isinstance(policy, dict):
                raise SessionError("Each task policy must identify one configured backend")
            if not isinstance(policy.get("mode"), str) or policy["mode"] not in {"monitor", "adapt"}:
                raise SessionError("Task policy mode must be monitor or adapt")
            branch = self._text(policy.get("branch", self.default_branch), "policy branch", 200, True)
            if branch.startswith("-") or any(c.isspace() for c in branch):
                raise SessionError("Invalid task policy branch")
            pr = policy.get("pr")
            if pr is not None and (type(pr) is not int or pr <= 0):
                raise SessionError("Task policy PR must be a positive integer")
            repository = self.config.get("github_repo")
            if pr is not None and repository:
                if (not isinstance(repository, str) or not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repository)
                        or any(part in {".", ".."} for part in repository.split("/"))):
                    raise SessionError("github_repo must be an owner/repo without URL or path escapes")
            policies[backend] = {"mode": policy["mode"], "branch": branch, "pr": pr,
                                 "pr_url": f"https://github.com/{repository}/pull/{pr}" if pr is not None and repository else None,
                                 "reason": self._text(policy.get("reason", ""), "policy reason", 2000)}
        return policies

    def _task_policy(self, backend, branch):
        policy = self.backend_task_policies.get(backend)
        if policy and policy["branch"] == branch:
            return dict(policy)
        return {"mode": "adapt", "branch": branch, "pr": None, "pr_url": None, "reason": ""}

    def _require_adapt(self, backend, branch):
        policy = self._task_policy(backend, branch)
        if policy["mode"] == "monitor":
            context = (f" PR #{policy['pr']}." if policy["pr"] else "")
            reason = (" " + policy["reason"]) if policy["reason"] else ""
            raise SessionError("This backend is monitor-only; active adaptation is disabled." + context + reason)

    def _require_new_session(self, backend, branch):
        for existing in self._sessions.values():
            if existing["backend"] == backend and existing["branch"] == branch:
                # A completed model turn does not prove the adaptation or Gate
                # finished. Keep ownership even when the machine or HEAD changes.
                raise SessionError(f"Use existing session {existing['id']} for this backend and baseline branch")

    def _session_view(self, session):
        view = {**session, "task_policy": self._task_policy(session["backend"], session["branch"])}
        return json.loads(json.dumps(view))

    def snapshot(self):
        with self._lock:
            for session in self._sessions.values():
                self._refresh_progress_artifact(session)
            return json.loads(json.dumps({
                "available": bool(self.executable), "default_branch": self.default_branch,
                "backends": sorted(self.backends), "health": self._health,
                "backend_task_policies": self.backend_task_policies,
                "machine_profiles": list(self._profiles.values()),
                "sessions": [self._session_view(s) for s in sorted(self._sessions.values(), key=lambda s: s["created_at"], reverse=True)],
            }))

    def _refresh_progress_artifact(self, session):
        """Read bounded progress from this session's isolated worktree only."""
        dirty = False

        def reject(code):
            nonlocal dirty
            # Fixed reasons only: never expose rejected content, paths or OS errors.
            warning = {"code": code}
            if session.get("progress_warning") != warning:
                session["progress_warning"] = warning
                dirty = True

        try:
            worktree = Path(session["worktree"]).resolve(strict=True)
            expected = (self.root / "worktrees" / session["id"]).resolve(strict=True)
            workspace_root = (self.root / "worktrees").resolve(strict=True)
            if worktree != expected or not worktree.is_relative_to(workspace_root):
                reject("artifact_unsafe_path")
                return
            candidate = worktree / ".flflow-progress.json"
            path = candidate.resolve(strict=True)
            if not path.is_relative_to(worktree) or not path.is_file():
                reject("artifact_unsafe_path")
                return
            before = path.stat()
            if before.st_size > 65536:
                reject("artifact_too_large")
                return
            with path.open("rb") as file:
                content = file.read(65537)
            after = path.stat()
            if len(content) > 65536:
                reject("artifact_too_large")
                return
            if (before.st_mtime_ns, before.st_size) != (after.st_mtime_ns, after.st_size):
                reject("artifact_changed")
                return
            artifact = json.loads(content.decode("utf-8"))
            if not isinstance(artifact, dict) or type(artifact.get("schema_version")) is not int or artifact["schema_version"] != 1:
                reject("artifact_invalid")
                return
            if artifact.get("session_id") != session["id"] or artifact.get("backend") != session["backend"]:
                reject("artifact_invalid")
                return
            plan = artifact.get("plan")
            if not isinstance(plan, list) or not 1 <= len(plan) <= 30:
                reject("artifact_invalid")
                return
            normalized = []
            for item in plan:
                if not isinstance(item, dict) or not isinstance(item.get("step"), str) or not 1 <= len(item["step"].strip()) <= 1000:
                    reject("artifact_invalid")
                    return
                if "\x00" in item["step"] or item.get("status") not in {"pending", "in_progress", "completed"}:
                    reject("artifact_invalid")
                    return
                item["step"].encode("utf-8")
                normalized.append({"step": item["step"].strip(), "status": item["status"]})
            summary = artifact.get("summary")
            if summary is not None and (not isinstance(summary, str) or len(summary) > 24000 or "\x00" in summary):
                reject("artifact_invalid")
                return
            if summary is not None:
                summary.encode("utf-8")
            # Reject the entire invalid progress record before accepting its
            # identity report. Valid reports keep their own observation clock,
            # independently of plan notifications or the file's mtime.
            dirty = self._refresh_reported_provenance(session, artifact.get("provenance"), worktree)
            if session.pop("progress_warning", None) is not None:
                dirty = True
            artifact_time = after.st_mtime
            current_time = session.get("plan_updated_at")
            if current_time:
                try:
                    recorded_time = datetime.fromisoformat(current_time.replace("Z", "+00:00")).timestamp()
                except (ValueError, TypeError):
                    recorded_time = 0
                if session.get("plan_source") != "artifact" and artifact_time <= recorded_time:
                    return
                if session.get("plan_source") == "artifact" and artifact_time < recorded_time:
                    return
            stamp = datetime.fromtimestamp(artifact_time, timezone.utc).isoformat(timespec="microseconds")
            changed = session.get("plan") != normalized or session.get("plan_source") != "artifact" or session.get("plan_updated_at") != stamp
            if session.get("progress_summary") != summary:
                changed = True
            if not changed:
                return
            session.update(plan=normalized, plan_source="artifact", plan_updated_at=stamp,
                           progress_artifact=".flflow-progress.json")
            session["progress_summary"] = summary
            dirty = True
        except FileNotFoundError:
            # A progress file is optional before the first artifact arrives.
            if session.get("progress_artifact") or session.get("progress_warning"):
                reject("artifact_unavailable")
        except OSError:
            reject("artifact_unavailable")
        except (ValueError, TypeError, KeyError, OverflowError, RuntimeError):
            reject("artifact_invalid")
        finally:
            if dirty:
                try:
                    self._save(session)
                except OSError:
                    # _save exposes the persistence error and schedules retry.
                    pass

    def _refresh_reported_provenance(self, session, value, worktree):
        """Accept bounded agent reports, without executing or verifying them."""
        try:
            if not isinstance(value, dict) or value.get("branch") != session["branch"]:
                return False

            def text(value, maximum):
                if (not isinstance(value, str) or not 1 <= len(value) <= maximum
                        or any(ord(c) < 32 or 127 <= ord(c) <= 159 for c in value)):
                    raise ValueError("Invalid provenance text")
                value.encode("utf-8")
                return value

            candidate = text(value.get("candidate_commit"), 40)
            if not re.fullmatch(r"[0-9a-fA-F]{40}", candidate):
                return False
            remote = text(value.get("remote_task_workspace"), 2000)
            if (not remote.startswith("/") or remote.startswith("//") or remote == "/" or "\\" in remote
                    or ".." in remote.split("/")):
                return False
            observed_at = text(value.get("observed_at"), 80)
            if not re.fullmatch(
                    r"[0-9]{4}-(0[1-9]|1[0-2])-(0[1-9]|[12][0-9]|3[01])"
                    r"T([01][0-9]|2[0-3]):[0-5][0-9]:[0-5][0-9]"
                    r"(?:\.[0-9]{1,9})?(?:Z|[+-](?:[01][0-9]|2[0-3]):[0-5][0-9])", observed_at):
                return False
            observed = datetime.fromisoformat(observed_at.replace("Z", "+00:00"))
            if observed.utcoffset() is None or observed > datetime.now(timezone.utc) + timedelta(minutes=5):
                return False
            references = value.get("evidence_paths")
            if not isinstance(references, list) or len(references) > 20:
                return False
            evidence_paths = []
            for reference in references:
                if not isinstance(reference, dict):
                    return False
                relative = text(reference.get("path"), 512)
                portable = PurePosixPath(relative)
                if (portable.is_absolute() or "\\" in relative or ":" in relative
                        or any(part in {"", ".", ".."} for part in relative.split("/"))):
                    return False
                target = (worktree / relative).resolve(strict=True)
                if not target.is_relative_to(worktree) or not target.is_file():
                    return False
                record = {"path": relative}
                if "sha256" in reference:
                    digest = text(reference["sha256"], 64)
                    if not re.fullmatch(r"[0-9a-fA-F]{64}", digest):
                        return False
                    record["sha256"] = digest.lower()
                evidence_paths.append(record)
            report = {"branch": session["branch"], "candidate_commit": candidate.lower(),
                      "remote_task_workspace": remote, "observed_at": observed_at,
                      "evidence_paths": evidence_paths, "source": "agent_report"}

            def observation_key(stamp, parsed):
                # datetime truncates fractions after six digits. Keep all nine
                # accepted digits when ordering reports within one microsecond.
                fraction = re.search(r"\.([0-9]{1,9})(?:Z|[+-][0-9]{2}:[0-9]{2})$", stamp)
                nanoseconds = int(fraction.group(1).ljust(9, "0")) if fraction else parsed.microsecond * 1000
                return parsed.replace(microsecond=0), nanoseconds

            current = session.get("reported_provenance")
            if isinstance(current, dict):
                previous = datetime.fromisoformat(current["observed_at"].replace("Z", "+00:00"))
                if previous.utcoffset() is not None and observation_key(current["observed_at"], previous) > observation_key(observed_at, observed):
                    return False
            if current == report:
                return False
            session["reported_provenance"] = report
            return True
        except (OSError, ValueError, TypeError, KeyError, OverflowError, RuntimeError):
            # Missing, partial, invalid or older reports retain the last accepted
            # record and its observation time; no turn/plan/Gate fields change.
            return False

    @staticmethod
    def _same_machine(left, right):
        if left["ssh_alias"] == right["ssh_alias"]:
            return True
        left_connection, right_connection = left.get("ssh_connection"), right.get("ssh_connection")
        if not left_connection or not right_connection:
            return False
        return (left_connection["host"].lower(), left_connection["user"], left_connection["port"]) == (
            right_connection["host"].lower(), right_connection["user"], right_connection["port"])

    def create(self, payload):
        """Prepare only; callers explicitly invoke start to begin model work."""
        profile = None
        if payload.get("profile_id"):
            profile = self._profiles.get(payload["profile_id"])
            if not profile:
                raise SessionError("Unknown machine profile")
            if profile.get("role", "compute") != "compute":
                raise SessionError(f"A {profile['role']} machine cannot be used as a backend validation machine")
            payload = {**profile, **payload}
        machine = self._machine(payload)
        branch = self._text(payload.get("branch", self.default_branch), "branch", 200, True)
        if branch.startswith("-") or any(c.isspace() for c in branch):
            raise SessionError("Invalid branch")
        with self._lock:
            self._require_new_session(machine["backend"], branch)
            self._require_adapt(machine["backend"], branch)
        if machine["ssh_connection"]:
            if not profile or machine["ssh_connection"] != profile.get("ssh_connection") or machine["ssh_alias"] != profile["ssh_alias"]:
                raise SessionError("Save this SSH connection before creating a task")
            machine["ssh_config_path"] = profile["ssh_config_path"]
        preflight_required = bool(payload.get("require_preflight", False) or machine["ssh_connection"])
        preflight = self._check_connection(machine) if preflight_required else None
        if preflight and not preflight["ok"]:
            raise SessionError("连接预检未通过：" + preflight["error"])
        for profile in self._profiles.values():
            if profile["ssh_alias"] == machine["ssh_alias"] and profile.get("role", "compute") != "compute":
                raise SessionError(f"A {profile['role']} machine cannot be used as a backend validation machine")
        self._git("check-ref-format", "refs/heads/" + branch)
        ref = f"refs/remotes/{self.remote}/{branch}"
        try:
            commit = self._git("rev-parse", "--verify", ref + "^{commit}")
        except SessionError:
            ref = "refs/heads/" + branch
            commit = self._git("rev-parse", "--verify", ref + "^{commit}")
        if not re.fullmatch(r"[0-9a-f]{40}", commit):
            raise SessionError("Branch did not resolve to one Git commit")
        session_id = uuid.uuid4().hex
        worktree = self.root / "worktrees" / session_id
        worktree.parent.mkdir(parents=True, exist_ok=True)
        work_branch = f"codex/backend-{machine['backend']}-{session_id[:10]}"
        goal = self._text(payload.get("goal", ""), "goal", 6000)
        session = {
            "id": session_id, **machine, "branch": branch, "ref": ref, "commit": commit,
            "worktree": str(worktree), "work_branch": work_branch, "goal": goal,
            "status": "prepared", "created_at": now(), "updated_at": now(),
            "thread_id": None, "turn_id": None, "pending_requests": [], "events": [], "error": None,
            "preflight_required": preflight_required, "preflight": preflight, "profile_id": payload.get("profile_id"),
            "plan": [], "last_agent_message": None,
        }
        with self._lock:
            self._require_new_session(machine["backend"], branch)
            self._require_adapt(machine["backend"], branch)
            # This lease covers active/prepared Codex sessions only. Remote tmux
            # processes can outlive a completed/interrupted local Codex turn.
            for existing in self._sessions.values():
                if self._same_machine(existing, machine) and existing["status"] in self.ACTIVE | {"prepared"}:
                    raise SessionError("This machine already has an active or prepared session; finish or interrupt it first")
            if machine["ssh_connection"]:
                config_path = self.root / session_id / "ssh.conf"
                self._write_ssh_config(config_path, machine["ssh_alias"], machine["ssh_connection"])
                session["ssh_config_path"] = str(config_path)
            self._git("worktree", "add", "-b", work_branch, str(worktree), commit)
            session["prompt"] = self._prompt(session)
            self._sessions[session_id] = session
            self._save(session)
            return self._session_view(session)

    def _prompt(self, session):
        evidence_root = self.evidence_dir / session["backend"] / session["id"]
        manifest_example = {
            "schema_version": 1, "run_id": session["id"] + "-<model>-<mode>",
            "backend": session["backend"], "branch": session["branch"],
            "commit": "<actual-tested-40-hex-commit>", "model": "<configured-model-name>",
            "mode": "<eager-or-graph>", "port": 8000, "results_dir": "results",
            "created_at": "<actual-run-UTC-time>",
        }
        progress_example = {"schema_version": 1, "session_id": session["id"], "backend": session["backend"],
                            "plan": [{"step": "环境检查", "status": "in_progress"},
                                     {"step": "版本适配", "status": "pending"},
                                     {"step": "服务启动", "status": "pending"},
                                     {"step": "Gate 验收", "status": "pending"},
                                     {"step": "结果归档", "status": "pending"}],
                            "summary": "填写当前真实进展、阻塞和下一步。"}
        ssh_arguments = ["ssh"]
        if session.get("ssh_config_path"):
            ssh_arguments += ["-F", session["ssh_config_path"]]
        ssh_arguments.append(session["ssh_alias"])
        ssh_instruction = ("本任务所有 SSH 连接必须使用此 argv，禁止 shell=True；远端命令中的目录和参数必须正确引用："
                           + json.dumps(ssh_arguments, ensure_ascii=False) + "。")
        return (
            "请跟进 vllm-plugin-FL 的 backend 适配，持续记录实现、测试和阻塞情况。\n"
            f"Backend: {session['backend']}\nBase branch: {session['branch']}\nBase commit: {session['commit']}\n"
            f"Local isolated worktree: {session['worktree']}\nWork branch: {session['work_branch']}\n"
            f"SSH host alias: {session['ssh_alias']}\nRemote workspace: {session['remote_workspace']}\n"
            f"Model paths: {json.dumps(session['model_paths'], ensure_ascii=False)}\n"
            f"Additional user objective: {session['goal'] or '检查当前适配，完成该 backend 的升级和验收。'}\n\n"
            f"{ssh_instruction}\n"
            "先调用 update_plan 制定环境检查、版本适配、服务启动、Gate 验收、结果归档五个阶段，"
            "随实际执行更新每项 pending/in_progress/completed；遇到阻塞保留未完成项，并说明需要的用户操作。"
            "若当前会话没有 update_plan 工具，必须在本机隔离 worktree 根目录持续维护 .flflow-progress.json，"
            "每阶段有真实变化就原子写入；文件不得超过 64 KiB，不得链接到 worktree 外。"
            "进度 JSON 只保留当前计划、简短摘要与 provenance 引用，建议最终 UTF-8 字节数不超过 32 KiB。"
            "详细诊断、原始结果、完整元数据和历史记录单独保存于本任务私有证据文件，使用 evidence_paths 短引用。"
            "原子替换前计算包含缩进和编码后的实际 UTF-8 字节数；超限时先拆分和精简摘要，保留全部原证据。"
            "只写真实阶段，不根据轮次结束或 open PR 将验收标为完成。使用以下格式：\n"
            + json.dumps(progress_example, ensure_ascii=False, indent=2) + "\n"
            "有可复核的候选身份记录后，可在进度文件增加 provenance 对象：branch 必须等于本任务基线分支，"
            "candidate_commit 为实际候选完整40位SHA，remote_task_workspace 为实际远端独立任务绝对目录，"
            "observed_at 为真实 RFC3339 时间（T 分隔，显式 Z 或 ±HH:MM 时区），evidence_paths 为最多20项本 worktree 内已有文件的相对路径引用"
            "（每项 path 最多512字符、/分隔且无空、.或..组件，可选 sha256）。摘要更新时间不能代替证据时间；引用哈希仅上报，服务不会验证其内容或据此判定Gate。\n"
            "先读取仓库 AGENTS.md 和相关 vllm-upgrade-flagos / vllm-plugin-fl-setup-flagos 技能，确认版本和 backend。"
            "For source byte-length/SHA pins across Windows and Linux, use the fixed commit Git blob raw bytes "
            "(git show <commit>:<path>), since working-tree copies can convert line endings. Independently verify "
            "the actual remote provider bytes and identity; never silently normalize differences to pass a guard. "
            "A single matching file does not refresh the complete source observation clock.\n"
            "先只读检查指定机器、硬件、远程目录 git 状态和模型路径；在独立工作分支/目录完成变更，保留现有修改。"
            "SSH 使用上述保存的配置和别名；不读取、复制或输出私钥内容，不在配置、命令或日志中写密码/token。"
            "不得禁用 SSH 主机密钥验证；首次信任或认证需要人工处理时暂停并在面板报告。"
            "不要更改全局代理；如已有 git 代理只用于 git，不能给 pip/curl 设置全局 HTTP(S)_PROXY。"
            "远程长任务（安装、下载、服务、测试）必须用唯一命名 tmux 会话并保存日志，避免 SSH 中断。"
            "每阶段报告：环境检查、版本适配、服务启动、Gate 验收、结果收集。"
            "验收用所选分支 tools/adaptation-gate-cases 的真实 case；不得用 pytest 退出码代替结果 JSON 的 summary.passed 和逐请求 checks。"
            "记录实际测试的 commit、backend、模型、eager/graph、机器、命令、时间、原始结果路径；报告失败和未执行项。"
            f"\n面板配置模型：{json.dumps(self.config.get('models', ['Qwen3.6-27B', 'Qwen3.6-35B-A3B']), ensure_ascii=False)}；"
            f"模式：{json.dumps(self.config.get('modes', ['eager', 'graph']), ensure_ascii=False)}。\n"
            f"Gate 归档根目录（本机）：{evidence_root}\n"
            "每个模型/模式独立创建 <model>/<mode>/manifest.json 和同级 results/ 目录；"
            "manifest 使用以下格式并填写真实运行值：\n"
            + json.dumps(manifest_example, ensure_ascii=False, indent=2) + "\n"
            "从验证机器原样复制 tools/adaptation-gate-cases 生成的各 <scenario>.json 到 results/，保留原始时间、输入、输出、checks 和 summary；"
            "保留失败结果，不得重写结果为通过。manifest 的 model 必须与面板模型名及原始 JSON 的 model_path 文件名或 served_model_name 相符；"
            "port 必须是该次验证的真实端口。results_dir 相对 manifest 且位于面板 evidence_dir 内。"
            "commit 填写实际被测试的干净代码提交；有未提交修改时先提交隔离工作分支再验证，不能冒用基线 SHA。"
            "候选工作分支 commit 与监控分支 HEAD 不同会显示证据过期，须如实报告，不能修改 manifest 伪装成 HEAD。"
            "写归档目录若需要沙盒权限则提出对应审批；无法写入时先保留在本 worktree 并报告可导入路径。"
            "Codex 轮次完成只表示本轮执行结束，绝不等同于 backend 或 Gate 验收通过。\n"
            "在用户指定机器及隔离工作目录持续完成常规适配、修复、测试、重试和证据归档，直到任务完成或确有阻塞；"
            "无需为已授权的常规实现步骤重复确认，审批请求由面板交给用户处理。"
            "最后检查关联 PR/CI 状态并给出候选改动、验收结果和仍待合入的内容，不能把 open PR 或轮次结束视为已通过。"
            "完成后提供修改提交和可复核的原始结果路径。提交可保留在工作分支；发布、合入或更改共用机器服务前先请求确认。"
            "若用户中断，明确哪些远程 tmux 任务仍运行；不可将中断 Codex 等同于停止远程任务。"
        )

    def health(self):
        result = {"available": bool(self.executable), "authenticated": None, "account_type": None, "version": None, "error": None}
        if not self.executable:
            result["error"] = "Codex CLI was not found; install it and run codex login locally."
        else:
            client = None
            try:
                client = self.client_factory(self.executable, self.base_dir, lambda _: None, timeout=15)
                result["version"] = client.info.get("userAgent")
                account = client.request("account/read", {"refreshToken": False})
                result["authenticated"] = bool(account.get("account"))
                result["account_type"] = (account.get("account") or {}).get("type")
                if not result["authenticated"]:
                    result["error"] = "Run codex login in a local terminal before starting a session."
            except (SessionError, OSError) as exc:
                result["error"] = str(exc)
            finally:
                if client:
                    client.close()
        with self._lock:
            self._health = result
        return result

    def _client(self, session):
        session_id = session["id"]
        client = self._clients.get(session_id)
        if client and client.process.poll() is None:
            return client
        if not self.executable:
            raise SessionError("Codex CLI is unavailable")
        client = self.client_factory(self.executable, session["worktree"], lambda msg: self._event(session_id, msg))
        self._clients[session_id] = client
        params = {"cwd": session["worktree"], "approvalPolicy": "on-request", "approvalsReviewer": "user", "sandbox": "workspace-write"}
        if session.get("thread_id"):
            result = client.request("thread/resume", {**params, "threadId": session["thread_id"]})
        else:
            result = client.request("thread/start", params)
        with self._lock:
            session["thread_id"] = result["thread"]["id"]
            session["model"] = result.get("model")
            self._save(session)
        return client

    def start(self, session_id):
        with self._lock:
            session = self._get(session_id)
            self._require_adapt(session["backend"], session["branch"])
            if session["status"] in self.ACTIVE:
                raise SessionError("Session already has an active turn")
            text = session["prompt"] if not session.get("initial_prompt_sent") else "继续跟进本任务，从已有进展恢复，先核对机器上仍在运行的 tmux 任务和测试。"
        return self._launch(session_id, text)

    def _launch(self, session_id, text):
        with self._lock:
            session = self._get(session_id)
            self._require_adapt(session["backend"], session["branch"])
            if session["status"] in self.ACTIVE:
                raise SessionError("Session already has an active turn")
            if session_id in self._workers and self._workers[session_id].is_alive():
                raise SessionError("Previous startup is still settling; refresh before resuming")
            for other in self._sessions.values():
                if other["id"] != session_id and self._same_machine(other, session) and other["status"] in self.ACTIVE | {"prepared"}:
                    raise SessionError("This machine is reserved by another session")
            previous_status = session["status"]
            # Reserve startup before releasing the lock for the SSH probe.
            # interrupt() keeps this lease until the probe acknowledges it.
            session.update(status="starting", error=None, pending_requests=[], cancel_requested=False,
                           approval_cancel_pending=False, turn_id=None)
            session.pop("approval_cancel_turn_id", None)
            self._save(session)
            machine = dict(session) if session.get("preflight_required") else None
        if machine:
            try:
                preflight = self._check_connection(machine)
            except (SessionError, OSError) as exc:
                with self._lock:
                    session = self._get(session_id)
                    session.update(status="interrupted" if session.get("cancel_requested") else previous_status,
                                   error=str(exc))
                    self._save(session)
                raise
            with self._lock:
                session = self._get(session_id)
                session["preflight"] = preflight
                if session.get("cancel_requested"):
                    session.update(status="interrupted", turn_id=None, pending_requests=[])
                    self._save(session)
                    return self._session_view(session)
                if not preflight["ok"]:
                    session.update(status=previous_status, error=preflight["error"])
                self._save(session)
            if not preflight["ok"]:
                raise SessionError("连接预检未通过，agent 尚未启动：" + preflight["error"])
        with self._lock:
            session = self._get(session_id)
            if session.get("cancel_requested"):
                session.update(status="interrupted", turn_id=None, pending_requests=[])
                self._save(session)
                return self._session_view(session)
            worker = threading.Thread(target=self._run, args=(session_id, text), daemon=True)
            self._workers[session_id] = worker
            worker.start()
            return self._session_view(session)

    def _run(self, session_id, text):
        session = self._sessions[session_id]
        try:
            client = self._client(session)
            with self._lock:
                if session.get("cancel_requested"):
                    # No turn/start has been sent. Releasing the machine lease
                    # is safe only now that startup has acknowledged cancellation.
                    session.update(status="interrupted", turn_id=None, pending_requests=[])
                    self._save(session)
                    return
            result = client.request("turn/start", {"threadId": session["thread_id"], "input": [{"type": "text", "text": text}]})
            with self._lock:
                if session["status"] in self.ACTIVE:
                    session["turn_id"] = result["turn"]["id"]
                if session["status"] == "starting":
                    session["status"] = "running"
                cancel_requested = session.get("cancel_requested", False) and session["status"] in self.ACTIVE
                self._save(session)
            if cancel_requested:
                client.request("turn/interrupt", {"threadId": session["thread_id"], "turnId": result["turn"]["id"]})
        except (SessionError, OSError, KeyError) as exc:
            client = self._clients.pop(session_id, None)
            if client:
                client.close()
            with self._lock:
                session.update(status="failed", error=str(exc))
                self._save(session)

    def message(self, session_id, text):
        text = self._text(text, "message", 12000, True)
        with self._lock:
            session = self._get(session_id)
            self._require_adapt(session["backend"], session["branch"])
            if session.get("approval_cancel_pending") and session["status"] in self.ACTIVE:
                raise SessionError("本轮正在结束，请等待状态更新后发送修订要求，继续原任务。")
            status, turn_id, thread_id = session["status"], session.get("turn_id"), session.get("thread_id")
            client = self._clients.get(session_id)
        if status in {"running", "waiting_approval", "waiting_input"}:
            if not client or not turn_id:
                raise SessionError("Session is still starting")
            client.request("turn/steer", {"threadId": thread_id, "expectedTurnId": turn_id, "input": [{"type": "text", "text": text}]})
            self._event(session_id, {"method": "dashboard/message", "params": {"text": text}})
            return self._copy(session_id)
        if not session.get("initial_prompt_sent"):
            text = session["prompt"] + "\nAdditional user message:\n" + text
        return self._launch(session_id, text)

    def _copy(self, session_id):
        with self._lock:
            return self._session_view(self._get(session_id))

    def interrupt(self, session_id):
        with self._lock:
            session = self._get(session_id)
            if session["status"] == "prepared":
                session["status"] = "interrupted"
                self._save(session)
                return self._copy(session_id)
            if session["status"] not in self.ACTIVE:
                return self._copy(session_id)
            client = self._clients.get(session_id)
            thread_id, turn_id = session.get("thread_id"), session.get("turn_id")
            session["cancel_requested"] = True
            # Keep the alias leased during client/turn startup until the worker
            # confirms no turn ran, or turn/completed acknowledges interruption.
            session["status"] = "interrupting"
            self._save(session)
        if client and turn_id:
            client.request("turn/interrupt", {"threadId": thread_id, "turnId": turn_id})
        return self._copy(session_id)

    @staticmethod
    def _subset(proposed, requested):
        if isinstance(proposed, dict):
            return isinstance(requested, dict) and all(k in requested and CodexSessionManager._subset(v, requested[k]) for k, v in proposed.items())
        if isinstance(proposed, list):
            return isinstance(requested, list) and all(v in requested for v in proposed)
        return proposed == requested

    def respond(self, session_id, request_id, response):
        if not isinstance(response, dict):
            raise SessionError("Response must be a JSON object")
        with self._lock:
            session = self._get(session_id)
            request = next((r for r in session["pending_requests"] if str(r["id"]) == str(request_id)), None)
            client = self._clients.get(session_id)
            if not request or not client:
                raise SessionError("Request expired or connection lost; refresh session")
            method, params = request["method"], request["params"]
            if self._task_policy(session["backend"], session["branch"])["mode"] == "monitor":
                denied_approval = (method in {"item/commandExecution/requestApproval", "item/fileChange/requestApproval"}
                                   and response.get("decision") in ("cancel", "decline"))
                denied_permissions = method == "item/permissions/requestApproval" and response.get("permissions") == {}
                if not (denied_approval or denied_permissions):
                    raise SessionError("Monitor-only tasks allow only cancel/decline or empty permissions; interrupt to stop other requests")
            if method in {"item/commandExecution/requestApproval", "item/fileChange/requestApproval"}:
                if not isinstance(response.get("decision"), str) or response["decision"] not in {"accept", "decline", "cancel"}:
                    raise SessionError("Choose accept, decline, or cancel for this single request")
                result = {"decision": response["decision"]}
                allowed = params.get("availableDecisions")
                if allowed and result["decision"] not in allowed:
                    raise SessionError("This decision is not available for this request")
            elif method == "item/permissions/requestApproval":
                permissions = response.get("permissions")
                if not isinstance(permissions, dict) or not self._subset(permissions, params.get("permissions", {})):
                    raise SessionError("Grant only a subset of the displayed requested permissions")
                result = {"permissions": permissions, "scope": "turn"}
            elif method == "item/tool/requestUserInput":
                answers = response.get("answers")
                expected = {q["id"] for q in params.get("questions", [])}
                if not isinstance(answers, dict) or set(answers) != expected:
                    raise SessionError("Provide an answer for each displayed question")
                for value in answers.values():
                    if not isinstance(value, dict) or not isinstance(value.get("answers"), list) or not all(isinstance(s, str) and len(s) <= 12000 for s in value["answers"]):
                        raise SessionError("Invalid answer format")
                result = {"answers": answers}
            else:
                raise SessionError("Unsupported request type; interrupt the task to cancel")
            root_cancel = (request.get("source_scope", "root") == "root"
                           and result.get("decision") == "cancel")
            previous_cancel_state = {key: session[key] for key in
                                     ("status", "approval_cancel_pending", "approval_cancel_turn_id")
                                     if key in session}
            if root_cancel:
                # cancel ends this root turn; it does not interrupt a remote
                # job or authorize a replacement turn. Mark before sending so
                # even an immediate turn/completed cannot be overwritten.
                session.update(status="interrupting", approval_cancel_pending=True,
                               approval_cancel_turn_id=session.get("turn_id"))
            try:
                client.respond(request["id"], result)
            except Exception:
                if root_cancel and session.get("approval_cancel_pending") and session["status"] == "interrupting":
                    for key in ("status", "approval_cancel_pending", "approval_cancel_turn_id"):
                        if key in previous_cancel_state:
                            session[key] = previous_cancel_state[key]
                        else:
                            session.pop(key, None)
                raise
            session["pending_requests"] = [r for r in session["pending_requests"] if r is not request]
            if (not session.get("approval_cancel_pending")
                    and request.get("source_scope", "root") == "root"
                    and session["status"] in {"waiting_approval", "waiting_input"}):
                if not any(r.get("source_scope", "root") == "root" for r in session["pending_requests"]):
                    session["status"] = "running"
            self._save(session)
        return self._copy(session_id)

    @staticmethod
    def _event_thread_id(params):
        if not isinstance(params, dict):
            return None
        for key in ("threadId", "thread_id"):
            if isinstance(params.get(key), str) and params[key]:
                return params[key]
        thread = params.get("thread")
        if isinstance(thread, dict) and isinstance(thread.get("id"), str):
            return thread["id"]
        for field in ("item", "turn"):
            nested = params.get(field)
            if isinstance(nested, dict):
                for key in ("threadId", "thread_id"):
                    if isinstance(nested.get(key), str) and nested[key]:
                        return nested[key]
        return None

    def _event(self, session_id, message):
        with self._lock:
            session = self._sessions.get(session_id)
            if not session:
                return
            method, params = message.get("method", ""), message.get("params", {})
            event_thread = self._event_thread_id(params)
            root_thread = session.get("thread_id")
            is_root = bool(root_thread and event_thread == root_thread)
            scope = "root" if is_root else ("child" if event_thread and root_thread else "unknown")
            record = {"at": now(), "method": method, "params": bounded(params),
                      "source_thread_id": event_thread, "source_scope": scope}
            session["events"].append(record)
            session["events"] = session["events"][-80:]
            backlog = self._event_backlogs.setdefault(session_id, deque(maxlen=80))
            if len(backlog) == backlog.maxlen:
                # Bound memory during a persistent filesystem outage and make
                # any lost historical log records explicit. Pending approvals
                # and lifecycle state are retained separately in session.json.
                session["event_log_dropped_records"] = session.get("event_log_dropped_records", 0) + 1
            backlog.append(record)
            self._flush_event_log(session)
            if "id" in message:
                # Child approvals still need a human response on this same
                # app-server connection, even though child turns do not own
                # the parent session's lifecycle or progress.
                session["pending_requests"].append({"id": message["id"], "method": method, "params": params,
                    "source_thread_id": event_thread, "source_scope": scope})
                if is_root and not session.get("approval_cancel_pending"):
                    session["status"] = "waiting_input" if method == "item/tool/requestUserInput" else "waiting_approval"
            elif method == "serverRequest/resolved":
                request_id = params.get("requestId")
                session["pending_requests"] = [r for r in session["pending_requests"]
                    if not (str(r["id"]) == str(request_id) and (not event_thread or r.get("source_thread_id") == event_thread))]
                if (not session.get("approval_cancel_pending")
                        and not any(r.get("source_scope", "root") == "root" for r in session["pending_requests"])
                        and session["status"] in {"waiting_approval", "waiting_input"}):
                    session["status"] = "running"
            elif method == "connection/closed":
                if session["status"] in self.ACTIVE:
                    detail = params.get("detail", "Codex connection lost.")
                    session.update(status="disconnected", error=str(detail)[:2000] + " Resume the stored thread.", pending_requests=[])
            elif method == "connection/callbackError":
                session["last_callback_error"] = bounded(params)
            elif not is_root:
                # App-server forwards descendant-thread notifications. They
                # remain visible in logs, but cannot finish or overwrite this
                # session's parent turn, plan, message or outstanding requests.
                self._save_event(session, method)
                return
            elif method == "turn/started":
                session["turn_id"] = params["turn"]["id"]
                session["initial_prompt_sent"] = True
                session["status"] = "interrupting" if session.get("cancel_requested") or session.get("approval_cancel_pending") else "running"
            elif method == "turn/completed":
                turn = params.get("turn", {})
                if (session.get("approval_cancel_pending") and session.get("approval_cancel_turn_id")
                        and turn.get("id") != session["approval_cancel_turn_id"]):
                    # A child/unrelated turn or incomplete notification cannot
                    # release the cancellation wait for this specific root turn.
                    self._save_event(session, method)
                    return
                session["status"] = {"completed": "completed", "interrupted": "interrupted", "failed": "failed"}.get(turn.get("status"), "failed")
                session["error"] = turn.get("error")
                session["pending_requests"] = [r for r in session["pending_requests"] if r.get("source_scope", "root") != "root"]
                session["turn_id"] = None
                session["approval_cancel_pending"] = False
                session.pop("approval_cancel_turn_id", None)
            elif method == "turn/plan/updated":
                plan = params.get("plan", [])
                if isinstance(plan, list):
                    normalized = []
                    for item in plan[:30]:
                        if not isinstance(item, dict) or not isinstance(item.get("step"), str):
                            continue
                        status = {"inProgress": "in_progress", "in-progress": "in_progress"}.get(item.get("status"), item.get("status"))
                        if status in {"pending", "in_progress", "completed"}:
                            normalized.append({"step": item["step"][:1000], "status": status})
                    session["plan"] = normalized
                    session["plan_updated_at"] = datetime.now(timezone.utc).isoformat(timespec="microseconds")
                    session["plan_source"] = "notification"
                    session["progress_summary"] = None
                    session.pop("progress_artifact", None)
                    if isinstance(params.get("explanation"), str):
                        session["plan_explanation"] = params["explanation"][:4000]
                        session["progress_summary"] = session["plan_explanation"]
            elif method == "item/completed":
                item = params.get("item", {})
                if isinstance(item, dict) and item.get("type") == "agentMessage" and isinstance(item.get("text"), str):
                    session["last_agent_message"] = bounded(item["text"])
            self._save_event(session, method)

    def close(self):
        with self._lock:
            self._closed = True
            for timer in self._save_timers.values():
                timer.cancel()
            self._save_timers.clear()
        for client in list(self._clients.values()):
            client.close()
        self._clients.clear()
        with self._lock:
            for session_id in list(self._dirty_sessions):
                self._flush_event_log(self._sessions[session_id], force=True)
                try:
                    self._save(self._sessions[session_id])
                except OSError:
                    logger.warning("Session state still could not be saved during close")
