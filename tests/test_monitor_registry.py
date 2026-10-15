import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch
from fl_dashboard.monitoring import registry as adapters
from fl_dashboard.projects.project_registry import ProjectRegistry


class ExampleMonitor:
    def __init__(self, config):
        self.config = config
        self.branch = config["branch"]
        self.github_repo = config.get("github_repo", "")

    def status(self, branch=None, force=False):
        return {"engine": self.config["engine"], "branch": branch or self.branch,
                "branches": [self.branch], "backends": [], "models": [], "modes": [],
                "head": "a"*40, "errors": []}

    def refresh_branch(self, branch=None):
        return self.status(branch)


class MonitorRegistryTests(unittest.TestCase):
    def test_custom_project_loads_without_plugin_task_executors(self):
        with patch.dict(adapters._TYPES), tempfile.TemporaryDirectory() as directory:
            adapters.register_monitor("example", ExampleMonitor)
            root=Path(directory)
            (root/"monitor.json").write_text(json.dumps({"engine":"example","branch":"main","tasks_enabled":True}))
            (root/"projects.json").write_text(json.dumps({"projects":[{"id":"third","label":"Third project","monitor_config":"monitor.json"}]}))
            forbidden=Mock(side_effect=AssertionError("Custom monitor must not start plugin executors"))
            registry=ProjectRegistry.from_config(root/"projects.json",sessions_factory=forbidden,
                claude_sessions_factory=forbidden,flagscale_sessions_factory=forbidden,
                settings_factory=lambda path:None,flagscale_settings_factory=lambda path:None)
            try:
                self.assertIsInstance(registry.projects[0].monitor,ExampleMonitor)
                self.assertEqual(registry.statuses()["projects"][0]["status"]["engine"],"example")
                self.assertFalse(registry.projects[0].metadata()["sessions"])
                forbidden.assert_not_called()
            finally:
                registry.close()

    def test_registration_cannot_replace_builtin_or_import_a_config_path(self):
        for name in ("vllm","some.module:Factory","../module"):
            with self.subTest(name=name), self.assertRaises(ValueError):
                adapters.register_monitor(name,ExampleMonitor)
        with self.assertRaises(ValueError):
            adapters.create_monitor({"engine":"unregistered"})
        with self.assertRaises(ValueError):
            adapters.register_monitor("unsupported_executor",ExampleMonitor,supports_tasks=True)

    def test_public_cache_accepts_custom_snapshot(self):
        from fl_dashboard.services.share_server import SnapshotCache
        monitor=ExampleMonitor({"engine":"example","branch":"main"})
        cache=SnapshotCache(monitor,refresh_interval=0)
        try:
            self.assertEqual(cache.get()["engine"],"example")
            self.assertEqual(cache.refresh()["branch"],"main")
        finally:
            cache.close()
