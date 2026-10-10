"""Private stdin bridge to the installed FlagScale Agent single-shot API.

This file is executed by the configured Python, never imported as flagscale_agent.
Only framed, scrubbed records are written to stdout; native terminal output is
suppressed. It neither reads agent.yaml nor performs native session resumption.
"""
from __future__ import annotations

import contextlib
import io
import json
import logging
import os
from pathlib import Path
import re
import sys

KEY_ENV = "FLFLOW_FLAGSCALE_API_KEY"
RECORD_LIMIT = 1024 * 1024


def scrub(value, key):
    if isinstance(value, str):
        return value.replace(key, "[redacted]") if key else value
    if isinstance(value, dict):
        return {scrub(str(k), key): scrub(v, key) for k, v in value.items()
                if str(k).lower() not in {"api_key", "apikey", "authorization", "headers", "env"}}
    if isinstance(value, list):
        return [scrub(item, key) for item in value]
    return value if value is None or isinstance(value, (bool, int, float)) else None


class NullOutput(io.TextIOBase):
    def write(self, value):
        return len(value)

    def flush(self):
        pass


def outcome(result, text, failed=False):
    """An exit code or saved conversation is not proof of task completion."""
    if failed or result is None:
        return "failed", "native_error" if failed else "missing_kernel_result"
    if getattr(result, "interrupted", False):
        return "interrupted", "interrupted"
    reason = str(getattr(result, "stop_reason", ""))
    if reason.startswith("llm_error"):
        return "failed", "llm_error"
    if reason == "explicit_signal":
        sentinels = ["[TASK_COMPLETE]", "[NEED_USER_INPUT]"]
        last = max(sentinels, key=text.rfind)
        if (text.rstrip().endswith(last)
                or last == sentinels[0] and re.search(r"\[TASK_COMPLETE\]\s*\n?_override_reason\s*[:=]", text.rstrip())):
            return ("completed", "task_complete") if last == sentinels[0] else ("waiting_input", "need_user_input")
        return "failed", "unverified_completion_signal"
    for prefix in ("max_continuations", "context_pressure", "completion_gate_livelock",
                   "blocked_by_guard_at_max_iter", "empty_output_max_retries"):
        if reason.startswith(prefix):
            return "waiting_input", prefix
    return "waiting_input", "iteration_limit" if not reason else "unrecognized_stop"


