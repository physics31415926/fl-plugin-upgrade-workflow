import contextlib
import io
import json
import os
from pathlib import Path
import shutil
import shlex
import subprocess
import tempfile
import unittest
from unittest.mock import patch

import configure


@unittest.skipUnless(shutil.which("git"), "Git is required for checkout fixtures")
class ConfigureTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.app = self.root / "dashboard app"
        self.app.mkdir()
        self.git(self.app, "init", "-q")
        (self.app / ".gitignore").write_text("runs/\nprojects.json\nbranch-monitor.json\nbranch-monitor.sglang.json\n", encoding="utf-8")
        for project_id, (_, _, source, _) in configure.PROJECTS.items():
            example = json.loads((configure.APP_ROOT / source).read_text(encoding="utf-8"))
            # Fields that must never flow from an example into new live settings.
            example.update(sessions_api_url="http://127.0.0.1:9999", external_tasks=[{"id": "demo"}],
                           acceptance_records=[{"status": "passed"}], workflow_config="workflow.json",
                           api_key="DEMONSTRATION_ONLY", ssh_profiles={"demo": {}},
                           case_dir="examples" if project_id == "sglang" else "tools/adaptation-gate-cases",
                           case_sources=["examples"] if project_id == "sglang" else ["tools/adaptation-gate-cases"])
            (self.app / source).write_text(json.dumps(example), encoding="utf-8")
        (self.app / "flflow.py").write_text("# Fixture launcher target\n", encoding="utf-8")
        self.git(self.app, "add", ".")
        self.git(self.app, "commit", "-qm", "Generic fixture")
        self.repos = {}
        for project_id in configure.PROJECTS:
            repo = self.root / (project_id + " plugin checkout")
            repo.mkdir()
            self.git(repo, "init", "-q")
            (repo / "source.txt").write_text("fixture\n", encoding="utf-8")
            self.git(repo, "add", "source.txt")
            self.git(repo, "commit", "-qm", "Generic fixture")
            self.git(repo, "branch", "-M", "target")
            self.git(repo, "remote", "add", "origin", f"https://github.com/fork/{project_id}-plugin.git")
            self.git(repo, "remote", "add", "upstream", f"https://github.com/upstream/{project_id}-plugin.git")
            self.git(repo, "update-ref", "refs/remotes/upstream/target", "HEAD")
            self.git(repo, "update-ref", "refs/remotes/origin/target", "HEAD")
            self.repos[project_id] = repo

    def git(self, repo, *arguments):
        result = subprocess.run(
            ["git", "-c", f"safe.directory={repo.as_posix()}", "-c", "core.hooksPath=",
             "-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid",
             "-C", str(repo), *arguments], capture_output=True, text=True,
             encoding="utf-8", errors="replace", timeout=15,
        )
        self.assertEqual(0, result.returncode, result.stderr)
        return result.stdout.strip()

    def args(self, *extra):
        base = ["--non-interactive", "--vllm-repo", str(self.repos["vllm"]),
                "--sglang-repo", str(self.repos["sglang"])]
        return configure.parser().parse_args([*base, *extra])

    def generate(self, *extra):
        with patch.object(configure, "cli_path", return_value=None):
            paths, port = configure.configure(self.args(*extra), app_root=self.app)
        return {path.name: json.loads(path.read_text(encoding="utf-8")) for path in paths}, port

    def test_clean_dual_defaults_and_distinct_runtime_directories(self):
        documents, port = self.generate()
        self.assertEqual(8766, port)
        self.assertEqual(["vllm", "sglang"], [row["id"] for row in documents["projects.json"]["projects"]])
        denied = {"backend_prs", "backend_pr_discovery", "accepted_ci_runs", "merged_commits",
                  "backend_task_policies", "external_tasks", "acceptance_records", "sessions_api_url",
                  "workflow_config", "api_key", "ssh_profiles"}
        directories = []
        for project_id, (_, _, _, filename) in configure.PROJECTS.items():
            config = documents[filename]
            self.assertEqual(project_id, config["engine"])
            self.assertEqual("upstream", config["remote"])
            self.assertEqual("target", config["branch"])
            self.assertEqual(f"upstream/{project_id}-plugin", config["github_repo"])
            self.assertEqual(str(self.repos[project_id]), config["repo"])
            self.assertFalse(denied.intersection(config))
            self.assertTrue(config["models"])
            self.assertTrue(config["modes"])
            self.assertEqual("iluvatar", config["backend_ci_platforms"]["corex"])
            self.assertIn("cuda", config["backend_labels"])
            directories.extend(config[key] for key in ("sessions_dir", "claude_sessions_dir", "evidence_dir"))
        self.assertEqual(6, len(set(directories)))
        launch = documents["launch.json"]
        self.assertEqual(str(self.app), launch["working_directory"])
        self.assertIn(str(self.app / "flflow.py"), launch["arguments"])
        self.assertIn(str(self.app / "runs" / "configured" / "projects.json"), launch["arguments"])
        self.assertEqual(["--host", "127.0.0.1", "--port", "8766"], launch["arguments"][-4:])
        self.assertTrue(Path(launch["python"]).is_absolute())
        self.assertTrue(Path(launch["log_file"]).is_absolute())

    def test_generated_configuration_loads_read_only_registry(self):
        self.generate()
        from project_registry import ProjectRegistry
        registry = ProjectRegistry.from_config(self.app / "runs" / "configured" / "projects.json", read_only=True)
        try:
            self.assertEqual(["vllm", "sglang"], [row["id"] for row in registry.metadata()["projects"]])
            self.assertTrue(all(project.monitor is not None for project in registry.projects))
            self.assertTrue(all(project.sessions is None for project in registry.projects))
        finally:
            registry.close()

    def test_engine_case_defaults_are_explicit_when_examples_omit_them(self):
        for project_id, (_, _, source, _) in configure.PROJECTS.items():
            template_path = self.app / source
            template = json.loads(template_path.read_text(encoding="utf-8"))
            template.pop("case_dir")
            template.pop("case_sources")
            template_path.write_text(json.dumps(template), encoding="utf-8")
        documents, _ = self.generate()
        self.assertEqual("examples", documents["branch-monitor.sglang.json"]["case_dir"])
        self.assertEqual(["examples", "tests/e2e_tests/concurrent"], documents["branch-monitor.sglang.json"]["case_sources"])
        self.assertEqual("tools/adaptation-gate-cases", documents["branch-monitor.json"]["case_dir"])
        self.assertEqual(["tools/adaptation-gate-cases"], documents["branch-monitor.json"]["case_sources"])

    def test_single_project_overrides_and_non_github_remote(self):
        self.git(self.repos["sglang"], "remote", "set-url", "origin", "https://example.invalid/plugin.git")
        self.git(self.repos["sglang"], "update-ref", "refs/remotes/origin/dev/custom", "HEAD")
        documents, port = self.generate("--projects", "sglang", "--sglang-remote", "origin",
                                       "--sglang-branch", "dev/custom", "--sglang-models", "Example Model",
                                       "--sglang-modes", "eager", "graph", "--port", "9000")
        self.assertNotIn("branch-monitor.json", documents)
        config = documents["branch-monitor.sglang.json"]
        self.assertEqual("", config["github_repo"])
        self.assertEqual("dev/custom", config["branch"])
        self.assertEqual(["Example Model"], config["models"])
        self.assertEqual(["eager", "graph"], config["modes"])
        self.assertEqual(9000, port)
        self.assertNotIn("workflow_config", documents["projects.json"]["projects"][0])

    def test_remote_credentials_are_not_saved_or_printed(self):
        self.git(self.repos["vllm"], "remote", "set-url", "upstream", "https://DUMMY_CONFIGURE_TOKEN@github.com/new-owner/new-repo.git")
        documents, _ = self.generate("--projects", "vllm")
        serialized = json.dumps(documents)
        self.assertEqual("new-owner/new-repo", documents["branch-monitor.json"]["github_repo"])
        self.assertNotIn("DUMMY_CONFIGURE_TOKEN", serialized)
        self.assertNotIn("https://", serialized)
        for url, expected in [("git@github.com:owner/repo.git", "owner/repo"),
                              ("ssh://git@github.com/owner/repo.git", "owner/repo"),
                              ("https://github.com/owner/repo.git?token=DUMMY", "owner/repo"),
                              ("https://github.com.example.invalid/owner/repo", ""),
                              ("https://github.com/too/many/segments", "")]:
            self.assertEqual(expected, configure.github_slug(url))

    def test_explicit_github_override_and_disable(self):
        documents, _ = self.generate("--vllm-github-repo", "explicit/plugin", "--sglang-github-repo", "")
        self.assertEqual("explicit/plugin", documents["branch-monitor.json"]["github_repo"])
        self.assertEqual("", documents["branch-monitor.sglang.json"]["github_repo"])

    def test_missing_or_wrong_remote_ref_never_fetches_or_writes(self):
        with self.assertRaisesRegex(configure.ConfigurationError, "never fetches"):
            self.generate("--vllm-branch", "missing")
        self.assertFalse((self.app / "runs").exists())
        self.git(self.repos["vllm"], "update-ref", "refs/remotes/origin/only-origin", "HEAD")
        with self.assertRaisesRegex(configure.ConfigurationError, "absent from local refs"):
            self.generate("--vllm-branch", "only-origin")
        with self.assertRaisesRegex(configure.ConfigurationError, "remote does not exist"):
            self.generate("--vllm-remote", "absent")

    def test_ambiguous_branch_requires_explicit_choice(self):
        repo = self.repos["vllm"]
        self.git(repo, "update-ref", "-d", "refs/remotes/upstream/target")
        self.git(repo, "update-ref", "refs/remotes/upstream/other", "HEAD")
        self.git(repo, "update-ref", "refs/remotes/upstream/another", "HEAD")
        with self.assertRaisesRegex(configure.ConfigurationError, "--vllm-branch"):
            self.generate()
        self.git(repo, "symbolic-ref", "refs/remotes/upstream/HEAD", "refs/remotes/upstream/other")
        documents, _ = self.generate()
        self.assertEqual("other", documents["branch-monitor.json"]["branch"])

    def test_remote_fallback_and_ambiguous_remotes(self):
        self.assertEqual("upstream", configure.default_remote(["origin", "upstream"]))
        self.assertEqual("origin", configure.default_remote(["custom", "origin"]))
        self.assertEqual("custom", configure.default_remote(["custom"]))
        self.assertIsNone(configure.default_remote(["first", "second"]))
        repo = self.repos["vllm"]
        self.git(repo, "remote", "rename", "upstream", "first")
        self.git(repo, "remote", "rename", "origin", "second")
        with self.assertRaisesRegex(configure.ConfigurationError, "--vllm-remote"):
            self.generate()

    def test_no_overwrite_preflight_then_explicit_replace(self):
        output = self.app / "runs" / "configured"
        output.mkdir(parents=True)
        existing = output / "projects.json"
        existing.write_text("original\n", encoding="utf-8")
        with self.assertRaisesRegex(configure.ConfigurationError, "--overwrite"):
            self.generate()
        self.assertEqual("original\n", existing.read_text(encoding="utf-8"))
        self.assertFalse((output / "branch-monitor.json").exists())
        documents, _ = self.generate("--overwrite")
        self.assertEqual(2, len(documents["projects.json"]["projects"]))
        self.assertFalse(list(output.glob(".configure-*.tmp")))

    def test_output_must_be_ignored_or_outside_git(self):
        with self.assertRaisesRegex(configure.ConfigurationError, "not Git-ignored"):
            self.generate("--output-dir", str(self.app / "public-config"))
        self.assertFalse((self.app / "public-config").exists())
        outside = self.root / "private settings"
        documents, _ = self.generate("--output-dir", str(outside))
        self.assertTrue((outside / "launch.json").is_file())
        self.assertEqual(4, len(documents))

    def test_output_git_failure_fails_closed_and_trusts_checkout_root(self):
        child = self.app / "existing" / "nested"
        child.mkdir(parents=True)
        with patch.object(configure.subprocess, "run", return_value=subprocess.CompletedProcess([], 128, "", "dubious ownership")) as run:
            with self.assertRaisesRegex(configure.ConfigurationError, "Git metadata check failed"):
                configure.private_targets([child / "projects.json", child / "launch.json"])
        command = run.call_args.args[0]
        self.assertIn(f"safe.directory={self.app.as_posix()}", command)
        self.assertEqual(str(self.app), command[command.index("-C") + 1])
        self.assertEqual(self.repos["vllm"], configure.checkout(self.repos["vllm"] / ".git"))

    def test_remote_names_must_also_be_valid_for_agent_provider(self):
        for name in ("team/origin", "x" * 101):
            with self.assertRaisesRegex(configure.ConfigurationError, "agent compatibility"):
                configure.validate_remote(name, [name])
        self.assertEqual("x" * 100, configure.validate_remote("x" * 100, ["x" * 100]))

    def test_tracked_configuration_is_never_overwritten(self):
        output = self.app / "runs" / "tracked"
        output.mkdir(parents=True)
        target = output / "projects.json"
        target.write_text("tracked\n", encoding="utf-8")
        self.git(self.app, "add", "-f", str(target))
        with self.assertRaisesRegex(configure.ConfigurationError, "tracked configuration"):
            self.generate("--output-dir", str(output), "--overwrite")
        self.assertEqual("tracked\n", target.read_text(encoding="utf-8"))

    def test_invalid_repo_port_or_project_does_not_write(self):
        non_git = self.root / "not a checkout"
        non_git.mkdir()
        with self.assertRaisesRegex(configure.ConfigurationError, "Git working tree"):
            self.generate("--vllm-repo", str(non_git))
        with self.assertRaisesRegex(configure.ConfigurationError, "Port"):
            self.generate("--port", "0")
        with self.assertRaisesRegex(configure.ConfigurationError, "once each"):
            self.generate("--projects", "vllm", "vllm")
        self.assertFalse((self.app / "runs").exists())

    def test_interactive_defaults_and_paths_with_spaces(self):
        args = configure.parser().parse_args([])
        answers = ["", "", "", str(self.repos["vllm"]), "", "", str(self.repos["sglang"]), "", ""]
        with patch("builtins.input", side_effect=answers), patch.object(configure, "cli_path", return_value=None):
            paths, port = configure.configure(args, app_root=self.app)
        self.assertEqual(4, len(paths))
        self.assertEqual(8766, port)
        self.assertTrue(all(path.is_file() for path in paths))

    def test_codex_discovery_does_not_execute_cli(self):
        cli = self.root / "CLI with spaces.exe"
        cli.write_bytes(b"fixture only; never executed")
        cli.chmod(0o700)
        with patch.object(configure.shutil, "which", return_value=str(cli)), patch.object(configure.subprocess, "run") as run:
            self.assertEqual(str(cli), configure.cli_path(None))
            run.assert_not_called()
        with patch.object(configure.shutil, "which", return_value=None), patch.dict(os.environ, {"LOCALAPPDATA": ""}):
            self.assertIsNone(configure.cli_path(None))
            with self.assertRaisesRegex(configure.ConfigurationError, "existing CLI file"):
                configure.cli_path(str(self.root / "missing.exe"))
        args = self.args("--codex-executable", str(cli))
        with patch.object(configure.shutil, "which", return_value=None):
            paths, _ = configure.configure(args, app_root=self.app)
        config = json.loads(next(path for path in paths if path.name == "branch-monitor.json").read_text(encoding="utf-8"))
        self.assertEqual(str(cli), config["codex_executable"])

    def test_automatic_cli_discovery_does_not_pin_a_version_path(self):
        versioned = self.root / "Codex" / "bin" / "aaaaaaaaaaaaaaaa" / "codex.exe"
        with patch.object(configure, "cli_path", return_value=str(versioned)) as discover:
            paths, _ = configure.configure(self.args(), app_root=self.app)
        discover.assert_not_called()
        for path in paths:
            if path.name.startswith("branch-monitor"):
                config = json.loads(path.read_text(encoding="utf-8"))
                self.assertNotIn("codex_executable", config)

    def test_posix_display_command_preserves_shell_metacharacters(self):
        command = ["python", "-B", "start_dashboard.py", "--config", "/private/$USER;folder with spaces/launch.json"]
        with patch.object(configure.os, "name", "posix"):
            rendered = configure.display_command(command)
        self.assertEqual(command, shlex.split(rendered))
        self.assertIn("'/private/$USER;folder with spaces/launch.json'", rendered)

    def test_main_prints_reviewable_start_commands(self):
        output = self.root / "中文 local settings"
        argv = ["--non-interactive", "--projects", "vllm", "--vllm-repo", str(self.repos["vllm"]), "--output-dir", str(output)]
        stdout = io.StringIO()
        with patch.object(configure, "cli_path", return_value=None), contextlib.redirect_stdout(stdout):
            self.assertEqual(0, configure.main(argv))
        text = stdout.getvalue()
        self.assertIn("python -B start_dashboard.py --config", text)
        if os.name == "nt":
            self.assertIn("start_local_dashboard.ps1", text)
        else:
            self.assertNotIn("start_local_dashboard.ps1", text)
        self.assertIn("http://127.0.0.1:8766/", text)
        self.assertNotIn("DUMMY_CONFIGURE_TOKEN", text)
        self.assertTrue((output / "launch.json").is_file())


if __name__ == "__main__":
    unittest.main()
