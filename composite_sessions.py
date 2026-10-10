"""Route dashboard sessions to their provider without changing existing owners."""
from __future__ import annotations

import copy
import re
import threading

from codex_sessions import SessionError


PUBLIC_SETTINGS_FIELDS = {"provider", "base_url", "model", "permission_mode", "allowed_tools", "max_turns",
                          "configured", "key_storage", "api_protocol", "llm_provider",
                          "max_iterations", "max_continuations", "max_output_tokens",
                          "thinking_budget", "time_budget_sec", "max_context_tokens"}

FLAGSCALE_CAPABILITIES = {"follow_up_mode": "fresh_turn_with_context", "supports_resume": False,
                          "supports_mid_turn_steer": False}


def safe_agent_settings(value):
    """Do not rely on a provider response to discard secret fields at the API."""
    return {key: copy.deepcopy(item) for key, item in value.items() if key in PUBLIC_SETTINGS_FIELDS}


class CompositeSessionManager:
    def __init__(self, codex, claude, settings_store, *, flagscale=None, flagscale_settings=None, errors=None):
        if codex is None and claude is None and flagscale is None:
            raise SessionError("No local agent session provider is configured")
        self.codex, self.claude, self.flagscale = codex, claude, flagscale
        self.agent_settings = settings_store
        self.flagscale_settings = flagscale_settings
        self.providers_error = dict(errors or {})
        self.managers = [manager for manager in (codex, claude, flagscale) if manager is not None]
        first = codex or claude or flagscale
        self.config = first.config
        self.base_dir = getattr(first, "base_dir", first.root.parent)
        self.root, self.evidence_dir = first.root, first.evidence_dir
        self.default_branch = getattr(first, "default_branch", self.config.get("branch", ""))
        self.machine_lock = threading.RLock()
        self.peer_managers = list(self.managers)
        self._health = {}
        for manager in self.managers:
            manager.machine_lock = self.machine_lock
            manager.peer_managers = self.peer_managers

    def _provider_managers(self):
        # An unextended legacy composite keeps its original provider set.
        names = ("codex", "claude")
        if self.flagscale is not None or self.flagscale_settings is not None or "flagscale" in self.providers_error:
            names += ("flagscale",)
        return tuple((name, getattr(self, name)) for name in names)

    @staticmethod
    def _provider(payload):
        provider = payload.get("provider", "codex")
        if provider not in ("codex", "claude", "flagscale"):
            raise SessionError("Agent provider must be codex, claude or flagscale")
        return provider

    def _manager(self, provider):
        manager = {"codex": self.codex, "claude": self.claude, "flagscale": self.flagscale}.get(provider)
        if manager is None:
            raise SessionError(f"The {provider} session provider is unavailable")
        return manager

    def _settings_store(self, provider):
        if provider not in ("claude", "flagscale"):
            raise SessionError("Agent settings provider must be claude or flagscale")
        store = self.agent_settings if provider == "claude" else self.flagscale_settings
        if store is None:
            raise SessionError(f"Protected {provider} settings are unavailable")
        return store

    def settings(self, provider="claude"):
        return safe_agent_settings(self._settings_store(provider).snapshot())

    def update_settings(self, payload):
        provider = payload.get("provider", "claude")
        return safe_agent_settings(self._settings_store(provider).update(payload))

    def _profile(self, profile_id):
        # Codex owns existing profiles and remains the preferred editable store.
        for provider, manager in self._provider_managers():
            if manager is None:
                continue
            for profile in manager.snapshot(compact=True).get("machine_profiles", []):
                if profile.get("id") == profile_id:
                    return provider, copy.deepcopy(profile)
        raise SessionError("Unknown machine profile")

    def _prepare_profile(self, payload, provider):
        value = dict(payload)
        if not value.get("profile_id"):
            return value
        source, profile = self._profile(value["profile_id"])
        if source != provider:
            # Only machine configuration and key *paths* are copied. save_profile
            # writes an owner-local ssh.conf; it never reads the private-key bytes.
            allowed = {"id", "label", "backend", "ssh_alias", "ssh_connection", "remote_workspace", "model_paths",
                       "role", "hardware", "source", "backend_source", "notes"}
            self._manager(provider).save_profile({key: item for key, item in profile.items() if key in allowed})
        return value

    def save_profile(self, payload):
        # Machine profiles are independent of agent credentials and permissions.
        manager = self.codex or self.claude or self.flagscale
        return manager.save_profile({key: value for key, value in payload.items() if key != "provider"})

    def check_machine(self, payload):
        provider = self._provider(payload)
        with self.machine_lock:
            value = self._prepare_profile(payload, provider)
            return self._manager(provider).check_machine({key: item for key, item in value.items() if key != "provider"})

    def create(self, payload):
        provider = self._provider(payload)
        with self.machine_lock:
            value = self._prepare_profile(payload, provider)
            if provider == "codex":
                # Older loopback owners predate the provider field. They must
                # never receive a request intended for another executor.
                value.pop("provider", None)
            else:
                value["provider"] = provider
            result = self._manager(provider).create(value)
            return {**result, "provider": provider}

    def allocation_sessions(self):
        rows = []
        for provider, manager in self._provider_managers():
            if manager is not None:
                # Deliberately fail closed when any live owner is unavailable.
                # A stale cache cannot safely release its occupied machines.
                rows += [{**row, "provider": provider} for row in self._allocation(manager)]
        return rows

    @staticmethod
    def _allocation(manager):
        method = getattr(manager, "allocation_sessions", None)
        return method() if method is not None else manager.snapshot(compact=True).get("sessions", [])

    def _owner(self, session_id):
        if not isinstance(session_id, str) or not re.fullmatch(r"[0-9a-f]{32}", session_id):
            raise SessionError("Invalid dashboard session ID; native tasks are observation-only")
        owners, unavailable = [], False
        for provider, manager in self._provider_managers():
            if manager is None:
                continue
            try:
                if any(row.get("id") == session_id for row in self._allocation(manager)):
                    owners.append((provider, manager))
            except Exception:
                unavailable = True
        if not owners:
            raise SessionError("Session owner lookup is unavailable" if unavailable else "Unknown dashboard session")
        if len(owners) != 1:
            raise SessionError("Dashboard session ownership is ambiguous")
        return owners[0]

    def _control(self, action, session_id, *args, **kwargs):
        with self.machine_lock:
            provider, manager = self._owner(session_id)
            result = getattr(manager, action)(session_id, *args, **kwargs)
            return {**result, "provider": provider} if isinstance(result, dict) else result

    def start(self, session_id):
        return self._control("start", session_id)

    def message(self, session_id, text):
        return self._control("message", session_id, text)

    def interrupt(self, session_id):
        return self._control("interrupt", session_id)

    def respond(self, session_id, request_id, response):
        return self._control("respond", session_id, request_id, response)

    def event_history(self, session_id, cursor=None, limit=80):
        provider, manager = self._owner(session_id)
        return {**manager.event_history(session_id, cursor=cursor, limit=limit), "provider": provider}

    def _provider_status(self, provider, snapshot):
        health = self._health.get(provider) or snapshot.get("health") or {}
        result = {"provider": provider, "available": bool(snapshot.get("available", False)), **health}
        if provider in ("claude", "flagscale"):
            try:
                public = self.settings(provider)
                result.update(configured=public.get("configured", False), model=public.get("model"))
                if provider == "claude":
                    result["permission_mode"] = public.get("permission_mode")
                else:
                    result.update({key: value for key, value in public.items()
                                   if key in ("llm_provider", "base_url", "api_protocol")})
            except SessionError:
                result.update(configured=False, error=f"Protected {provider.title()} settings are unavailable")
        if provider == "flagscale":
            result.update(FLAGSCALE_CAPABILITIES)
            result["capabilities"] = {**(result.get("capabilities") or {}), **FLAGSCALE_CAPABILITIES}
        if provider in self.providers_error:
            result.update(available=False, error=self.providers_error[provider])
        return result

    def snapshot(self, compact=False):
        snapshots, errors = {}, dict(self.providers_error)
        for provider, manager in self._provider_managers():
            if manager is not None:
                try:
                    snapshots[provider] = manager.snapshot(compact=compact)
                except Exception:
                    errors[provider] = f"The {provider} session owner is unavailable"
        result = copy.deepcopy(snapshots.get("codex") or snapshots.get("claude") or snapshots.get("flagscale") or {})
        result["sessions"] = [{**row, "provider": provider}
                              for provider, snapshot in snapshots.items() for row in snapshot.get("sessions", [])]
        result["sessions"].sort(key=lambda row: str(row.get("created_at", "")), reverse=True)
        profiles = {}
        for provider, _ in self._provider_managers():
            for profile in snapshots.get(provider, {}).get("machine_profiles", []):
                profiles.setdefault(profile["id"], copy.deepcopy(profile))
        result["machine_profiles"] = list(profiles.values())
        result["providers"] = {provider: self._provider_status(provider, snapshots.get(provider, {}))
                               for provider, _ in self._provider_managers()}
        for provider, error in errors.items():
            result["providers"][provider].update(available=False, error=error)
        try:
            result["agent_settings"] = self.settings()
        except SessionError:
            result["agent_settings"] = {"provider": "claude", "configured": False, "error": "Protected settings are unavailable"}
        result["agent_settings_by_provider"] = {"claude": copy.deepcopy(result["agent_settings"])}
        try:
            result["agent_settings_by_provider"]["flagscale"] = self.settings("flagscale")
        except SessionError:
            result["agent_settings_by_provider"]["flagscale"] = {"provider": "flagscale", "configured": False}
        result["provider_errors"] = errors
        return result

    def providers(self):
        return self.snapshot(compact=True)["providers"]

    def health(self):
        health = {}
        for provider, manager in self._provider_managers():
            if manager is not None:
                try:
                    health[provider] = manager.health()
                except Exception:
                    health[provider] = {"provider": provider, "available": False, "error": f"The {provider} health check is unavailable"}
            else:
                health[provider] = {"provider": provider, "available": False, "error": self.providers_error.get(provider, "Provider unavailable")}
        if "flagscale" in health:
            health["flagscale"].update(FLAGSCALE_CAPABILITIES)
            health["flagscale"]["capabilities"] = {
                **(health["flagscale"].get("capabilities") or {}), **FLAGSCALE_CAPABILITIES}
        self._health = health
        return {**health["codex"], "providers": copy.deepcopy(health)}

    def close(self):
        # Remote proxies close nothing; local managers own only their own tasks.
        for manager in self.managers:
            manager.close()
