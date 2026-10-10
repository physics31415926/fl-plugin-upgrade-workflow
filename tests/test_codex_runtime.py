import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

import codex_runtime
from codex_runtime import resolve_codex_executable


class CodexRuntimeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.local = self.root / "local"
        self.bin = self.local / "OpenAI" / "Codex" / "bin"
        self.windows = patch.object(codex_runtime, "IS_WINDOWS", True)
        self.windows.start()
        self.addCleanup(self.windows.stop)
        self.environment = patch.dict(os.environ, {"LOCALAPPDATA": str(self.local)})
        self.environment.start()
        self.addCleanup(self.environment.stop)
        self.which = patch.object(codex_runtime.shutil, "which", return_value=None)
        self.which_mock = self.which.start()
        self.addCleanup(self.which.stop)

    def executable(self, path, modified=None):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"synthetic CLI fixture; never executed")
        path.chmod(0o700)
        if modified is not None:
            os.utime(path, (modified, modified))
        return str(path.resolve())

    def test_existing_explicit_file_takes_precedence_over_path(self):
        custom = self.executable(self.root / "custom CLI.exe")
        current = self.executable(self.bin / "bbbbbbbbbbbbbbbb" / "codex.exe")
        self.which_mock.return_value = current
        self.assertEqual(custom, resolve_codex_executable(custom))
        self.which_mock.assert_not_called()

    def test_relative_custom_file_uses_the_configured_base_directory(self):
        custom = self.executable(self.root / "tools" / "custom.exe")
        self.assertEqual(custom, resolve_codex_executable("tools/custom.exe", base_dir=self.root))

    def test_missing_custom_path_does_not_switch_programs(self):
        current = self.executable(self.bin / "bbbbbbbbbbbbbbbb" / "codex.exe")
        self.which_mock.return_value = current
        self.assertIsNone(resolve_codex_executable(str(self.root / "custom.exe")))
        self.which_mock.assert_not_called()

    def test_explicit_command_resolves_its_own_path(self):
        custom = self.executable(self.root / "custom.exe")
        self.which_mock.side_effect = lambda name: custom if name == "custom-cli" else None
        self.assertEqual(custom, resolve_codex_executable("custom-cli"))

    def test_automatic_discovery_prefers_a_valid_path_cli(self):
        current = self.executable(self.root / "path" / "codex.exe")
        self.executable(self.bin / "bbbbbbbbbbbbbbbb" / "codex.exe")
        self.which_mock.return_value = current
        self.assertEqual(current, resolve_codex_executable())

    def test_removed_app_version_falls_back_to_current_path(self):
        stale = self.bin / "aaaaaaaaaaaaaaaa" / "codex.exe"
        current = self.executable(self.bin / "bbbbbbbbbbbbbbbb" / "codex.exe")
        self.which_mock.return_value = current
        self.assertEqual(current, resolve_codex_executable(str(stale)))

    def test_removed_app_version_with_stale_path_selects_latest_sibling(self):
        stale = self.bin / "aaaaaaaaaaaaaaaa" / "codex.exe"
        self.which_mock.return_value = str(stale)
        self.executable(self.bin / "bbbbbbbbbbbbbbbb" / "codex.exe", modified=1000)
        latest = self.executable(self.bin / "cccccccccccccccc" / "codex.exe", modified=2000)
        self.assertEqual(latest, resolve_codex_executable(str(stale)))

    def test_automatic_discovery_finds_the_standard_app_root(self):
        current = self.executable(self.bin / "bbbbbbbbbbbbbbbb" / "codex.exe")
        self.assertEqual(current, resolve_codex_executable())

    def test_existing_app_version_remains_an_explicit_override(self):
        old = self.executable(self.bin / "aaaaaaaaaaaaaaaa" / "codex.exe", modified=1000)
        self.executable(self.bin / "bbbbbbbbbbbbbbbb" / "codex.exe", modified=2000)
        self.assertEqual(old, resolve_codex_executable(old))

    def test_only_a_recognized_codex_app_layout_can_scan_siblings(self):
        other = self.root / "Unrelated" / "bin"
        self.executable(other / "bbbbbbbbbbbbbbbb" / "codex.exe")
        self.assertIsNone(resolve_codex_executable(str(other / "aaaaaaaaaaaaaaaa" / "codex.exe")))

    def test_scan_is_shallow_and_ignores_invalid_directory_names(self):
        self.executable(self.bin / "bbbbbbbbbbbbbbbb" / "nested" / "codex.exe")
        self.executable(self.bin / "invalid version name" / "codex.exe")
        self.assertIsNone(resolve_codex_executable())

    def test_symlinked_or_redirected_candidates_are_not_discovered(self):
        candidate = self.executable(self.bin / "bbbbbbbbbbbbbbbb" / "codex.exe")
        with patch.object(codex_runtime, "_redirected", side_effect=lambda path: str(path) == candidate):
            self.assertIsNone(resolve_codex_executable())
        with patch.object(codex_runtime, "_redirected", side_effect=lambda path: path == self.bin):
            self.assertIsNone(resolve_codex_executable())

    def test_windows_shell_shim_is_not_reported_as_native_cli(self):
        shim = self.executable(self.root / "codex.cmd")
        self.which_mock.return_value = shim
        self.assertIsNone(resolve_codex_executable())
        self.assertIsNone(resolve_codex_executable(shim))

    def test_relative_localappdata_is_not_used_as_an_install_root(self):
        with patch.dict(os.environ, {"LOCALAPPDATA": "relative-app-data"}):
            self.assertIsNone(resolve_codex_executable())

    def test_malformed_configurations_fail_without_execution(self):
        with patch.object(subprocess, "Popen") as popen, patch.object(subprocess, "run") as run:
            for value in ("invalid\x00.exe", {"path": "codex.exe"}):
                self.assertIsNone(resolve_codex_executable(value))
            current = self.executable(self.bin / "bbbbbbbbbbbbbbbb" / "codex.exe")
            self.assertEqual(current, resolve_codex_executable())
            popen.assert_not_called()
            run.assert_not_called()

    def test_posix_cli_requires_execute_permission_and_uses_only_path(self):
        cli = self.root / "codex"
        current = self.executable(cli)
        self.which_mock.return_value = current
        with patch.object(codex_runtime, "IS_WINDOWS", False):
            self.assertEqual(current, resolve_codex_executable())
            with patch.object(codex_runtime.os, "access", return_value=False):
                self.assertIsNone(resolve_codex_executable())

    def test_invalid_file_and_missing_cli_report_unavailable(self):
        directory = self.root / "directory.exe"
        directory.mkdir()
        self.assertIsNone(resolve_codex_executable(str(directory)))
        self.assertIsNone(resolve_codex_executable())


if __name__ == "__main__":
    unittest.main()
