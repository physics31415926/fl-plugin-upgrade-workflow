"""Explicit, local-only transfer of a persisted Codex thread to its desktop UI."""
from __future__ import annotations

from datetime import datetime, timezone
from contextlib import contextmanager
from functools import wraps
import os
from pathlib import Path
import re
import time

from fl_dashboard.tasks.native_task_observer import NativeTaskError, NativeTaskObserver


UUID = re.compile(r"[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}\Z")
TERMINAL = {"task_complete", "task_failed", "task_interrupted", "task_aborted", "turn_aborted"}


def serialized_control(method):
    @wraps(method)
    def serialized(manager, *args, **kwargs):
        with manager.machine_lock:
            return method(manager, *args, **kwargs)
    return serialized


def rollout_path(value):
    if (not isinstance(value, str) or not value or len(value) > 4096
            or any(ord(c) < 32 for c in value)):
        return None
    try:
        path = Path(value)
        return str(path) if path.is_absolute() and path.suffix.lower() == ".jsonl" else None
    except (OSError, ValueError):
        return None


def thread_name(manager, session):
    return re.sub(r"\s+", " ",
                  f"{manager.project_label} · {session['backend']} · {session['branch']}").strip()[:200]


def remember_thread(manager, session, result, client):
    """Persist identity before optional naming, so a title error never duplicates it."""
    from fl_dashboard.agents.codex_sessions import SessionError
    with manager._lock:
        session["thread_id"] = result["thread"]["id"]
        session["model"] = result.get("model")
        path = rollout_path(result["thread"].get("path"))
        if path:
            session["rollout_path"] = path
        manager._save(session)
    if manager._uses_codex_runtime:
        name = thread_name(manager, session)
        try:
            client.request("thread/name/set", {"threadId": session["thread_id"], "name": name})
        except (SessionError, OSError):
            with manager._lock:
                session["thread_name_error"] = "会话已保存，但简短标题暂时未同步。"
                manager._save(session)
        else:
            with manager._lock:
                session["thread_name"] = name
                session.pop("thread_name_error", None)
                manager._save(session)
        if manager.codex_project_id:
            # Desktop project IDs may differ from the CLI's project registry.
            # Optional association must never prevent the persisted turn from starting.
            try:
                with request_timeout(client, time.monotonic() + 2):
                    client.request("thread/metadata/update", {
                        "threadId": session["thread_id"], "projectId": manager.codex_project_id})
            except (SessionError, OSError):
                with manager._lock:
                    session["thread_project_error"] = "会话已保存，项目归属暂时未同步；原任务可继续执行。"
                    manager._save(session)
            else:
                with manager._lock:
                    session.pop("thread_project_error", None)
                    manager._save(session)


class DesktopRolloutObserver(NativeTaskObserver):
    """Only a new desktop turn can release the machine's transferred lease."""
    def __init__(self, task, base_dir, handed_off_at):
        self._handed_off_at = self._time(handed_off_at)
        self._prefix_desktop = None
        super().__init__([task], base_dir)

    def _consume(self, state, event, thread_id):
        if not isinstance(event, dict):
            return
        payload, stamp = event.get("payload"), self._time(event.get("timestamp"))
        if (not isinstance(payload, dict) or payload.get("thread_id", thread_id) != thread_id
                or stamp is None or self._handed_off_at is None or stamp <= self._handed_off_at):
            return
        super()._consume(state, event, thread_id)
        if event.get("type") != "event_msg":
            return
        kind, turn = payload.get("type"), payload.get("turn_id")
        if kind == "task_started":
            valid_turn = isinstance(turn, str) and bool(turn.strip())
            state["_desktop_started"], state["_desktop_terminal"] = valid_turn, False
            state["_desktop_turn"] = turn if valid_turn else None
        elif (kind in TERMINAL and state.get("_desktop_started")
              and isinstance(turn, str) and turn == state.get("_desktop_turn")):
            state["_desktop_terminal"] = True
        if self._prefix_desktop is not None:
            self._prefix_desktop = {key: state[key] for key in ("_desktop_started", "_desktop_terminal", "_desktop_turn")
                                    if key in state}

    def _recover_lifecycle(self, stream, row, header_size, boundary):
        # NativeTaskObserver intentionally returns only lifecycle metadata from
        # the omitted prefix. Retain our epoch gate too, never prefix messages.
        self._prefix_desktop = {}
        try:
            lifecycle = super()._recover_lifecycle(stream, row, header_size, boundary)
            return {**lifecycle, **self._prefix_desktop}
        finally:
            self._prefix_desktop = None


