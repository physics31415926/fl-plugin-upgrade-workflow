"""Derive one selected-device task without writing to that remote machine.

The probe is run only for explicit automatic creation. A short-lived, one-use
process-local proof lets local owners prepare a future workspace; a JSON context
supplied by an API caller can never stand in for that proof.
"""
from __future__ import annotations

import copy
import hashlib
import hmac
import json
import os
from pathlib import Path, PurePosixPath
import re
import secrets
import shlex
import shutil
import subprocess
import threading
import time

from fl_dashboard.agents.codex_sessions import CodexSessionManager, SessionError

_RESERVED = {"_automatic_proof", "automatic_context"}
_FIELDS = ("profile_id", "backend", "branch", "ssh_alias", "remote_workspace",
           "model_paths", "ssh_connection", "role", "goal", "automatic_context")
_PROOFS = {}
_PROOF_LOCK = threading.Lock()
_PROOF_SECRET = secrets.token_bytes(32)
_PROOF_TTL = 240
PROBE_TIMEOUT = 20
PROBE_OUTPUT_LIMIT = 16384

# These are vendor/model identifiers, not guesses from generic GPU counts or RAM.
_HINTS = {
    "cuda": r"\b(?:cuda|nvidia|a100|h100|h200|a800|h800|l40s|geforce|tesla|rtx)\b",
    "ascend": r"\b(?:ascend|huawei|atlas|cann|910b|910c)\b|昇腾",
    "metax": r"\b(?:metax|maca|mc550|c550)\b|沐曦",
    "corex": r"\b(?:iluvatar|corex|bi100|bi150)\b|天数",
    "hygon": r"\b(?:hygon|dcu)\b|海光",
    "musa": r"\b(?:musa|moorethreads|mtt)\b|摩尔线程",
    "enflame": r"\b(?:enflame|topsrider|gcu)\b|燧原",
    "tsingmicro": r"\btsingmicro\b|清微",
    "sunrise": r"\bsunrise\b|曦望",
    "kunlunxin": r"\b(?:kunlunxin|kunlun)\b|昆仑芯",
    "thead": r"\b(?:thead|t-head|hanguang)\b|平头哥|含光",
}

# No mkdir, clone, checkout, environment mutation, or remote agent runs here.
# Only known shallow roots are inspected; at most 100 candidate directories are
# examined and no more than three HOME-relative directory levels are visited.
_PROBE_SCRIPT = """set -eu
engine=$1
configured=$2
hardware=$3
printf 'FLFLOW_AUTO_READY=1\\n'
printf 'HOME\\t%s\\n' "$HOME"
if command -v tmux >/dev/null 2>&1; then printf 'TMUX\\t1\\n'; else printf 'TMUX\\t0\\n'; fi
if [ "$hardware" = 1 ]; then
  if command -v lspci >/dev/null 2>&1; then
    lspci 2>/dev/null | grep -Ei 'VGA|3D controller|Display controller|Processing accelerators|Co-processor' | head -n 80 | sed 's/^/HARDWARE\\t/' || :
  fi
  if command -v nvidia-smi >/dev/null 2>&1; then
    nvidia-smi --query-gpu=name --format=csv,noheader 2>/dev/null | head -n 4 | sed 's/^/HARDWARE\\tnvidia GPU: /' || :
  fi
fi
count=0
found=0
inspect() {
  [ "$found" = 0 ] || return 0
  [ "$count" -lt 100 ] || return 0
  count=$((count + 1))
  candidate=$1
  [ -d "$candidate" ] || return 0
  case "$engine" in
    sglang) [ -d "$candidate/sglang_fl" ] || return 0 ;;
    vllm) [ -d "$candidate/vllm_fl" ] || return 0 ;;
    *) return 0 ;;
  esac
  command -v git >/dev/null 2>&1 || return 0
  top=$(git -C "$candidate" rev-parse --show-toplevel 2>/dev/null) || return 0
  [ "$top" = "$candidate" ] || return 0
  sha=$(git -C "$candidate" rev-parse --verify HEAD 2>/dev/null) || return 0
  printf 'SOURCE\\t%s\\t%s\\n' "$candidate" "$sha"
  found=1
}
[ -z "$configured" ] || inspect "$configured"
inspect "$HOME"
for root in "$HOME" "$HOME/work" "$HOME/workspace" "$HOME/repos" "$HOME/projects" "$HOME/GitHub" "$HOME/Documents/GitHub"; do
  [ -d "$root" ] || continue
  for candidate in "$root"/*; do
    [ "$count" -lt 100 ] || break
    inspect "$candidate"
    [ "$found" = 0 ] || break
  done
  [ "$found" = 0 ] || break
done
printf 'CHECKED\\t%s\\n' "$count"
"""


