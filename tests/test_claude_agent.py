import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from fl_dashboard.agents.agent_settings import AgentSettingsStore, validate_base_url
from fl_dashboard.agents.claude_agent import ClaudeClient, ClaudeSessionManager, cli_availability
from fl_dashboard.agents.codex_sessions import CodexSessionManager, SessionError

DUMMY_KEY = "explicit-dummy-claude-key-for-tests-only"


def fake_store(directory):
    return AgentSettingsStore(directory, encrypt=lambda value: b"dummy-cipher:" + value[::-1],
                              decrypt=lambda value: value[len(b"dummy-cipher:"):][::-1])


class SettingsTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.store = fake_store(self.root)

    def tearDown(self):
        self.temp.cleanup()

    def test_key_never_returned_and_blank_preserves_key(self):
        public = self.store.update({"api_key": DUMMY_KEY, "base_url": "https://api.example.test/anthropic"})
        self.assertTrue(public["configured"])
        self.assertNotIn(DUMMY_KEY, json.dumps(public))
        self.assertNotIn(DUMMY_KEY, self.store.path.read_text())
        self.store.update({"api_key": "", "model": "opus"})
        self.assertEqual(self.store.runtime()["api_key"], DUMMY_KEY)
        self.assertEqual(self.store.snapshot()["model"], "opus")
        self.store.update({"delete_api_key": True})
        self.assertFalse(self.store.snapshot()["configured"])
        with self.assertRaises(SessionError):
            self.store.runtime()

    def test_key_cannot_leak_via_public_model_or_base_url(self):
        with self.assertRaises(SessionError):
            self.store.update({"api_key":DUMMY_KEY, "model":DUMMY_KEY})
        with self.assertRaises(SessionError):
            self.store.update({"api_key":DUMMY_KEY, "base_url":"https://api.example.test/"+DUMMY_KEY})

    def test_only_loopback_http_and_no_url_credentials(self):
        for url in ("http://localhost:8080", "http://127.0.0.1:8080/v1", "http://[::1]:8080"):
            self.assertEqual(validate_base_url(url), url)
        for url in ("http://api.example.test", "http://127.0.0.1.evil.test", "http://192.168.1.2",
                    "https://user:key@example.test", "https://example.test?key=dummy",
                    "https://example.test/#secret", "https://example.test:bad"):
            with self.subTest(url=url), self.assertRaises(SessionError):
                validate_base_url(url)

    def test_no_permission_bypass_or_unknown_settings(self):
        for payload in ({"permission_mode": "bypassPermissions"}, {"permission_mode": "auto"},
                        {"allowed_tools": ["*"]}, {"allowed_tools": ["UnknownTool"]},
                        {"max_turns": True}, {"api_key": "line\nbreak"},
                        {"authorization": "dummy"}):
            with self.subTest(payload=payload), self.assertRaises(SessionError):
                self.store.update(payload)

    @unittest.skipUnless(os.name == "nt", "Windows DPAPI roundtrip")
    def test_real_current_user_dpapi_has_no_plaintext(self):
        secure = AgentSettingsStore(self.root / "dpapi")
        secure.update({"api_key": DUMMY_KEY})
        self.assertNotIn(DUMMY_KEY, secure.path.read_text())
        self.assertEqual(secure.runtime()["api_key"], DUMMY_KEY)


STUB = r"""
import json, os, sys, time
args = sys.argv[1:]
thread = args[args.index("--resume")+1] if "--resume" in args else args[args.index("--session-id")+1]
prompt = sys.stdin.read()
def emit(value):
    print(json.dumps(value), flush=True)
emit({"type":"system","subtype":"init","session_id":thread,"model":"stub"})
if prompt == "slow":
    time.sleep(20)
if prompt == "missing":
    sys.exit(0)
if prompt == "wrong-id":
    emit({"type":"result","subtype":"success","session_id":"bad-id","result":"no"})
    sys.exit(0)
key = os.environ["ANTHROPIC_API_KEY"]
emit({"type":"stream_event","session_id":thread,"event":{"delta":{"type":"text_delta","text":key[:15]}}})
emit({"type":"stream_event","session_id":thread,"event":{"delta":{"type":"text_delta","text":key[15:]}}})
emit({"type":"assistant","session_id":thread,"message":{"content":[{"type":"text","text":"redact "+key}]}})
print("stderr "+key, file=sys.stderr, flush=True)
denials = [{"tool_name":"Bash","tool_input":{"command":"blocked"}}] if prompt == "denied" else []
emit({"type":"result","subtype":"success","session_id":thread,"is_error":False,
      "result":("resumed" if "--resume" in args else "new")+" "+key,
      "permission_denials":denials,"num_turns":1,"total_cost_usd":0})
"""


class ClaudeProtocolTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.stub = self.root / "stub.py"
        self.stub.write_text(STUB, encoding="utf-8")
        self.events = []
        self.commands = []
        self.environment = None
        self.prompt = None
        store = fake_store(self.root / "settings")
        store.update({"api_key": DUMMY_KEY, "allowed_tools": ["Read", "Bash(ssh *)"]})
        def launch(command, **kwargs):
            self.commands.append(command)
            self.environment = kwargs["env"]
            return subprocess.Popen([sys.executable, str(self.stub), *command[1:]], **kwargs)
        self.client = ClaudeClient("stub-claude", self.root, self.events.append,
                                   settings=store.runtime(), config_dir=self.root / "cli", popen=launch)
        self.thread = self.client.request("thread/start", {})["thread"]["id"]

    def tearDown(self):
        self.client.close()
        if self.client._reader:
            self.client._reader.join(5)
        self.temp.cleanup()

    def run_turn(self, text):
        result = self.client.request("turn/start", {"threadId": self.thread, "input": [{"type": "text", "text": text}]})
        self.client._reader.join(6)
        self.assertFalse(self.client._reader.is_alive())
        return result

    def test_stream_redaction_resume_and_auth_environment(self):
        with patch.dict(os.environ, {"ANTHROPIC_AUTH_TOKEN": "foreign-dummy-auth", "CLAUDE_CODE_OAUTH_TOKEN": "foreign-dummy-oauth",
                                    "OPENAI_API_KEY": "foreign-dummy-openai",
                                    "FLFLOW_SHARE_PASSWORD": "foreign-dummy-dashboard"}):
            self.run_turn("normal")
        self.assertIn("--bare", self.commands[0])
        self.assertNotIn("--dangerously-skip-permissions", self.commands[0])
        self.assertNotIn("--permission-prompts", self.commands[0])
        self.assertNotIn(DUMMY_KEY, " ".join(self.commands[0]))
        self.assertNotIn("normal", self.commands[0])
        self.assertNotIn("OPENAI_API_KEY", self.environment)
        self.assertNotIn("FLFLOW_SHARE_PASSWORD", self.environment)
        self.assertNotIn("ANTHROPIC_AUTH_TOKEN", self.environment)
        self.assertNotIn("CLAUDE_CODE_OAUTH_TOKEN", self.environment)
        self.assertEqual(self.environment["ANTHROPIC_API_KEY"], DUMMY_KEY)
        self.assertNotIn(DUMMY_KEY, json.dumps(self.events))
        partial = "".join(event["params"].get("delta", "") for event in self.events)
        self.assertNotIn(DUMMY_KEY, partial)
        self.assertFalse(any(event["method"] == "item/agentMessage/delta" for event in self.events))
        self.assertNotIn(DUMMY_KEY, "\n".join(self.client.stderr))
        self.assertEqual(self.events[-1]["params"]["turn"]["status"], "completed")
        self.run_turn("follow-up")
        self.assertIn("--resume", self.commands[1])
        self.assertEqual(self.commands[1][self.commands[1].index("--resume")+1], self.thread)

    def test_no_result_or_wrong_identity_cannot_complete(self):
        for prompt in ("missing", "wrong-id"):
            self.run_turn(prompt)
            self.assertEqual(self.events[-1]["params"]["turn"]["status"], "failed")

    def test_denials_are_explicit(self):
        self.run_turn("denied")
        result = next(event for event in reversed(self.events) if event["method"] == "claude/result")
        self.assertEqual(result["params"]["permission_denials"][0]["tool_name"], "Bash")

    def test_running_followup_rejected_and_stop_owns_only_new_child(self):
        self.client.request("turn/start", {"threadId": self.thread, "input": [{"type":"text","text":"slow"}]})
        child = self.client._child
        with self.assertRaises(SessionError):
            self.client.request("turn/steer", {"input":[{"type":"text","text":"next"}]})
        # Inject stop rather than taskkill to keep the fake integration isolated.
        stopped = []
        self.client._stop_process = lambda process: (stopped.append(process), process.terminate())
        self.client.request("turn/interrupt", {"threadId":self.thread, "turnId":self.client._turn_id})
        self.client._reader.join(5)
        self.assertEqual(stopped, [child])
        self.assertEqual(self.events[-1]["params"]["turn"]["status"], "interrupted")

    def test_health_only_calls_version_and_help(self):
        calls = []
        def runner(args, **kwargs):
            calls.append(args)
            return subprocess.CompletedProcess(args, 0, "2.1.212 (Claude Code)" if args[-1]=="--version" else "--bare --output-format", "")
        result = cli_availability("stub", runner=runner)
        self.assertTrue(result["available"])
        self.assertEqual([args[-1] for args in calls], ["--version", "--help"])


class ManagerTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.repo = self.root / "plugin"
        self.repo.mkdir()
        self.git("init", "-b", "main")
        self.git("config", "user.name", "Test")
        self.git("config", "user.email", "test@example.test")
        (self.repo / "README.md").write_text("source\n")
        self.git("add", ".")
        self.git("commit", "-m", "base")
        self.store = fake_store(self.root / "settings")
        self.store.update({"api_key": DUMMY_KEY})
        self.stub = self.root / "stub.py"
        self.stub.write_text(STUB)
        def factory(executable, cwd, callback, **kwargs):
            return ClaudeClient(executable, cwd, callback, **kwargs,
                popen=lambda argv, **options: subprocess.Popen([sys.executable, str(self.stub), *argv[1:]], **options))
        self.config = {"repo": str(self.repo), "branch":"main", "engine":"sglang",
                       "sessions_dir":"runs/claude-sessions", "claude_executable":"stub"}
        self.manager = ClaudeSessionManager(self.config, self.root, self.store, claude_client_factory=factory)
        self.payload = {"provider":"claude","backend":"cuda","ssh_alias":"gpu_demo",
                        "remote_workspace":"/work/plugin","branch":"main"}

    def git(self, *args):
        return subprocess.check_output(["git","-C",str(self.repo),*args], stderr=subprocess.STDOUT,text=True).strip()

    def tearDown(self):
        self.manager.close()
        for worktree in (self.root / "runs/claude-sessions/worktrees").glob("*"):
            self.git("worktree","remove","--force",str(worktree))
        self.temp.cleanup()

    def test_provider_store_policy_and_permission_denials(self):
        with self.assertRaises(SessionError):
            self.manager.create({**self.payload,"provider":"codex"})
        with self.assertRaises(SessionError):
            self.manager.create({**self.payload,"api_key":DUMMY_KEY})
        with self.assertRaises(SessionError):
            self.manager.create({**self.payload,"goal":"accidental "+DUMMY_KEY})
        session = self.manager.create(self.payload)
        self.assertEqual(session["provider"], "claude")
        self.assertIn("AGENTS.md", session["prompt"])
        self.assertNotIn(DUMMY_KEY, json.dumps(self.manager.snapshot()))
        # Use a bounded stub turn, preserving real manager lifecycle + event persistence.
        self.manager._launch(session["id"], "denied")
        self.manager._workers[session["id"]].join(5)
        self.manager._clients[session["id"]]._reader.join(5)
        blocked = self.manager._copy(session["id"])
        self.assertEqual(blocked["status"], "waiting_input")
        self.assertTrue(blocked["claude_permission_denials"])
        self.manager.message(session["id"], "resolved")
        self.manager._workers[session["id"]].join(5)
        self.manager._clients[session["id"]]._reader.join(5)
        self.assertEqual(self.manager._copy(session["id"])["status"], "completed")
        self.assertNotIn(DUMMY_KEY, json.dumps(self.manager.snapshot()))

    def test_claude_cannot_attach_old_codex_service_and_codex_rejects_provider(self):
        with self.assertRaises(SessionError):
            ClaudeSessionManager({**self.config,"sessions_api_url":"http://127.0.0.1:1234"}, self.root, self.store)
        codex = CodexSessionManager({**self.config,"sessions_dir":"runs/codex-only"}, self.root)
        try:
            with self.assertRaises(SessionError):
                codex.create(self.payload)
        finally:
            codex.close()

    def test_live_peer_machine_blocks_claude_preparation(self):
        class ExistingOwner:
            def allocation_sessions(self):
                return [{"id":"legacy","status":"waiting_approval","ssh_alias":"gpu_demo","ssh_connection":None}]
        self.manager.peer_managers = [self.manager, ExistingOwner()]
        with self.assertRaises(SessionError):
            self.manager.create(self.payload)
        self.assertEqual(self.manager.snapshot()["sessions"], [])


if __name__ == "__main__":
    unittest.main()
