"""Local Codex app-server sessions for backend adaptation.

Uses the documented JSONL app-server protocol. No desktop database access or
automatic approval is used. Machine profiles contain SSH aliases, never keys.
"""
from __future__ import annotations

import atexit
from collections import deque
from concurrent.futures import Future, TimeoutError as FutureTimeout
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import threading
import time
import uuid


class SessionError(ValueError):
    pass


def now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def atomic_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix("." + uuid.uuid4().hex + ".tmp")
    try:
        tmp.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        os.replace(tmp, path)
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
            if self._closed or self.process.poll() is not None:
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

    def _read(self):
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
                    self.callback(message)
        finally:
            with self._lock:
                pending = list(self._pending.values())
            for future in pending:
                if not future.done():
                    future.set_exception(SessionError("Codex app-server exited"))
            if not self._closed:
                self.callback({"method": "connection/closed", "params": {"detail": "Codex app-server exited"}})

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
        self.client_factory = client_factory
        self._lock = threading.RLock()
        self._clients = {}
        self._workers = {}
        self._sessions = {}
        self._profiles = {}
        self._saved_at = {}
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

    def _machine(self, payload, require_ready=True):
        backend = payload.get("backend", "")
        if backend == "nvidia":
            backend = "cuda"
        if not isinstance(backend, str) or (backend not in self.backends and (require_ready or backend != "")):
            raise SessionError("Choose a configured backend")
        role = payload.get("role", "compute")
        if role not in ("compute", "gateway"):
            raise SessionError("Machine role must be compute or gateway")
        if require_ready and role != "compute":
            raise SessionError("A gateway cannot be used as a backend validation machine")
        alias = self._name(payload.get("ssh_alias", ""), "SSH alias")
        workspace = self._text(payload.get("remote_workspace", ""), "remote_workspace", required=require_ready)
        if workspace and (not workspace.startswith("/") or any(c in workspace for c in "\r\n\x00")):
            raise SessionError("remote_workspace must be an absolute Linux path")
        models = payload.get("model_paths", {})
        if not isinstance(models, dict) or len(models) > 20:
            raise SessionError("model_paths must map model names to machine paths")
        models = {self._text(k, "model name", 200, True): self._text(v, "model path", 2000, True) for k, v in models.items()}
        return {"backend": backend, "ssh_alias": alias, "remote_workspace": workspace, "model_paths": models, "role": role}

    def save_profile(self, payload):
        profile_id = self._name(payload.get("id") or uuid.uuid4().hex, "profile id")
        with self._lock:
            values = {**self._profiles.get(profile_id, {}), **payload}
            profile = self._machine(values, require_ready=False)
            profile.update(id=profile_id, label=self._text(values.get("label") or profile["ssh_alias"], "label", 200, True))
            for field in ("hardware", "source", "backend_source", "notes"):
                profile[field] = self._text(values.get(field, ""), field, 2000)
            self._profiles[profile_id] = profile
            atomic_json(self.root / "machines.json", self._profiles)
        return dict(profile)

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
        session["updated_at"] = now()
        atomic_json(self.root / session["id"] / "session.json", session)
        self._saved_at[session["id"]] = time.monotonic()

    def _get(self, session_id):
        if session_id not in self._sessions:
            raise SessionError("Unknown session")
        return self._sessions[session_id]

    def snapshot(self):
        with self._lock:
            return json.loads(json.dumps({
                "available": bool(self.executable), "default_branch": self.default_branch,
                "backends": sorted(self.backends), "health": self._health,
                "machine_profiles": list(self._profiles.values()),
                "sessions": sorted(self._sessions.values(), key=lambda s: s["created_at"], reverse=True),
            }))

    def create(self, payload):
        """Prepare only; callers explicitly invoke start to begin model work."""
        if payload.get("profile_id"):
            profile = self._profiles.get(payload["profile_id"])
            if not profile:
                raise SessionError("Unknown machine profile")
            payload = {**profile, **payload}
        machine = self._machine(payload)
        branch = self._text(payload.get("branch", self.default_branch), "branch", 200, True)
        if branch.startswith("-") or any(c.isspace() for c in branch):
            raise SessionError("Invalid branch")
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
        }
        with self._lock:
            # This lease covers active/prepared Codex sessions only. Remote tmux
            # processes can outlive a completed/interrupted local Codex turn.
            for existing in self._sessions.values():
                if existing["ssh_alias"] == machine["ssh_alias"] and existing["status"] in self.ACTIVE | {"prepared"}:
                    raise SessionError("This machine already has an active or prepared session; finish or interrupt it first")
            self._git("worktree", "add", "-b", work_branch, str(worktree), commit)
            session["prompt"] = self._prompt(session)
            self._sessions[session_id] = session
            self._save(session)
            return json.loads(json.dumps(session))

    def _prompt(self, session):
        evidence_root = self.evidence_dir / session["backend"] / session["id"]
        manifest_example = {
            "schema_version": 1, "run_id": session["id"] + "-<model>-<mode>",
            "backend": session["backend"], "branch": session["branch"],
            "commit": "<actual-tested-40-hex-commit>", "model": "<configured-model-name>",
            "mode": "<eager-or-graph>", "port": 8000, "results_dir": "results",
            "created_at": "<actual-run-UTC-time>",
        }
        return (
            "请跟进 vllm-plugin-FL 的 backend 适配，持续记录实现、测试和阻塞情况。\n"
            f"Backend: {session['backend']}\nBase branch: {session['branch']}\nBase commit: {session['commit']}\n"
            f"Local isolated worktree: {session['worktree']}\nWork branch: {session['work_branch']}\n"
            f"SSH host alias: {session['ssh_alias']}\nRemote workspace: {session['remote_workspace']}\n"
            f"Model paths: {json.dumps(session['model_paths'], ensure_ascii=False)}\n"
            f"Additional user objective: {session['goal'] or '检查当前适配，完成该 backend 的升级和验收。'}\n\n"
            "先读取仓库 AGENTS.md 和相关 vllm-upgrade-flagos / vllm-plugin-fl-setup-flagos 技能，确认版本和 backend。"
            "先只读检查指定机器、硬件、远程目录 git 状态和模型路径；在独立工作分支/目录完成变更，保留现有修改。"
            "SSH 只使用用户配置的别名，不能在配置或日志中写密码/私钥。不要更改全局代理；如已有 git 代理只用于 git，不能给 pip/curl 设置全局 HTTP(S)_PROXY。"
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
            if session["status"] in self.ACTIVE:
                raise SessionError("Session already has an active turn")
            text = session["prompt"] if not session.get("initial_prompt_sent") else "继续跟进本任务，从已有进展恢复，先核对机器上仍在运行的 tmux 任务和测试。"
        return self._launch(session_id, text)

    def _launch(self, session_id, text):
        with self._lock:
            session = self._get(session_id)
            if session["status"] in self.ACTIVE:
                raise SessionError("Session already has an active turn")
            if session_id in self._workers and self._workers[session_id].is_alive():
                raise SessionError("Previous startup is still settling; refresh before resuming")
            for other in self._sessions.values():
                if other["id"] != session_id and other["ssh_alias"] == session["ssh_alias"] and other["status"] in self.ACTIVE | {"prepared"}:
                    raise SessionError("This machine is reserved by another session")
            session.update(status="starting", error=None, pending_requests=[], cancel_requested=False, turn_id=None)
            self._save(session)
            worker = threading.Thread(target=self._run, args=(session_id, text), daemon=True)
            self._workers[session_id] = worker
            worker.start()
            return json.loads(json.dumps(session))

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
            return json.loads(json.dumps(self._get(session_id)))

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
            client.respond(request["id"], result)
            session["pending_requests"] = [r for r in session["pending_requests"] if r is not request]
            session["status"] = "running" if not session["pending_requests"] else session["status"]
            self._save(session)
        return self._copy(session_id)

    def _event(self, session_id, message):
        with self._lock:
            session = self._sessions.get(session_id)
            if not session:
                return
            method, params = message.get("method", ""), message.get("params", {})
            record = {"at": now(), "method": method, "params": bounded(params)}
            session["events"].append(record)
            session["events"] = session["events"][-80:]
            event_path = self.root / session_id / "events.jsonl"
            event_path.parent.mkdir(parents=True, exist_ok=True)
            with event_path.open("a", encoding="utf-8") as file:
                file.write(json.dumps(record, ensure_ascii=False) + "\n")
            if "id" in message:
                session["pending_requests"].append({"id": message["id"], "method": method, "params": params})
                session["status"] = "waiting_input" if method == "item/tool/requestUserInput" else "waiting_approval"
            elif method == "turn/started":
                session["turn_id"] = params["turn"]["id"]
                session["initial_prompt_sent"] = True
                session["status"] = "interrupting" if session.get("cancel_requested") else "running"
            elif method == "turn/completed":
                turn = params.get("turn", {})
                session["status"] = {"completed": "completed", "interrupted": "interrupted", "failed": "failed"}.get(turn.get("status"), "failed")
                session["error"] = turn.get("error")
                session["pending_requests"] = []
                session["turn_id"] = None
            elif method == "serverRequest/resolved":
                session["pending_requests"] = [r for r in session["pending_requests"] if str(r["id"]) != str(params.get("requestId"))]
                if not session["pending_requests"] and session["status"] in {"waiting_approval", "waiting_input"}:
                    session["status"] = "running"
            elif method == "connection/closed":
                if session["status"] in self.ACTIVE:
                    session.update(status="disconnected", error="Codex connection lost. Resume the stored thread.", pending_requests=[])
            if not method.endswith("/delta") or time.monotonic() - self._saved_at.get(session_id, 0) > 1:
                self._save(session)

    def close(self):
        for client in list(self._clients.values()):
            client.close()
        self._clients.clear()
