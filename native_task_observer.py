"""Observe explicitly configured native Codex threads without taking ownership.

Rollouts are private append-only runtime evidence. Only lifecycle, assistant
messages and explicit plans are projected, never arbitrary tool arguments or outputs.
"""
from __future__ import annotations

from datetime import datetime, timezone
import copy
import json
import os
from pathlib import Path
import re
import threading


class NativeTaskError(ValueError):
    pass


class NativeTaskObserver:
    INITIAL_BYTES = 16 * 1024 * 1024
    INCREMENT_BYTES = 4 * 1024 * 1024
    LINE_BYTES = 512 * 1024
    HEADER_BYTES = 256 * 1024
    PUBLIC_FIELDS = ("id", "backend", "branch", "title", "source", "source_label",
                     "status", "stale", "observed_at", "updated_at", "stage")

    def __init__(self, tasks, base_dir, *, stale_seconds=1800, clock=None):
        if not isinstance(tasks, list) or len(tasks) > 100:
            raise NativeTaskError("external_tasks must be an array of at most 100 tasks")
        self._tasks, self._cache, self._lock = [], {}, threading.RLock()
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._stale_seconds = stale_seconds
        seen = set()
        for row in tasks:
            if not isinstance(row, dict):
                raise NativeTaskError("Each external task must be an object")
            tid, thread_id = row.get("id"), row.get("thread_id")
            if (not isinstance(tid, str) or not re.fullmatch(r"[a-z][a-z0-9_-]{0,63}", tid)
                    or re.fullmatch(r"[0-9a-f]{32}", tid) or tid in seen):
                raise NativeTaskError("External task IDs must be unique lowercase identifiers")
            if not isinstance(thread_id, str) or not re.fullmatch(r"[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}", thread_id):
                raise NativeTaskError("External task thread_id must be a UUID")
            for key, maximum in (("backend", 64), ("branch", 200), ("title", 200), ("rollout_path", 4096)):
                value = row.get(key)
                if not isinstance(value, str) or not value.strip() or len(value) > maximum or any(ord(c) < 32 for c in value):
                    raise NativeTaskError(f"External task {key} must be a nonempty bounded string")
            if not re.fullmatch(r"[a-z][a-z0-9_-]{0,63}", row["backend"]):
                raise NativeTaskError("External task backend must be a lowercase identifier")
            path = Path(row["rollout_path"]).expanduser()
            path = path if path.is_absolute() else Path(base_dir) / path
            if path.suffix.lower() != ".jsonl":
                raise NativeTaskError("External task rollout_path must identify a JSONL file")
            self._tasks.append({key: row[key] for key in ("id", "thread_id", "backend", "branch", "title")} | {"path": path})
            seen.add(tid)

    @staticmethod
    def _time(value):
        if not isinstance(value, str) or len(value) > 80:
            return None
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
            return parsed.astimezone(timezone.utc) if parsed.tzinfo is not None else None
        except ValueError:
            return None

    @staticmethod
    def _empty(row):
        return {key: row[key] for key in ("id", "thread_id", "backend", "branch", "title")} | {
            "source": "codex_native", "source_label": "Codex 原生子会话",
            "controllable": False, "historical": False, "status": "unknown", "stale": False,
            "updated_at": None, "message_updated_at": None, "progress_summary": None,
            "last_agent_message": None, "plan": [], "stage": None}

    @staticmethod
    def _text(content):
        if isinstance(content, str):
            return content[:8000]
        if not isinstance(content, list):
            return ""
        return "\n".join(item["text"] for item in content[:30] if isinstance(item, dict)
                         and isinstance(item.get("text"), str)
                         and item.get("type") in ("Text", "text", "output_text", "input_text"))[:8000]

    @staticmethod
    def _stage(plan):
        # Only an explicit in-progress plan can identify a stage. Text summaries
        # and completed turns cannot promote tests or acceptance to completion.
        aliases = (("environment", ("环境", "environment")), ("adaptation", ("适配", "升级", "adaptation")),
                   ("serving", ("服务启动", "model serving")), ("gate", ("gate", "验收")),
                   ("archive", ("归档", "结果收集", "archive")))
        for entry in plan:
            if entry["status"] != "in_progress":
                continue
            step = entry["step"].lower()
            for stage, words in aliases:
                if any(word in step for word in words):
                    return stage
        return None

    def _consume(self, state, event, thread_id):
        if not isinstance(event, dict) or event.get("type") not in ("event_msg", "response_item"):
            return
        payload = event.get("payload")
        if not isinstance(payload, dict):
            return
        if payload.get("thread_id", thread_id) != thread_id:
            return
        timestamp = self._time(event.get("timestamp"))
        if timestamp is None:
            return
        if event["type"] == "event_msg":
            kind = payload.get("type")
            if kind in ("task_started", "task_complete", "task_interrupted", "task_aborted", "task_failed", "turn_aborted", "item_completed", "token_count"):
                state["updated_at"] = timestamp.isoformat()
            if kind == "task_started":
                state["status"] = "running"
                state["_turn_id"] = payload.get("turn_id")
                state["plan"], state["stage"] = [], None
            elif kind in ("task_complete", "task_interrupted", "task_aborted", "task_failed", "turn_aborted"):
                if state.get("_turn_id") is not None and payload.get("turn_id") not in (None, state["_turn_id"]):
                    return
                state["status"] = {"task_complete": "completed", "task_failed": "failed"}.get(kind, "interrupted")
                if isinstance(payload.get("last_agent_message"), str):
                    state["progress_summary"] = state["last_agent_message"] = payload["last_agent_message"][:8000]
                    state["message_updated_at"] = timestamp.isoformat()
            elif kind == "item_completed":
                item = payload.get("item", {})
                if isinstance(item, dict) and item.get("type") == "AgentMessage" and item.get("phase") in ("commentary", "final_answer", "final"):
                    message = self._text(item.get("content"))
                    if message:
                        state["progress_summary"] = state["last_agent_message"] = message
                        state["message_updated_at"] = timestamp.isoformat()
        elif payload.get("type") == "message" and payload.get("role") == "assistant" and payload.get("phase") in ("commentary", "final_answer", "final"):
            message = self._text(payload.get("content"))
            if message:
                state["progress_summary"] = state["last_agent_message"] = message
                state["message_updated_at"] = timestamp.isoformat()
        elif (payload.get("type") == "function_call" and isinstance(payload.get("name"), str)
              and payload["name"].split(".")[-1] == "update_plan"):
            arguments = payload.get("arguments")
            if isinstance(arguments, str) and len(arguments) <= 16000:
                try:
                    arguments = json.loads(arguments)
                except ValueError:
                    return
            if not isinstance(arguments, dict) or not isinstance(arguments.get("plan"), list):
                return
            state["plan"] = [{"step": item["step"][:400], "status": item["status"]}
                             for item in arguments["plan"][:20] if isinstance(item, dict)
                             and isinstance(item.get("step"), str)
                             and item.get("status") in ("pending", "in_progress", "completed")]
            state["stage"] = self._stage(state["plan"])

    def _read(self, row):
        path = row["path"]
        # Explicit configuration still must not follow a swapped symlink.
        if path.is_symlink() or any(parent.is_symlink() for parent in path.parents):
            raise NativeTaskError("Native task rollout is unavailable")
        with path.open("rb") as stream:
            header = stream.readline(self.HEADER_BYTES + 1)
            if len(header) > self.HEADER_BYTES or not header.endswith(b"\n"):
                raise NativeTaskError("Native task rollout identity is unavailable")
            try:
                meta = json.loads(header)
            except (ValueError, UnicodeError):
                raise NativeTaskError("Native task rollout identity is invalid") from None
            if not isinstance(meta, dict) or meta.get("type") != "session_meta" or not isinstance(meta.get("payload"), dict) or meta["payload"].get("id") != row["thread_id"]:
                raise NativeTaskError("Native task rollout identity does not match")
            stat = os.fstat(stream.fileno())
            identity = (stat.st_dev, stat.st_ino)
            cached = self._cache.get(row["id"])
            reset = (cached is None or cached["identity"] != identity or stat.st_size < cached["position"]
                     or stat.st_size - cached["position"] > self.INCREMENT_BYTES)
            if reset:
                state = self._empty(row)
                offset = max(len(header), stat.st_size - self.INITIAL_BYTES)
                stream.seek(offset)
                if offset > len(header):
                    stream.readline(self.LINE_BYTES + 1)  # discard the partial first record
            else:
                state = copy.deepcopy(cached["state"])
                stream.seek(cached["position"])
            allowance = self.INITIAL_BYTES if reset else self.INCREMENT_BYTES
            start = stream.tell()
            content = stream.read(allowance)
            # Leave an incomplete final line for the next observation. It can be
            # appended while a task runs and must never discard a final event.
            last_newline = content.rfind(b"\n")
            consumed = last_newline + 1 if last_newline >= 0 else 0
            position = start + consumed
            for line in content[:consumed].splitlines():
                if not line or len(line) > self.LINE_BYTES:
                    continue
                try:
                    event = json.loads(line)
                except (ValueError, UnicodeError):
                    continue
                self._consume(state, event, row["thread_id"])
            state.pop("error", None)
            self._cache[row["id"]] = {"identity": identity, "position": position, "state": copy.deepcopy(state)}
            return state

    def snapshot(self, *, read_only=False):
        now = self._clock().astimezone(timezone.utc)
        result = []
        with self._lock:
            for row in self._tasks:
                try:
                    state = self._read(row)
                except (OSError, NativeTaskError):
                    state = self._empty(row)
                    state["error"] = "Native task runtime record unavailable; status is unknown"
                state.pop("_turn_id", None)
                state["observed_at"] = now.isoformat()
                changed = self._time(state.get("updated_at"))
                state["stale"] = state["status"] == "running" and (changed is None or (now - changed).total_seconds() > self._stale_seconds)
                if read_only:
                    state = {key: state[key] for key in self.PUBLIC_FIELDS}
                result.append(state)
        return result