def refresh_desktop(manager, session):
    if session.get("execution_owner") != "codex_desktop":
        return
    path, tid = rollout_path(session.get("rollout_path")), session.get("thread_id")
    observed = None
    if path and isinstance(tid, str) and UUID.fullmatch(tid):
        signature = (path, tid, session.get("desktop_handed_off_at"))
        previous = manager._desktop_observers.get(session["id"])
        try:
            if previous is None or previous[0] != signature:
                task = {"id": "desktop-" + session["id"], "thread_id": tid,
                        "backend": session["backend"], "branch": session["branch"],
                        "title": thread_name(manager, session), "rollout_path": path}
                observer = DesktopRolloutObserver(task, manager.base_dir, signature[2])
                manager._desktop_observers[session["id"]] = (signature, observer)
            observed = manager._desktop_observers[session["id"]][1].snapshot()[0]
        except (NativeTaskError, OSError, ValueError, TypeError):
            observed = None
    # A pre-transfer interruption is not evidence of a completed desktop turn.
    started = bool(observed and observed.get("_desktop_started"))
    material = {"status": observed["status"] if started else "unknown",
                "desktop_lease_released": bool(started and observed.get("_desktop_terminal")
                    and observed.get("status") in {"completed", "interrupted", "failed"}),
                "controllable": False}
    material["machine_reserved"] = not material["desktop_lease_released"]
    if observed:
        for field in ("plan", "stage", "progress_summary", "last_agent_message", "stale"):
            material[field] = observed.get(field)
        session["desktop_observed_at"] = observed.get("observed_at")
        if started:
            session["desktop_progress_updated_at"] = observed.get("updated_at")
    else:
        material.update(plan=[], stage=None, progress_summary=None, last_agent_message=None, stale=False)
    if any(session.get(key) != value for key, value in material.items()):
        session.update(material)
        manager._save(session)


def open_desktop_url(url):
    if os.name != "nt" or not hasattr(os, "startfile"):
        raise OSError("Desktop URL launcher is unavailable")
    os.startfile(url)


@contextmanager
def request_timeout(client, deadline):
    from fl_dashboard.agents.codex_sessions import SessionError
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise SessionError("Codex desktop handoff timed out")
    previous = getattr(client, "timeout", None)
    if isinstance(previous, (int, float)):
        client.timeout = min(previous, remaining)
    try:
        yield
    finally:
        if isinstance(previous, (int, float)):
            client.timeout = previous


def desktop_metadata(manager, session, client, deadline):
    from fl_dashboard.agents.codex_sessions import SessionError
    tid = session["thread_id"]
    for method, params, field in (
            ("thread/read", {"threadId": tid, "includeTurns": False}, "desktop_metadata_error"),
            ("thread/name/set", {"threadId": tid, "name": thread_name(manager, session)}, "thread_name_error"),
            ("thread/metadata/update", {"threadId": tid, "projectId": manager.codex_project_id}, "desktop_project_error")):
        if method == "thread/metadata/update" and not params["projectId"]:
            continue
        try:
            with request_timeout(client, deadline):
                result = client.request(method, params)
            with manager._lock:
                if method == "thread/read":
                    thread = result.get("thread", {})
                    path = rollout_path(thread.get("path")) if thread.get("id") == tid else None
                    if path:
                        session["rollout_path"] = path
                elif method == "thread/name/set":
                    session["thread_name"] = params["name"]
                session.pop(field, None)
                manager._save(session)
        except (SessionError, OSError, AttributeError, TypeError):
            with manager._lock:
                session[field] = "会话已保存，部分桌面元数据暂时无法同步。"
                manager._save(session)