def _digest(payload):
    body = {key: payload.get(key) for key in _FIELDS}
    try:
        raw = json.dumps(body, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    except (TypeError, ValueError):
        raise SessionError("Automatic task context must contain JSON values") from None
    return hmac.new(_PROOF_SECRET, raw, hashlib.sha256).digest()


def _issue(payload, manager):
    token = secrets.token_urlsafe(32)
    current = time.monotonic()
    with _PROOF_LOCK:
        for key, record in list(_PROOFS.items()):
            if current - record[0] > _PROOF_TTL:
                _PROOFS.pop(key, None)
        if len(_PROOFS) >= 256:
            raise SessionError("Too many pending automatic tasks; retry after creating existing tasks")
        _PROOFS[token] = (current, manager, _digest(payload))
    return token


def consume_automatic_task(payload, manager):
    """Validate and remove a server-issued proof before session persistence."""
    token = payload.get("_automatic_proof")
    if not token:
        if _RESERVED.intersection(payload) or payload.get("automatic") is True:
            raise SessionError("Automatic task context requires a server-issued probe proof")
        return dict(payload), None
    if not isinstance(token, str):
        raise SessionError("Invalid automatic task proof")
    with _PROOF_LOCK:
        record = _PROOFS.pop(token, None)
    if (record is None or time.monotonic() - record[0] > _PROOF_TTL
            or record[1] is not manager or not hmac.compare_digest(record[2], _digest(payload))):
        raise SessionError("Automatic task proof is invalid, expired or already used")
    context = payload.get("automatic_context")
    if not isinstance(context, dict) or context.get("probe", {}).get("ok") is not True:
        raise SessionError("Automatic task probe context is invalid")
    clean = {key: value for key, value in payload.items() if key not in _RESERVED and key != "automatic"}
    return clean, copy.deepcopy(context)


def _hint(value):
    text = str(value).lower().replace("_", " ")
    matches = {backend for backend, pattern in _HINTS.items() if re.search(pattern, text)}
    return next(iter(matches)) if len(matches) == 1 else None


def _path(value, field, *, allow_empty=False):
    if value == "" and allow_empty:
        return ""
    if (not isinstance(value, str) or not value.startswith("/") or len(value) > 2000
            or any(ord(c) < 32 for c in value) or ".." in PurePosixPath(value).parts):
        raise SessionError(f"Automatic probe returned an invalid {field}")
    return str(PurePosixPath(value))


def _resolve(project, provider, profile_id):
    sessions = project.sessions
    if sessions is None:
        raise SessionError("Project sessions are unavailable")
    if hasattr(sessions, "_manager") and hasattr(sessions, "_profile"):
        target = sessions._manager(provider)
        source_provider, profile = sessions._profile(profile_id)
        source = sessions._manager(source_provider)
    else:
        if provider != getattr(sessions, "provider", "codex"):
            raise SessionError("The selected agent provider is unavailable")
        target = source = sessions
        profile = next((row for row in sessions.snapshot(compact=True).get("machine_profiles", [])
                        if row.get("id") == profile_id), None)
        if profile is None:
            raise SessionError("Unknown machine profile")
    return target, source, copy.deepcopy(profile)


def _ssh_argv(source, profile, executable):
    alias = CodexSessionManager._name(profile.get("ssh_alias", ""), "SSH alias")
    argv = [executable]
    config_path = profile.get("ssh_config_path")
    if profile.get("ssh_connection"):
        if not isinstance(config_path, str):
            raise SessionError("Saved SSH connection has no owner-managed config; save it again")
        path = Path(config_path).resolve()
        if not path.is_file() or not path.is_relative_to(Path(source.root).resolve()):
            raise SessionError("Saved SSH config is unavailable or outside its owner directory")
        argv += ["-F", str(path)]
    argv += ["-o", "BatchMode=yes", "-o", "ConnectTimeout=8", "-o", "ConnectionAttempts=1",
             "-o", "ServerAliveInterval=5", "-o", "ServerAliveCountMax=1",
             "-o", "StrictHostKeyChecking=yes", "-o", "ClearAllForwardings=yes",
             "-o", "ForwardAgent=no", "-o", "ForwardX11=no", alias]
    return argv


def _probe(source, profile, engine, need_hardware, *, runner, ssh_executable):
    executable = ssh_executable or shutil.which("ssh")
    if not executable:
        raise SessionError("OpenSSH was not found; automatic device discovery is unavailable")
    argv = _ssh_argv(source, profile, executable)
    configured = _path(profile.get("remote_workspace") or "", "configured workspace", allow_empty=True)
    command = "sh -s -- " + " ".join(shlex.quote(value) for value in
                                   (engine, configured, "1" if need_hardware else "0"))
    try:
        result = runner(argv + [command], input=_PROBE_SCRIPT.encode("utf-8"), capture_output=True,
                        timeout=PROBE_TIMEOUT,
                        creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
    except subprocess.TimeoutExpired:
        raise SessionError("所选设备的只读 SSH 探测超时（20 秒）；请检查连接后重试") from None
    except OSError:
        raise SessionError("无法启动所选设备的只读 SSH 探测") from None
    stdout = result.stdout if isinstance(result.stdout, bytes) else str(result.stdout or "").encode("utf-8")
    if len(stdout) > PROBE_OUTPUT_LIMIT:
        raise SessionError("所选设备探测输出超过 16 KiB；未创建任务")
    if result.returncode:
        stderr = result.stderr.decode("utf-8", "replace") if isinstance(result.stderr, bytes) else str(result.stderr or "")
        if "host key verification failed" in stderr.lower():
            error = "SSH 主机密钥尚未受信任；请在本机终端确认所选别名后重试"
        elif "permission denied" in stderr.lower():
            error = "所选设备 SSH 认证失败；请检查已保存的连接"
        else:
            error = "所选设备只读 SSH 探测失败；请在本机终端检查已保存的连接"
        raise SessionError(error)
    lines = stdout.decode("utf-8", "replace").splitlines()
    if "FLFLOW_AUTO_READY=1" not in lines:
        raise SessionError("所选设备未返回有效的只读 SSH 探测结果")
    homes = [line.partition("\t")[2] for line in lines if line.startswith("HOME\t")]
    if len(homes) != 1:
        raise SessionError("所选设备未返回唯一的远端 HOME")
    home = _path(homes[0], "remote HOME")
    if home == "/":
        raise SessionError("Remote HOME must be a user directory")
    sources = []
    for line in lines:
        if line.startswith("SOURCE\t"):
            parts = line.split("\t")
            if len(parts) != 3 or not re.fullmatch(r"[0-9a-f]{40}", parts[2]):
                raise SessionError("Automatic probe returned an invalid checkout identity")
            sources.append({"path": _path(parts[1], "source checkout"), "commit": parts[2]})
    if len(sources) > 1:
        raise SessionError("Automatic probe returned ambiguous source checkouts")
    hardware = "\n".join(line.partition("\t")[2] for line in lines if line.startswith("HARDWARE\t"))
    # Command options are probe-only; the agent receives the effective alias and
    # the same owner-managed -F config, which can be essential for manual profiles.
    task_ssh = ["ssh"]
    if "-F" in argv:
        task_ssh += ["-F", argv[argv.index("-F") + 1]]
    task_ssh.append(profile["ssh_alias"])
    return {"ok": True, "home": home, "tmux_available": "TMUX\t1" in lines,
            "source_checkout": sources[0] if sources else None}, hardware, task_ssh


def _goal(project, backend, branch, workspace, probe, ssh_argv, user_goal, *, legacy_remote):
    source = probe["source_checkout"]
    facts = {"project": project.id, "engine": project.engine_name, "backend": backend,
             "branch": branch, "remote_home": probe["home"], "source_checkout": source,
             "future_task_workspace": workspace, "workspace_exists": False,
             "tmux_available": probe["tmux_available"], "probe_ssh_argv": ssh_argv}
    ssh_instruction = (
        "旧 owner 任务：后续所有 SSH 使用本段 probe_ssh_argv 的保存别名及 -F 配置；"
        "通用提示若仅显示裸别名，必须补上此 -F。此配置仍归来源 profile owner 管理，"
        "如配置已更改或不可用，报告阻塞并核对连接，不能退回裸别名或禁用主机校验。"
        if legacy_remote else
        "probe_ssh_argv 仅说明本次探测使用的来源配置；后续所有 SSH 以任务 prompt 中"
        "冻结的 task-local session SSH argv 为准，不得继续使用来源 profile 的 -F 路径。"
    )
    return (
        "自动准备任务：以下事实来自本次所选设备的只读探测：\n"
        + json.dumps(facts, ensure_ascii=False) + "\n"
        "future_task_workspace 是计划创建的任务私有目录，尚未创建，不能声称它已经存在或已经通过 Git 预检。"
        "先只读确认硬件、HOME、源码与模型；已有 source_checkout 只作只读基线，保留其全部修改，"
        "不得在原源码 checkout 执行 checkout/pull/reset/clean、安装或修改代码。"
        "在所示任务目录内准备隔离候选和工具/日志/证据；若需新取源码，核对本项目真实源地址和基线分支后，"
        "由 agent 通过唯一命名 tmux 执行，HTTP 创建请求未执行 clone。"
        "安装、下载、服务、测试等长操作一律用 tmux；检查或准备 tmux 后再开始长操作。"
        "遵循本项目真实 AGENTS.md/README/CI；模型路径、依赖与工具以设备实际发现为准，缺失明确报告。"
        + ssh_instruction +
        "正确引用远端参数；不得读取或输出私钥/凭据。"
        "后台任务与来源 checkout 均不得清理或停止；按面板任务指令记录真实进度和验收证据。\n"
        + ("用户补充目标：" + user_goal if user_goal else "目标：完成所选 backend 的实际适配和验收，明确报告阻塞与未执行项。")
    )


def normalize_automatic_task(project, payload, *, runner=None, ssh_executable=None):
    """Return a JSON create payload; legacy/manual requests never invoke SSH."""
    if not isinstance(payload, dict):
        raise SessionError("Task payload must be an object")
    if _RESERVED.intersection(payload):
        raise SessionError("Automatic proof and context are server-owned fields")
    if payload.get("automatic") is not True:
        return dict(payload)
    if set(payload) - {"provider", "profile_id", "automatic", "branch", "goal", "start"}:
        raise SessionError("Automatic tasks accept only device, agent, branch and optional goal")
    provider = payload.get("provider", "codex")
    if provider not in ("codex", "claude", "flagscale"):
        raise SessionError("Agent provider must be codex, claude or flagscale")
    if "start" in payload and not isinstance(payload["start"], bool):
        raise SessionError("start must be a boolean")
    profile_id = CodexSessionManager._name(payload.get("profile_id", ""), "profile id")
    target, source, profile = _resolve(project, provider, profile_id)
    if provider == "claude":
        settings = getattr(target, "agent_settings", None)
        if settings is None:
            raise SessionError("Protected Claude settings are unavailable")
        public = settings.snapshot()
        if not public.get("configured"):
            raise SessionError("Configure the local Claude API key before creating an automatic task")
        tools = public.get("allowed_tools") or []
        if not any(isinstance(tool, str) and (tool == "Bash" or tool.startswith("Bash(")) for tool in tools):
            raise SessionError("Claude 自动远端任务需要显式许可 Bash；请在本机 Agent 设置中确认工具权限")
    if profile.get("role", "compute") != "compute":
        raise SessionError("Select a compute device for automatic adaptation")
    branch = CodexSessionManager._text(payload.get("branch") or project.default_branch
                                      or getattr(target, "default_branch", ""), "branch", 200, True)
    if branch.startswith("-") or any(c.isspace() for c in branch):
        raise SessionError("Invalid branch")
    user_goal = CodexSessionManager._text(payload.get("goal", ""), "goal", 2500)
    configured_backend = profile.get("backend") or ""
    if configured_backend == "nvidia":
        configured_backend = "cuda"
    available = getattr(target, "backends", set(target.config.get("session_backends", CodexSessionManager.BACKENDS)))
    if not isinstance(configured_backend, str):
        raise SessionError("The saved device backend must be a string")
    if configured_backend and configured_backend not in available:
        raise SessionError("The saved device backend is unsupported by this project")
    # Names and aliases may be stale or misleading. Every unconfigured
    # backend is resolved from hardware observed in this same selected SSH probe.
    probe, hardware, ssh_argv = _probe(source, profile, project.engine_name, not configured_backend,
                                     runner=runner or subprocess.run, ssh_executable=ssh_executable)
    backend_source = "configured" if configured_backend else "ssh_hardware"
    backend = configured_backend or _hint(hardware)
    if not backend:
        raise SessionError("无法从所选设备实际硬件可靠识别 backend；请为该设备配置明确的 backend")
    hardware_hint = _hint(profile.get("hardware") or "")
    if not configured_backend and hardware_hint and hardware_hint != backend:
        raise SessionError("所选设备实际硬件与已保存的 hardware 信息冲突；请核对设备配置")
    if backend not in available:
        raise SessionError("The detected device backend is unsupported by this project")
    project_id = CodexSessionManager._name(project.id, "project id")
    workspace = probe["home"].rstrip("/") + "/.flflow/tasks/" + project_id + "/" + secrets.token_hex(8) + "/candidate"
    context = {"schema_version": 1, "project_id": project_id, "provider": provider,
               "backend_source": backend_source, "workspace_exists": False, "probe": probe}
    from fl_dashboard.projects.project_registry import RemoteSessionManager
    legacy_remote = isinstance(target, RemoteSessionManager)
    value = {"provider": provider, "profile_id": profile_id, "role": "compute", "backend": backend,
             "branch": branch, "ssh_alias": profile["ssh_alias"], "remote_workspace": workspace,
             "model_paths": copy.deepcopy(profile.get("model_paths") or {}),
             "ssh_connection": copy.deepcopy(profile.get("ssh_connection")),
             "require_preflight": False,
             "goal": _goal(project, backend, branch, workspace, probe, ssh_argv, user_goal,
                           legacy_remote=legacy_remote)}
    if len(value["goal"]) > 6000:
        raise SessionError("Automatic task instructions exceed the task limit; shorten the optional goal")
    if legacy_remote:
        # Older owners know only plain payload fields and force strict preflight
        # for manual SSH connections. The read-only probe above already used the
        # saved owner's actual config. Retain that argv in the bootstrap goal.
        value["ssh_connection"] = None
    else:
        value["automatic_context"] = context
        value["_automatic_proof"] = _issue(value, target)
    return value
