import json
from pathlib import Path
import subprocess
import sys
import threading
from unittest import TestCase, mock

from codex_sessions import CodexSessionManager, SessionError
from tests import test_codex_sessions as fixtures

FakeClient = fixtures.FakeClient


class SSHDispatchTest(TestCase):
    setUp = fixtures.SessionsTest.setUp
    tearDown = fixtures.SessionsTest.tearDown
    git = fixtures.SessionsTest.git
    start = fixtures.SessionsTest.start

    def direct_profile(self, **values):
        return self.manager.save_profile({**self.payload, "ssh_alias": "", "label": "Validation host",
            "ssh_connection": {"host": "gpu.example.test", "user": "compute", "port": 2222, **values}})

    def success_output(self, dirty=False):
        return "\n".join(["FLFLOW_PROBE_READY=1", "WORKSPACE=present", "REPOSITORY=present", "COMMIT=" + self.sha,
            "BRANCH=feature/adaptation", "DIRTY=" + ("yes" if dirty else "no"),
            "GATE=present", "TMUX=present", "MODEL_0=present", ""])

    def probe(self, profile, output=None, error="", returncode=0):
        with mock.patch("codex_sessions.shutil.which", return_value="ssh"), mock.patch("codex_sessions.subprocess.run") as run:
            output = output if output is not None else (self.success_output() if returncode == 0 else "")
            run.return_value = subprocess.CompletedProcess([], returncode, output.encode("utf-8"), error.encode("utf-8"))
            result = self.manager.check_machine({"profile_id": profile["id"]})
            return result, run.call_args

    def test_managed_ssh_config_references_key_without_reading_its_content(self):
        key = self.base / "private key.pem"
        key.write_text("PRIVATE TEST CONTENT MUST NOT BE COPIED")
        profile = self.direct_profile(identity_file=str(key), jump_alias="existing_jump")
        self.assertTrue(profile["ssh_alias"].startswith("flflow-"))
        config = Path(profile["ssh_config_path"])
        self.assertTrue(config.is_relative_to(self.manager.root))
        text = config.read_text()
        self.assertIn('IdentityFile "' + key.as_posix() + '"', text)
        self.assertIn("ProxyJump existing_jump", text)
        self.assertIn("Host *\n  BatchMode yes\n  StrictHostKeyChecking yes\nInclude", text)
        self.assertNotIn(key.read_text(), text)
        self.assertNotIn(key.read_text(), (self.manager.root / "machines.json").read_text())
        other = CodexSessionManager(self.config, self.base, FakeClient)
        self.assertEqual(other.snapshot()["machine_profiles"][0]["ssh_connection"]["port"], 2222)
        other.close()

    def test_connection_configuration_rejects_option_and_config_injection(self):
        for values in ({"host": "-oProxyCommand=bad"}, {"host": "gpu\nProxyCommand bad"},
                       {"user": "user;whoami"}, {"user": "user\nIdentityFile secret"},
                       {"port": "22 -oProxyCommand=bad"}, {"port": 65536}, {"port": True},
                       {"jump_alias": "jump,evil"}, {"password": "not-accepted"}, {"identity_file": "./key"}):
            with self.subTest(values=values), self.assertRaises(SessionError):
                self.direct_profile(**values)
        self.assertEqual(self.manager.snapshot()["machine_profiles"], [])

    def test_probe_preserves_host_verification_and_quotes_remote_parameters(self):
        profile = self.direct_profile()
        workspace = "/work/plugin' ; touch /tmp/should-never-run ; '"
        with mock.patch("codex_sessions.shutil.which", return_value="ssh"), mock.patch("codex_sessions.subprocess.run") as run:
            run.return_value = subprocess.CompletedProcess([], 0, self.success_output(dirty=True).encode("utf-8"), b"")
            result = self.manager.check_machine({"profile_id": profile["id"], "remote_workspace": workspace})
        self.assertTrue(result["ok"])
        self.assertTrue(result["remote"]["git_dirty"])
        arguments = run.call_args.args[0]
        self.assertIn("StrictHostKeyChecking=yes", arguments)
        self.assertIn("BatchMode=yes", arguments)
        self.assertIn("-F", arguments)
        self.assertEqual(arguments[-2], profile["ssh_alias"])
        self.assertIn("'\"'\"'", arguments[-1])
        self.assertNotIn(workspace.encode("utf-8"), run.call_args.kwargs["input"])
        self.assertNotIn("shell", run.call_args.kwargs)
        self.assertLessEqual(run.call_args.kwargs["timeout"], 20)

    def test_actual_pipe_receives_lf_script_bytes_without_windows_text_translation(self):
        profile = self.direct_profile()
        real_run = subprocess.run
        received = []
        child = ("import sys,json; script=sys.stdin.buffer.read(); "
                 "print(json.dumps({'cr':script.count(bytes([13])), 'lf':script.count(bytes([10])), "
                 "'ready':b'FLFLOW_PROBE_READY=1' in script}))")

        def receive_pipe(arguments, **options):
            child_result = real_run([sys.executable, "-c", child], **options)
            self.assertEqual(child_result.returncode, 0)
            received.append(json.loads(child_result.stdout.decode("utf-8")))
            return subprocess.CompletedProcess(arguments, 0, self.success_output().encode("utf-8"), b"")

        with mock.patch("codex_sessions.shutil.which", return_value="ssh"), mock.patch("codex_sessions.subprocess.run", side_effect=receive_pipe):
            result = self.manager.check_machine({"profile_id": profile["id"]})
        self.assertTrue(result["ok"])
        self.assertEqual(received[0]["cr"], 0)
        self.assertGreater(received[0]["lf"], 10)
        self.assertTrue(received[0]["ready"])

    def test_remote_probe_failure_is_reported_separately_after_ready_marker(self):
        profile = self.direct_profile()
        result, _ = self.probe(profile, output="FLFLOW_PROBE_READY=1\n", error="shell syntax error raw-secret-token", returncode=2)
        self.assertFalse(result["ok"])
        self.assertEqual(result["checks"][0]["status"], "passed")
        self.assertEqual(result["checks"][1]["name"], "environment_probe")
        self.assertIn("退出码 2", result["error"])
        self.assertNotIn("raw-secret-token", json.dumps(result))

    def test_missing_ready_marker_never_claims_authenticated_remote_probe(self):
        profile = self.direct_profile()
        for code in (0, 2):
            result, _ = self.probe(profile, output="gateway welcome only\n", returncode=code)
            self.assertFalse(result["ok"])
            self.assertFalse(any(item["name"] == "ssh" and item["status"] == "passed" for item in result["checks"]))

    def test_first_trust_and_password_failures_are_actionable_without_raw_stderr(self):
        profile = self.direct_profile()
        for stderr, expected in (("Host key verification failed. raw-secret-token", "主机"),
                                 ("Permission denied (password). raw-secret-token", "认证"),
                                 ("Connection timed out raw-secret-token", "网络")):
            result, _ = self.probe(profile, error=stderr, returncode=255)
            self.assertFalse(result["ok"])
            self.assertIn(expected, result["error"])
            self.assertNotIn("raw-secret-token", json.dumps(result))
            self.assertIn("StrictHostKeyChecking=ask", result["terminal_command_argv"])

    def test_missing_workspace_or_tmux_prevents_dispatch(self):
        profile = self.direct_profile()
        for output in ("FLFLOW_PROBE_READY=1\nWORKSPACE=missing\n", self.success_output().replace("TMUX=present", "TMUX=missing")):
            result, _ = self.probe(profile, output=output)
            self.assertFalse(result["ok"])
            with mock.patch.object(self.manager, "_check_connection", return_value=result), self.assertRaisesRegex(SessionError, "预检"):
                self.manager.create({"profile_id": profile["id"]})
            self.assertEqual(self.manager.snapshot()["sessions"], [])
            self.assertEqual(FakeClient.instances, [])

    def test_start_rechecks_connection_and_freezes_saved_ssh_configuration(self):
        profile = self.direct_profile()
        success, _ = self.probe(profile)
        with mock.patch.object(self.manager, "_check_connection", return_value=success):
            session = self.manager.create({"profile_id": profile["id"]})
        config = Path(session["ssh_config_path"])
        self.assertNotEqual(config, Path(profile["ssh_config_path"]))
        self.assertIn("gpu.example.test", config.read_text())
        self.manager.save_profile({"id": profile["id"], "ssh_connection": {"host": "other.example.test", "user": "compute", "port": 22}})
        self.assertIn("gpu.example.test", config.read_text())
        failure = {"ok": False, "checked_at": "now", "checks": [], "error": "SSH network unavailable"}
        with mock.patch.object(self.manager, "_check_connection", return_value=failure), self.assertRaisesRegex(SessionError, "尚未启动"):
            self.manager.start(session["id"])
        self.assertEqual(FakeClient.instances, [])
        self.assertEqual(self.manager._copy(session["id"])["status"], "prepared")
        with mock.patch.object(self.manager, "_check_connection", return_value=success):
            client = self.start(session)
        text = next(params for method, params in client.requests if method == "turn/start")["input"][0]["text"]
        self.assertIn(str(config).replace("\\", "\\\\"), text)
        self.assertIn("tools/adaptation-gate-cases", text)
        self.assertIn("update_plan", text)

    def test_existing_alias_keeps_standard_config_and_can_opt_into_preflight(self):
        profile = self.manager.save_profile(self.payload)
        result, args = self.probe(profile)
        self.assertTrue(result["ok"])
        self.assertNotIn("-F", args.args[0])
        with mock.patch.object(self.manager, "_check_connection", return_value={"ok": False, "error": "SSH unavailable"}), self.assertRaises(SessionError):
            self.manager.create({"profile_id": profile["id"], "require_preflight": True})
        self.assertEqual(FakeClient.instances, [])

    def test_two_aliases_for_same_direct_host_cannot_bypass_machine_lease(self):
        first = self.direct_profile()
        second = self.direct_profile()
        self.assertNotEqual(first["ssh_alias"], second["ssh_alias"])
        success, _ = self.probe(first)
        with mock.patch.object(self.manager, "_check_connection", return_value=success):
            self.manager.create({"profile_id": first["id"]})
            with self.assertRaisesRegex(SessionError, "machine already"):
                self.manager.create({"profile_id": second["id"], "backend": "metax"})

    def test_cancel_during_preflight_does_not_start_agent_and_holds_machine_lease(self):
        profile = self.direct_profile()
        success, _ = self.probe(profile)
        with mock.patch.object(self.manager, "_check_connection", return_value=success):
            session = self.manager.create({"profile_id": profile["id"]})
        entered, release = threading.Event(), threading.Event()
        results = []

        def delayed_probe(machine):
            entered.set()
            release.wait(5)
            return success

        with mock.patch.object(self.manager, "_check_connection", side_effect=delayed_probe):
            startup = threading.Thread(target=lambda: results.append(self.manager.start(session["id"])))
            startup.start()
            try:
                self.assertTrue(entered.wait(3))
                self.assertEqual(self.manager._copy(session["id"])["status"], "starting")
                self.assertEqual(self.manager.interrupt(session["id"])["status"], "interrupting")
                self.assertEqual(FakeClient.instances, [])
                with self.assertRaisesRegex(SessionError, "machine already"):
                    self.manager.create(self.payload | {"ssh_alias": session["ssh_alias"], "backend": "metax"})
            finally:
                release.set()
                startup.join(5)
        self.assertFalse(startup.is_alive())
        self.assertEqual(results[0]["status"], "interrupted")
        self.assertEqual(FakeClient.instances, [])
        with self.assertRaisesRegex(SessionError, session["id"]):
            self.manager.create(self.payload | {"ssh_alias": session["ssh_alias"]})
        replacement = self.manager.create(self.payload | {"ssh_alias": session["ssh_alias"], "backend": "metax"})
        self.assertEqual(replacement["status"], "prepared")

    def test_real_plan_and_last_message_survive_restart_without_claiming_gate_success(self):
        session = self.manager.create(self.payload)
        client = self.start(session)
        client.callback({"method": "turn/plan/updated", "params": {"threadId": "thread-123", "plan": [
            {"step": "环境检查", "status": "completed"}, {"step": "Gate 验收", "status": "inProgress"}]}})
        client.callback({"method": "item/completed", "params": {"threadId": "thread-123", "item": {"type": "agentMessage", "text": "模型目录缺失，验收尚未执行。"}}})
        client.callback({"method": "turn/completed", "params": {"threadId": "thread-123", "turn": {"status": "completed"}}})
        other = CodexSessionManager(self.config, self.base, FakeClient)
        state = other.snapshot()["sessions"][0]
        self.assertEqual(state["plan"][1]["status"], "in_progress")
        self.assertIn("尚未执行", state["last_agent_message"])
        self.assertNotIn("gate_passed", state)
        other.close()
