"""Version badges must use the selected commit, never the checkout/default branch."""
import subprocess
import tempfile
import unittest
from pathlib import Path

from fl_dashboard.monitoring.branch_monitor import BranchMonitor
from fl_dashboard.services.share_server import sanitize_snapshot


class BranchVersionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.repo = Path(self.temp.name)
        self.git("init", "-q")
        self.git("config", "user.name", "Test")
        self.git("config", "user.email", "test@example.invalid")
        platforms = self.repo / ".github/configs/platforms.yml"
        platforms.parent.mkdir(parents=True)
        platforms.write_text("platforms:\n  cuda:\n    enabled: true\n")
        self.commit_pin("older", "0.24.0")
        self.commit_pin("newer", "0.28.0")
        self.monitor = BranchMonitor({"repo": str(self.repo), "branch": "newer",
            "remote": "upstream", "evidence_dir": str(self.repo / "evidence"),
            "models": ["fixture"], "modes": ["eager"]})

    def git(self, *args):
        return subprocess.check_output(["git", "-C", str(self.repo), *args], stderr=subprocess.PIPE).decode().strip()

    def commit_pin(self, branch, version, engine="vllm"):
        dependency = f'"{engine}[audio]=={version}"' if version else '"unrelated==1.0"'
        (self.repo / "pyproject.toml").write_text(f"[project]\ndependencies = [{dependency}]\n")
        self.git("add", ".")
        self.git("commit", "-qm", "Version fixture")
        self.git("update-ref", "refs/remotes/upstream/" + branch, self.git("rev-parse", "HEAD"))

    def test_versions_follow_selected_branch_and_ignore_worktree_edits(self):
        (self.repo / "pyproject.toml").write_text('[project]\ndependencies = ["vllm==9.9.9"]\n')
        for branch, expected in (("older", "0.24.0"), ("newer", "0.28.0"), ("older", "0.24.0")):
            with self.subTest(branch=branch):
                snapshot = self.monitor.status(branch, force=True)
                self.assertEqual(snapshot["branch"], branch)
                self.assertEqual(snapshot["upstream_version"], expected)
                self.assertEqual(snapshot["vllm_version"], expected)
                self.assertEqual(sanitize_snapshot(snapshot)["upstream_version"], expected)

    def test_missing_pin_does_not_reuse_previous_branch_version(self):
        self.commit_pin("unpinned", None)
        self.assertEqual(self.monitor.status("newer", force=True)["upstream_version"], "0.28.0")
        self.assertIsNone(self.monitor.status("unpinned", force=True)["upstream_version"])

    def test_sglang_uses_its_own_branch_pin(self):
        self.commit_pin("sg-old", "0.5.17", "sglang")
        self.commit_pin("sg-new", "0.5.18", "sglang")
        monitor = BranchMonitor(dict(self.monitor.config, engine="sglang", branch="sg-new"))
        for branch, expected in (("sg-old", "0.5.17"), ("sg-new", "0.5.18")):
            snapshot = monitor.status(branch, force=True)
            self.assertEqual(snapshot["upstream_version"], expected)
            self.assertIsNone(snapshot["vllm_version"])
