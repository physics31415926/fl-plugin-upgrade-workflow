"""Trusted-code monitor registry. Configuration selects names, never import paths."""
from dataclasses import dataclass
from typing import Callable, Protocol
import re


class Monitor(Protocol):
    branch: str
    github_repo: str
    config: dict
    def status(self, branch: str | None = None, force: bool = False) -> dict: ...
    def refresh_branch(self, branch: str | None = None) -> dict: ...
    def read_evidence(self, **fields) -> dict: ...
    def acceptance_record(self, backend: str, record_id: str, branch: str | None = None) -> dict: ...


@dataclass(frozen=True)
class MonitorType:
    factory: Callable[[dict], Monitor]
    supports_tasks: bool = False


def _plugin(config):
    from .branch_monitor import BranchMonitor
    return BranchMonitor(config)


_TYPES = {name: MonitorType(_plugin, True) for name in ("vllm", "sglang")}


def register_monitor(name: str, factory: Callable[[dict], Monitor], *, supports_tasks: bool = False):
    """Call from trusted startup code before loading the project configuration."""
    if not isinstance(name, str) or not re.fullmatch(r"[a-z][a-z0-9_-]{0,63}", name):
        raise ValueError("Monitor names must be simple lowercase identifiers")
    if name in _TYPES:
        raise ValueError(f"Monitor already registered: {name}")
    if not callable(factory):
        raise ValueError("Monitor factory must be callable")
    # The existing executors have plugin-specific prompts and validation.
    if supports_tasks:
        raise ValueError("Custom monitor types currently support monitoring only")
    _TYPES[name] = MonitorType(factory)


def monitor_type(name: str) -> MonitorType:
    if not isinstance(name, str) or name not in _TYPES:
        raise ValueError(f"Unknown monitor type: {name}; register a trusted adapter first")
    return _TYPES[name]


def create_monitor(config: dict) -> Monitor:
    return monitor_type(config.get("engine", "vllm")).factory(config)