def desktop(manager, session_id, *, launcher=None, timeout=15):
    """Stop only this local turn, persist transfer, then open the same thread ID."""
    from fl_dashboard.agents.codex_sessions import SessionError
    if not isinstance(timeout, (int, float)) or isinstance(timeout, bool) or not 0 < timeout <= 30:
        raise SessionError("Invalid desktop handoff timeout")
    launcher = launcher or open_desktop_url
    deadline = time.monotonic() + timeout
    # Serialize with both starts and other handoffs; callbacks need only _lock.
    with manager.machine_lock:
        with manager._lock:
            session = manager._get(session_id)
            if not manager._uses_codex_runtime or session.get("provider", "codex") != "codex":
                raise SessionError("只有本机 Codex 任务可以转交到 Codex 桌面端。")
            manager._require_adapt(session["backend"], session["branch"])
            tid = session.get("thread_id")
            if not isinstance(tid, str) or not UUID.fullmatch(tid):
                raise SessionError("任务还没有持久化的 Codex 会话；请先启动该任务。")
            url = "codex://threads/" + tid
            already_transferred = session.get("execution_owner") == "codex_desktop"
            if not already_transferred:
                session.update(desktop_handoff_pending=True, desktop_handoff_thread_id=tid,
                               desktop_handoff_turn_id=session.get("turn_id"), desktop_handoff_confirmed=False)
                manager._save(session)
                client = manager._clients.get(session_id)
                active = session["status"] in manager.ACTIVE or bool(session.get("turn_id"))
                if active and client is None:
                    session["desktop_handoff_error"] = "原执行器状态无法确认，未转交或打开桌面会话。"
                    manager._save(session)
                    raise SessionError(session["desktop_handoff_error"])
        if not already_transferred:
            if active:
                with request_timeout(client, deadline):
                    manager.interrupt(session_id, _desktop=True)
            while True:
                with manager._lock:
                    worker = manager._workers.get(session_id)
                    settled = worker is None or not worker.is_alive()
                    ended = (not active or session.get("desktop_handoff_confirmed")
                             or (session.get("desktop_handoff_turn_id") is None
                                 and session["status"] == "interrupted" and settled))
                    safe = settled and ended and session["status"] not in manager.ACTIVE
                if safe:
                    break
                if time.monotonic() >= deadline:
                    with manager._lock:
                        session["desktop_handoff_error"] = "尚未确认原轮次结束，执行权仍由看板保留；请稍后重试转交。"
                        manager._save(session)
                    raise SessionError(session["desktop_handoff_error"])
                time.sleep(min(0.025, max(0, deadline - time.monotonic())))
            metadata_client = client
            if metadata_client is None:
                executable = manager._resolve_executable()
                if executable and time.monotonic() < deadline:
                    try:
                        metadata_client = manager.client_factory(executable, manager.base_dir, lambda _: None,
                            timeout=min(5, max(0.001, (deadline - time.monotonic()) / 4)))
                    except (SessionError, OSError):
                        metadata_client = None
            if metadata_client is not None:
                try:
                    metadata_budget = min(3, max(0, deadline - time.monotonic()) / 2)
                    desktop_metadata(manager, session, metadata_client,
                                     time.monotonic() + metadata_budget)
                finally:
                    if metadata_client is not client:
                        metadata_client.close()
            if time.monotonic() >= deadline:
                with manager._lock:
                    session["desktop_handoff_error"] = "转交等待已超时，执行权仍由看板保留；请稍后重试。"
                    manager._save(session)
                raise SessionError(session["desktop_handoff_error"])
            if client is not None:
                try:
                    client.close()
                    if client.process.poll() is None:
                        raise OSError("Original app-server still alive")
                except (OSError, SessionError):
                    with manager._lock:
                        session["desktop_handoff_error"] = "原执行器尚未关闭，未转交或打开桌面会话。"
                        manager._save(session)
                    raise SessionError(session["desktop_handoff_error"]) from None
                with manager._lock:
                    if manager._clients.get(session_id) is client:
                        manager._clients.pop(session_id, None)
            if time.monotonic() >= deadline:
                with manager._lock:
                    session["desktop_handoff_error"] = "原执行器已关闭，转交等待已超时；请重试打开桌面端。"
                    manager._save(session)
                raise SessionError(session["desktop_handoff_error"])
            with manager._lock:
                session.update(execution_owner="codex_desktop", controllable=False, status="unknown",
                               desktop_handed_off_at=datetime.now(timezone.utc).isoformat(timespec="microseconds"),
                               desktop_lease_released=False, machine_reserved=True, pending_requests=[], turn_id=None,
                               desktop_handoff_pending=False)
                session.pop("desktop_handoff_error", None)
                manager._save(session)
        try:
            launcher(url)
        except (OSError, ValueError):
            with manager._lock:
                session["desktop_open_error"] = "执行权已转交；桌面应用未能自动打开，可点击会话链接重试。"
                session["desktop_opened"] = False
                manager._save(session)
        else:
            with manager._lock:
                session.pop("desktop_open_error", None)
                session["desktop_opened"] = True
                manager._save(session)
        with manager._lock:
            refresh_desktop(manager, session)
            return {"session": manager._session_view(session), "desktop_url": url,
                    "desktop_opened": bool(session.get("desktop_opened")),
                    "desktop_open_error": session.get("desktop_open_error")}
