"""Recover monitor remote renames without choosing an unrelated fork."""
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from fl_dashboard.monitoring.branch_monitor import BranchMonitor, MonitorError


class MonitorRemoteAliasTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.repo = Path(self.temp.name)
        self.git("init", "-q")
        self.git("config", "user.name", "Test")
        self.git("config", "user.email", "test@example.invalid")
        self.git("commit", "--allow-empty", "-qm", "fixture")
        self.sha = self.git("rev-parse", "HEAD")
        self.monitor = BranchMonitor({"repo": str(self.repo), "remote": "upstream",
                                      "branch": "main", "github_repo": "example/plugin"})

    def git(self, *args):
        return subprocess.run(["git", "-C", str(self.repo), *args], check=True,
                              capture_output=True, text=True).stdout.strip()

    def remote(self, name, url, branch="main"):
        self.git("remote", "add", name, url)
        self.git("update-ref", f"refs/remotes/{name}/{branch}", self.sha)

    def test_clone_url_identity_accepts_https_and_ssh_without_path_confusion(self):
        for url in ("https://github.com/example/plugin.git", "https://github.com/example/plugin",
                    "git@github.com:example/plugin.git", "ssh://git@github.com/example/plugin.git",
                    "https://github.com:443/Example/Plugin.git", "ssh://git@github.com:22/example/plugin"):
            with self.subTest(url=url):
                self.assertEqual(self.monitor._github_remote_repo(url), "example/plugin")
        for url in ("https://github.com.attacker.invalid/example/plugin.git",
                    "https://github.com@example.invalid/example/plugin.git",
                    "https://github.com/example/plugin/extra", "https://github.com/example/plugin?other",
                    "https://github.com/example/plugin#other", "http://github.com/example/plugin",
                    "file:///example/plugin", "ssh://git@github.com:2222/example/plugin",
                    "git@github.com:other/plugin.git", "https://github.com/example/plugin-extra.git",
                    "https://github.com/example/%70lugin.git"):
            with self.subTest(url=url):
                self.assertNotEqual(self.monitor._github_remote_repo(url), "example/plugin")

    def test_existing_explicit_remote_keeps_configured_behavior(self):
        self.remote("upstream", "https://example.invalid/private/configured.git")
        self.remote("origin", "https://github.com/example/plugin.git")
        self.assertEqual(self.monitor._resolved_remote(), "upstream")
        self.assertEqual(self.monitor._head("main"), ("refs/remotes/upstream/main", self.sha))

    def test_missing_alias_recovers_only_matching_repository_and_branches(self):
        self.remote("fork", "https://github.com/another/plugin.git", branch="fork-only")
        self.remote("origin", "git@github.com:example/plugin.git")
        self.git("update-ref", "refs/remotes/origin/release", self.sha)
        # Orphaned old refs must not win over the verified replacement alias.
        self.git("update-ref", "refs/remotes/upstream/stale", self.sha)
        before = self.git("config", "--local", "--list")
        self.assertEqual(self.monitor._head("main"), ("refs/remotes/origin/main", self.sha))
        self.assertEqual(self.monitor._branches(), ["main", "release"])
        self.assertEqual(self.git("config", "--local", "--list"), before)
        self.assertEqual(self.monitor.remote, "upstream")

    def test_running_monitor_recovers_after_remote_is_renamed(self):
        self.remote("upstream", "https://github.com/example/plugin.git")
        self.assertEqual(self.monitor._head("main")[0], "refs/remotes/upstream/main")
        self.git("remote", "rename", "upstream", "origin")
        self.assertEqual(self.monitor._head("main")[0], "refs/remotes/origin/main")
        self.assertEqual(self.monitor._branches(), ["main"])

    def test_refresh_fetches_matching_alias_into_its_own_tracking_ref(self):
        self.remote("origin", "https://github.com/example/plugin.git")
        commands = []
        original = self.monitor._git
        def record(*args, **kwargs):
            commands.append(args)
            if args[0] == "fetch":
                return subprocess.CompletedProcess(args, 0, "", "")
            return original(*args, **kwargs)
        def snapshot(branch=None, force=False):
            ref, sha = self.monitor._head(branch)
            return {"branch": branch, "branch_ref": ref, "head": sha,
                    "branches": self.monitor._branches(), "errors": []}
        with patch.object(self.monitor, "_git", side_effect=record), patch.object(self.monitor, "status", side_effect=snapshot):
            result = self.monitor.refresh_branch()
        self.assertIn(("fetch", "--no-tags", "origin", "+refs/heads/main:refs/remotes/origin/main"), commands)
        self.assertEqual(result["branch_ref"], "refs/remotes/origin/main")
        self.assertEqual(result["errors"], [])

    def test_refresh_fetches_before_reading_a_missing_replacement_tracking_ref(self):
        self.git("remote", "add", "origin", "https://github.com/example/plugin.git")
        original = self.monitor._git
        def fetch(*args, **kwargs):
            if args[0] == "fetch":
                self.assertEqual(args[2], "origin")
                self.git("update-ref", "refs/remotes/origin/main", self.sha)
                return subprocess.CompletedProcess(args, 0, "", "")
            return original(*args, **kwargs)
        def snapshot(branch=None, force=False):
            ref, sha = self.monitor._head(branch)
            return {"branch": branch, "branch_ref": ref, "head": sha, "errors": []}
        with patch.object(self.monitor, "_git", side_effect=fetch), patch.object(self.monitor, "status", side_effect=snapshot):
            result = self.monitor.refresh_branch()
        self.assertEqual(result["head"], self.sha)
        self.assertEqual(result["branch_ref"], "refs/remotes/origin/main")

    def test_ambiguous_matching_aliases_fail_without_guessing(self):
        self.remote("origin", "https://github.com/example/plugin.git")
        self.remote("backup", "git@github.com:example/plugin.git")
        with self.assertRaises(MonitorError) as caught:
            self.monitor._head("main")
        self.assertEqual(caught.exception.code, "remote_ambiguous")
        with self.assertRaises(MonitorError):
            self.monitor.refresh_branch()

    def test_missing_or_unrelated_remotes_fail_without_leaking_url_credentials(self):
        self.remote("origin", "https://dummy-secret@example.invalid/example/plugin.git")
        self.git("update-ref", "refs/remotes/upstream/main", self.sha)
        with self.assertRaises(MonitorError) as caught:
            self.monitor._head("main")
        self.assertEqual(caught.exception.code, "remote_missing")
        self.assertNotIn("dummy-secret", str(caught.exception))
        self.assertNotIn("example.invalid", str(caught.exception))

    def test_unconfigured_github_repo_does_not_assume_origin(self):
        self.remote("origin", "https://github.com/example/plugin.git")
        self.monitor.github_repo = ""
        with self.assertRaises(MonitorError) as caught:
            self.monitor._branches()
        self.assertEqual(caught.exception.code, "remote_missing")

    def test_automatic_offline_read_keeps_refs_but_reports_incomplete_refresh(self):
        self.git("update-ref", "refs/remotes/upstream/main", self.sha)
        self.monitor._auto_refresh_times.clear()
        with patch.object(self.monitor, "_cases", return_value=[]), \
                patch.object(self.monitor, "_registry", return_value={}), \
                patch.object(self.monitor, "_branch_ci", return_value=({}, [])), \
                patch.object(self.monitor, "_version", return_value=None):
            result = self.monitor.status()
        self.assertEqual(result["head"], self.sha)
        self.assertEqual(result["errors"], ["Git remote is unavailable; showing local tracking refs"])

    def test_offline_tracking_refs_remain_readable_but_cannot_refresh(self):
        self.git("update-ref", "refs/remotes/upstream/main", self.sha)
        self.assertEqual(self.monitor._head("main"), ("refs/remotes/upstream/main", self.sha))
        self.assertEqual(self.monitor._branches(), ["main"])
        with self.assertRaises(MonitorError) as caught:
            self.monitor.refresh_branch()
        self.assertEqual(caught.exception.code, "remote_missing")


if __name__ == "__main__":
    unittest.main()
