import contextlib
import copy
import io
import importlib.util
import json
from pathlib import Path
import shutil
import socket
import subprocess
import unittest
from unittest.mock import Mock, patch

import fl_dashboard.deployment.start_dashboard as start_dashboard
# Load the adjacent fixture by filename: an installed top-level "tests"
# package must not shadow this repository during unittest discovery.
_fixture_spec = importlib.util.spec_from_file_location("dashboard_configure_fixtures", Path(__file__).with_name("test_configure.py"))
config_tests = importlib.util.module_from_spec(_fixture_spec)
_fixture_spec.loader.exec_module(config_tests)


@unittest.skipUnless(shutil.which("git"), "Git is required for checkout fixtures")
class StartDashboardTests(unittest.TestCase):
    def setUp(self):
        self.fixture = config_tests.ConfigureTests("test_clean_dual_defaults_and_distinct_runtime_directories")
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.app = self.fixture.app
        self.documents, self.port = self.fixture.generate()
        self.output = self.app / "runs" / "configured"
        self.launch = self.output / "launch.json"
        self.projects = self.output / "projects.json"

    def args(self, *extra):
        return start_dashboard.parser().parse_args(["--config", str(self.launch), *extra])

    def write_launch(self, value):
        self.launch.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")

    def test_fresh_generated_launch_and_read_only_repository_checks(self):
        python, projects, port = start_dashboard.load_launch(self.launch, app_root=self.app)
        self.assertTrue(python.is_file())
        self.assertEqual(self.projects, projects)
        self.assertEqual(8766, port)
        factory = start_dashboard.ProjectRegistry.from_config
        with patch.object(start_dashboard.ProjectRegistry, "from_config", wraps=factory) as called:
            start_dashboard.check_projects(projects)
        called.assert_called_once_with(projects, read_only=True)
        # A launcher check must verify the configured remote target rather than
        # silently fall back to the checkout's local HEAD.
        self.fixture.git(self.fixture.repos["vllm"], "update-ref", "-d", "refs/remotes/upstream/target")
        with self.assertRaisesRegex(start_dashboard.StartupError, "target refs"):
            start_dashboard.check_projects(projects)

    def test_invalid_argv_host_working_directory_python_and_project_paths(self):
        base = self.documents["launch.json"]
        variants = []
        for index, replacement in [(0, "-c"), (2, str(self.app / "unrelated.py")),
                                   (3, "exec"), (6, "--trusted-ingress"), (7, "0.0.0.0"),
                                   (9, "0"), (9, "not-a-port"), (5, "projects.json"),
                                   (5, str(self.output / "missing.json"))]:
            value = copy.deepcopy(base)
            value["arguments"][index] = replacement
            variants.append(value)
        for field, value in [("arguments", base["arguments"] + ["--anonymous-lan"]),
                             ("arguments", "shell command"), ("working_directory", str(self.app.parent)),
                             ("python", "python"), ("python", str(self.app / "missing.exe"))]:
            item = copy.deepcopy(base)
            item[field] = value
            variants.append(item)
        item = copy.deepcopy(base)
        item["arguments"][5] += "\n--unsafe"
        variants.append(item)
        for index, value in enumerate(variants):
            with self.subTest(index=index):
                self.write_launch(value)
                with self.assertRaises(start_dashboard.StartupError):
                    start_dashboard.load_launch(self.launch, app_root=self.app)

    def test_missing_malformed_and_nonobject_launch_files(self):
        for text in ("{malformed", "[]", "null"):
            self.launch.write_text(text, encoding="utf-8")
            with self.assertRaises(start_dashboard.StartupError):
                start_dashboard.load_launch(self.launch, app_root=self.app)
        with self.assertRaisesRegex(start_dashboard.StartupError, "Run python -B scripts/configure.py"):
            start_dashboard.load_launch(self.output / "missing.json", app_root=self.app)
        self.launch.write_text(json.dumps(self.documents["launch.json"]), encoding="utf-8-sig")
        self.assertEqual(8766, start_dashboard.load_launch(self.launch, app_root=self.app)[2])

    def test_prepare_local_command_and_port_override(self):
        with patch.object(start_dashboard, "check_projects") as projects, patch.object(start_dashboard, "check_port") as port:
            command, url = start_dashboard.prepare(self.args("--port", "9090"), app_root=self.app)
        self.assertEqual(str(self.app / "scripts" / "flflow.py"), command[3])
        self.assertEqual(["serve", "--projects-config", str(self.projects)], command[4:7])
        self.assertEqual(["--host", "127.0.0.1", "--port", "9090"], command[-4:])
        self.assertEqual("http://127.0.0.1:9090/", url)
        projects.assert_called_once_with(self.projects)
        port.assert_called_once_with("127.0.0.1", 9090)

    def test_check_only_validates_without_spawning_any_service(self):
        original_prepare = start_dashboard.prepare
        stdout = io.StringIO()
        with patch.object(start_dashboard, "prepare", side_effect=lambda args: original_prepare(args, app_root=self.app)), \
                patch.object(start_dashboard, "check_projects") as projects, \
                patch.object(start_dashboard, "check_port") as port, \
                patch.object(start_dashboard.subprocess, "run") as run, \
                contextlib.redirect_stdout(stdout):
            result = start_dashboard.main(["--config", str(self.launch), "--check-only"])
        self.assertEqual(0, result)
        projects.assert_called_once_with(self.projects)
        port.assert_called_once_with("127.0.0.1", 8766)
        run.assert_not_called()
        self.assertIn("No service", stdout.getvalue())

    def test_port_conflict_is_detected_before_startup(self):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as occupied:
            occupied.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            occupied.bind(("127.0.0.1", 0))
            occupied.listen(1)
            port = occupied.getsockname()[1]
            with self.assertRaisesRegex(start_dashboard.StartupError, "unused port"):
                start_dashboard.check_port("127.0.0.1", port)
        for value in (0, -1, 65536, "bad", True):
            with self.assertRaises(start_dashboard.StartupError):
                start_dashboard.port_number(value)

    def test_lan_uses_only_private_https_read_only_server(self):
        cert = self.fixture.root / "TLS certificate.pem"
        key = self.fixture.root / "TLS key.pem"
        args = self.args("--lan", "--lan-ip", "192.168.8.9", "--lan-port", "9443",
                         "--tls-cert", str(cert), "--tls-key", str(key), "--refresh-interval", "0")
        with patch.object(start_dashboard, "load_tls_context", return_value=object()) as tls, \
                patch.object(start_dashboard, "check_projects") as projects, \
                patch.object(start_dashboard, "check_port") as port:
            command, url = start_dashboard.prepare(args, app_root=self.app)
        tls.assert_called_once_with(str(cert), str(key))
        self.assertEqual(str(self.app / "scripts" / "share_server.py"), command[3])
        self.assertNotIn("serve", command)
        self.assertIn("--anonymous-lan", command)
        self.assertEqual(1, command.count("--control-url"))
        self.assertEqual("http://127.0.0.1:8766", command[command.index("--control-url") + 1])
        self.assertNotIn("--access-shares-file", command)
        self.assertNotIn(str(self.app / "scripts" / "flflow.py"), command)
        self.assertEqual("0", command[-1])
        self.assertEqual("https://192.168.8.9:9443/", url)
        projects.assert_called_once_with(self.projects)
        port.assert_called_once_with("192.168.8.9", 9443)

    def test_lan_rejects_missing_tls_public_and_broad_bindings(self):
        for extra in (["--lan"], ["--lan", "--lan-ip", "192.168.8.9"],
                      ["--lan", "--lan-ip", "192.168.8.9", "--tls-cert", "cert.pem"]):
            with self.subTest(extra=extra), self.assertRaisesRegex(start_dashboard.StartupError, "LAN mode requires"):
                start_dashboard.prepare(self.args(*extra), app_root=self.app)
        for host in ("0.0.0.0", "8.8.8.8", "127.0.0.1", "::1", "localhost", "192.0.2.1"):
            args = self.args("--lan", "--lan-ip", host, "--tls-cert", "cert.pem", "--tls-key", "key.pem")
            with self.subTest(host=host), patch.object(start_dashboard, "load_tls_context") as tls:
                with self.assertRaisesRegex(start_dashboard.StartupError, "RFC1918"):
                    start_dashboard.prepare(args, app_root=self.app)
                tls.assert_not_called()

    def test_lan_tls_failure_refresh_and_local_port_flag_are_rejected(self):
        base = ["--lan", "--lan-ip", "192.168.8.9", "--tls-cert", "cert.pem", "--tls-key", "key.pem"]
        with patch.object(start_dashboard, "load_tls_context", side_effect=ValueError("Cannot load TLS certificate/key")):
            with self.assertRaisesRegex(start_dashboard.StartupError, "TLS"):
                start_dashboard.prepare(self.args(*base), app_root=self.app)
        with patch.object(start_dashboard, "load_tls_context", return_value=object()):
            with self.assertRaisesRegex(start_dashboard.StartupError, "Refresh interval"):
                start_dashboard.prepare(self.args(*base, "--refresh-interval", "1"), app_root=self.app)
            with self.assertRaisesRegex(start_dashboard.StartupError, "Use --lan-port"):
                start_dashboard.prepare(self.args(*base, "--port", "9000"), app_root=self.app)

    def test_local_mode_rejects_accidental_lan_options(self):
        for extra in (["--lan-ip", "192.168.8.9"], ["--lan-port", "9443"],
                      ["--tls-cert", "cert.pem"], ["--tls-key", "key.pem"], ["--refresh-interval", "300"]):
            with self.subTest(extra=extra), patch.object(start_dashboard, "check_projects") as projects:
                with self.assertRaisesRegex(start_dashboard.StartupError, "require --lan"):
                    start_dashboard.prepare(self.args(*extra), app_root=self.app)
                projects.assert_not_called()

    def test_access_sharing_uses_matching_store_and_explicit_local_origin(self):
        with patch.object(start_dashboard, "check_projects"), patch.object(start_dashboard, "check_port"), \
                patch.object(start_dashboard, "load_tls_context", return_value=object()):
            local, _ = start_dashboard.prepare(self.args("--enable-access-sharing", "--share-public-url", "https://192.168.8.9:9443"), app_root=self.app)
            lan, _ = start_dashboard.prepare(self.args("--lan", "--lan-ip", "192.168.8.9", "--lan-port", "9443", "--tls-cert", "cert.pem", "--tls-key", "key.pem", "--enable-access-sharing"), app_root=self.app)
        self.assertEqual(local[local.index("--access-shares-file") + 1], lan[lan.index("--access-shares-file") + 1])
        self.assertEqual("https://192.168.8.9:9443", local[local.index("--share-public-url") + 1])
        self.assertEqual("https://192.168.8.9:9443", lan[lan.index("--public-url") + 1])
        self.assertEqual("http://127.0.0.1:8766", lan[lan.index("--control-url") + 1])

    def test_saved_sharing_arguments_are_preserved_by_both_launch_modes(self):
        value = copy.deepcopy(self.documents["launch.json"])
        value["arguments"] += ["--share-public-url", "https://192.168.8.9:9443", "--access-shares-file", str(self.output / "grants.json")]
        self.write_launch(value)
        with patch.object(start_dashboard, "check_projects"), patch.object(start_dashboard, "check_port"), \
                patch.object(start_dashboard, "load_tls_context", return_value=object()):
            local, _ = start_dashboard.prepare(self.args(), app_root=self.app)
            lan, _ = start_dashboard.prepare(self.args("--lan", "--lan-ip", "192.168.8.9", "--lan-port", "9443", "--tls-cert", "cert.pem", "--tls-key", "key.pem"), app_root=self.app)
        self.assertEqual(str(self.output / "grants.json"), local[local.index("--access-shares-file") + 1])
        self.assertEqual(str(self.output / "grants.json"), lan[lan.index("--access-shares-file") + 1])
        self.assertEqual("https://192.168.8.9:9443", lan[lan.index("--public-url") + 1])

    def test_access_sharing_rejects_missing_url_non_https_and_prefix(self):
        for extra in (["--enable-access-sharing"], ["--share-public-url", "https://dashboard.example"],
                      ["--enable-access-sharing", "--share-public-url", "http://dashboard.example"],
                      ["--enable-access-sharing", "--share-public-url", "https://dashboard.example/prefix"]):
            with self.subTest(extra=extra), patch.object(start_dashboard, "check_projects") as projects:
                with self.assertRaises(start_dashboard.StartupError):
                    start_dashboard.prepare(self.args(*extra), app_root=self.app)
                projects.assert_not_called()

    def test_repository_error_closes_registry_and_hides_diagnostics(self):
        project = Mock(id="vllm", monitor_error=None, default_branch="target")
        project.monitor._head.side_effect = RuntimeError("DUMMY_PRIVATE_TOKEN remote diagnostic")
        registry = Mock(projects=[project])
        with patch.object(start_dashboard.ProjectRegistry, "from_config", return_value=registry):
            with self.assertRaises(start_dashboard.StartupError) as error:
                start_dashboard.check_projects(self.projects)
        self.assertNotIn("DUMMY_PRIVATE_TOKEN", str(error.exception))
        registry.close.assert_called_once_with()


if __name__ == "__main__":
    unittest.main()