def execute(request, key, output):
    thread_id, turn_id = request["thread_id"], request["turn_id"]

    def emit(kind, **fields):
        value = scrub({"type": kind, "thread_id": thread_id, "turn_id": turn_id, **fields}, key)
        encoded = json.dumps(value, ensure_ascii=False)
        if len(encoded.encode("utf-8")) > RECORD_LIMIT:
            raise ValueError("record size")
        output.write(encoded + "\n")
        output.flush()

    # Pop the credential before importing the agent or creating its Shell tool.
    os.environ.pop(KEY_ENV, None)
    state = Path(request["state_dir"]).resolve()
    cwd = Path.cwd().resolve()
    if state == cwd or state.is_relative_to(cwd):
        raise ValueError("state must be outside task checkout")
    state.mkdir(parents=True, exist_ok=True)
    if os.name != "nt":
        state.chmod(0o700)
    os.environ["FLAGSCALE_HOME"] = str(state)
    logging.disable(logging.CRITICAL)
    from flagscale_agent.react.config import AgentConfig
    from flagscale_agent.react.agent import WorkerAgent

    settings = request["settings"]
    config = AgentConfig(provider=settings["llm_provider"], api_key=key,
                         session_dir=str(state / "sessions"), shell_env={},
                         **{name: settings[name] for name in (
                             "model", "base_url", "max_iterations", "max_continuations",
                             "max_context_tokens", "max_output_tokens", "thinking_budget", "time_budget_sec")})
    agent = WorkerAgent(config)
    kernel = agent._kernel
    if not callable(kernel.run_turn) or not hasattr(kernel, "deps"):
        raise ValueError("unsupported agent API")
    native_id = agent._session_id
    if not isinstance(native_id, str) or not re.fullmatch(r"[0-9a-f]{8}", native_id):
        raise ValueError("invalid native session identity")
    captured = {"result": None, "failed": False}
    original_turn = kernel.run_turn

    def run_turn(*args, **kwargs):
        try:
            captured["result"] = original_turn(*args, **kwargs)
            return captured["result"]
        except BaseException:
            captured["failed"] = True
            raise

    kernel.run_turn = run_turn
    original_loop = agent._react_loop

    def react_loop(*args, **kwargs):
        try:
            return original_loop(*args, **kwargs)
        except BaseException:
            captured["failed"] = True
            raise

    agent._react_loop = react_loop
    original_append = agent.history.append

    def append(message):
        return original_append(scrub(message, key))

    agent.history.append = append
    original_save = agent._save_conversation

    def clean_history():
        for name in ("_messages", "_full_log"):
            if hasattr(agent.history, name):
                setattr(agent.history, name, scrub(getattr(agent.history, name), key))
        if hasattr(agent, "_session_input_history"):
            agent._session_input_history = scrub(agent._session_input_history, key)

    def save(*args, **kwargs):
        clean_history()
        return original_save(*(scrub(value, key) for value in args), **scrub(kwargs, key))

    agent._save_conversation = save
    original_full_save = getattr(agent, "_save_conversation_full", None)
    if callable(original_full_save):
        def save_full(*args, **kwargs):
            clean_history()
            return original_full_save(*(scrub(value, key) for value in args), **scrub(kwargs, key))
        agent._save_conversation_full = save_full
    # Context compaction writes history directly rather than calling append,
    # and native hard-reset immediately saves its full conversation.
    original_reset = getattr(agent.history, "hard_reset", None)
    if callable(original_reset):
        def hard_reset(*args, **kwargs):
            return original_reset(*(scrub(value, key) for value in args), **scrub(kwargs, key))
        agent.history.hard_reset = hard_reset
    original_response = kernel.deps.on_response_fn
    original_tools = kernel.deps.on_tool_results_fn

    def response(value):
        if original_response:
            original_response(value)
        content = value.get("content", "") if isinstance(value, dict) else ""
        if isinstance(content, list):
            content = "\n".join(item.get("text", "") for item in content if isinstance(item, dict) and item.get("type") == "text")
        if isinstance(content, str) and content:
            emit("assistant", text=scrub(content, key)[:24000])

    def tools(calls, results):
        if original_tools:
            original_tools(calls, results)
        # Arguments/results may carry raw credentials; show tool names only.
        names = [item.get("name") for item in calls if isinstance(item, dict)
                 and isinstance(item.get("name"), str) and re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{0,79}", item["name"])]
        emit("tools", names=names[:100])

    kernel.deps.on_response_fn = response
    kernel.deps.on_tool_results_fn = tools
    emit("init", native_session_id=native_id)
    try:
        agent.run(single_shot_query=request["prompt"])
    except BaseException:
        captured["failed"] = True
    text = agent._get_last_assistant_text()
    text = text if isinstance(text, str) else ""
    status, reason = outcome(captured["result"], text, captured["failed"])
    counts = {}
    for name in ("iterations", "input_tokens", "output_tokens"):
        number = getattr(captured["result"], name, None)
        if type(number) is int and number >= 0:
            counts[name] = number
    emit("result", status=status, stop_reason=reason, native_session_id=native_id,
         text=scrub(text, key)[:24000], **counts)


def main():
    key = os.environ.pop(KEY_ENV, "")
    output = sys.stdout
    request = None
    try:
        raw = sys.stdin.read(RECORD_LIMIT + 1)
        if len(raw.encode("utf-8")) > RECORD_LIMIT or not key:
            raise ValueError("invalid worker input")
        request = json.loads(raw)
        if (request.get("schema_version") != 1 or not isinstance(request.get("prompt"), str)
                or not request["prompt"].strip()
                or not re.fullmatch(r"[0-9a-f-]{36}", request.get("thread_id", ""))
                or not re.fullmatch(r"[0-9a-f]{32}", request.get("turn_id", ""))):
            raise ValueError("invalid worker request")
        with contextlib.redirect_stdout(NullOutput()), contextlib.redirect_stderr(NullOutput()):
            execute(request, key, output)
        return 0
    except BaseException:
        # Never forward exception text, native stderr, or configuration.
        if isinstance(request, dict):
            output.write(json.dumps({"type": "result", "thread_id": request.get("thread_id"),
                                    "turn_id": request.get("turn_id"), "status": "failed",
                                    "stop_reason": "worker_unavailable"}) + "\n")
            output.flush()
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
