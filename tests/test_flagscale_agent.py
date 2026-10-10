import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

from agent_settings import AgentSettingsStore, FlagScaleSettingsStore, validate_flagscale_settings
from codex_sessions import CodexSessionManager, SessionError
from flagscale_adapter import FlagScaleClient, FlagScaleSessionManager, flagscale_availability, CAPABILITIES, KEY_ENV
from flagscale_worker import outcome


DUMMY_KEY = "explicit-dummy-flagscale-key-for-tests-only"


def store_at(path):
    return FlagScaleSettingsStore(path, encrypt=lambda value: b"dummy-cipher:" + value[::-1],
                                decrypt=lambda value: value[len(b"dummy-cipher:"):][::-1])


# Native API fixture, not a fake dashboard completion signal. The actual bridge
# captures KernelResult even when the CLI's single-shot wrapper swallows errors.
NATIVE = r'''
from dataclasses import dataclass
from types import SimpleNamespace
import json, os, pathlib, time

@dataclass
class AgentConfig:
    provider: str = 'anthropic'
    model: str = 'fixture'
    api_key: str = ''
    base_url: str = ''
    max_iterations: int = 200
    max_continuations: int = 20
    max_context_tokens: int = 0
    max_output_tokens: int = 8192
    thinking_budget: int = 0
    time_budget_sec: int = 0
    session_dir: str = ''
    shell_env: dict = None

class History:
    def __init__(self):
        self._messages, self._full_log = [], []
    @property
    def messages(self):
        return self._messages
    def append(self, message):
        self._messages.append(message)
        self._full_log.append(message)
    def hard_reset(self, summary):
        self._messages=[{'role':'assistant','content':summary}]
        self._full_log=[{'role':'assistant','content':summary}]

class AgentKernel:
    def __init__(self, agent):
        self.agent = agent
        self.deps = SimpleNamespace(on_response_fn=None, on_tool_results_fn=None)
    def run_turn(self):
        a = self.agent
        if a.prompt == 'slow':
            time.sleep(20)
        if a.prompt == 'exception':
            raise RuntimeError(a.config.api_key)
        endings = {'need':'[NEED_USER_INPUT]', 'inverted':'[TASK_COMPLETE] quoted before [NEED_USER_INPUT]',
                   'override':'[TASK_COMPLETE]\n_override_reason: native accepted override',
                   'complete':'[TASK_COMPLETE]', 'wrong-signal':'plain output'}
        suffix = endings.get(a.prompt, '[TASK_COMPLETE]')
        a.text = 'done '+a.config.api_key+' '+suffix
        a.history.append({'role':'assistant','content':a.text})
        if a.prompt=='hard-reset':
            a.history.hard_reset('context summary '+a.config.api_key)
            a._session_input_history.append('summary '+a.config.api_key)
            a._save_conversation_full()
        if self.deps.on_response_fn:
            self.deps.on_response_fn({'content':a.text})
        if self.deps.on_tool_results_fn:
            self.deps.on_tool_results_fn([{'name':'Shell','arguments':{'secret':a.config.api_key}}],
                                        [{'content':a.config.api_key}])
        print(a.config.api_key[:15], end='', flush=True)
        print(a.config.api_key[15:], flush=True)
        reasons={'limit':'', 'context':'context_pressure', 'continuations':'max_continuations',
                 'guard':'completion_gate_livelock: '+a.config.api_key,
                 'llm-error':'llm_error: '+a.config.api_key}
        return SimpleNamespace(stop_reason=reasons.get(a.prompt,'explicit_signal'), interrupted=a.prompt=='native-interrupted',
                               iterations=3,input_tokens=10,output_tokens=20)

class WorkerAgent:
    def __init__(self, config):
        assert 'FLFLOW_FLAGSCALE_API_KEY' not in os.environ
        assert 'ANTHROPIC_API_KEY' not in os.environ
        assert 'OPENAI_API_KEY' not in os.environ
        assert 'FLFLOW_SHARE_PASSWORD' not in os.environ
        self.config=config
        import uuid
        self._session_id=uuid.uuid4().hex[:8]
        self._session_dir=pathlib.Path(config.session_dir)/self._session_id
        self._session_dir.mkdir(parents=True)
        self.history=History()
        self._session_input_history=[]
        self._kernel=AgentKernel(self)
        self.text=''
    def _react_loop(self):
        if self.prompt=='outside-kernel-error':
            raise RuntimeError(self.config.api_key)
        self._kernel.run_turn()
    def _save_conversation(self, completed=False, session_summary=None):
        value={'messages':self.history.messages,'full':self.history._full_log,
               'inputs':self._session_input_history,'completed':completed}
        (self._session_dir/'conversation.json').write_text(json.dumps(value))
    def _get_last_assistant_text(self):
        return self.text
    def _save_conversation_full(self):
        (self._session_dir/'conversation_full.json').write_text(json.dumps({'messages':self.history._full_log,'inputs':self._session_input_history}))
    def run(self, single_shot_query=None):
        self.prompt=single_shot_query
        self.history.append({'role':'user','content':single_shot_query})
        self._session_input_history.append(single_shot_query)
        try:
            self._react_loop()
        except Exception:
            print('native caught '+self.config.api_key)
            self.history.append({'role':'assistant','content':'error '+self.config.api_key})
        finally:
            self._save_conversation()
'''


def native_fixture(root):
    package = root / "packages"
    react = package / "flagscale_agent" / "react"
    react.mkdir(parents=True)
    (react.parent / "__init__.py").write_text("")
    (react / "__init__.py").write_text("")
    (react / "fixture.py").write_text(NATIVE, encoding="utf-8")
    (react / "agent.py").write_text("from .fixture import WorkerAgent\n")
    (react / "config.py").write_text("from .fixture import AgentConfig\n")
    (react / "kernel.py").write_text("from .fixture import AgentKernel\n")
    dist = package / "flagscale_agent-0.0.1.dist-info"
    dist.mkdir()
    (dist / "METADATA").write_text("Metadata-Version: 2.1\nName: flagscale-agent\nVersion: 0.0.1\n")
    return package


class SettingsTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.store = store_at(self.root)

    def tearDown(self):
        self.temp.cleanup()

    def test_independent_encrypted_settings_and_blank_delete(self):
        claude = AgentSettingsStore(self.root, encrypt=lambda value: value[::-1], decrypt=lambda value:value[::-1])
        claude.update({"api_key":"another-dummy-claude-key"})
        public = self.store.update({"provider":"flagscale", "api_key":DUMMY_KEY, "llm_provider":"openai",
                                    "base_url":"https://api.example.test/v1", "model":"fixture/model"})
        self.assertEqual(public["provider"], "flagscale")
        self.assertEqual(public["api_protocol"], "openai")
        self.assertNotIn(DUMMY_KEY, json.dumps(public))
        self.assertNotIn(DUMMY_KEY, self.store.path.read_text())
        self.assertNotEqual(self.store.path, claude.path)
        self.store.update({"api_key":"", "max_iterations":17})
        self.assertEqual(self.store.runtime()["api_key"], DUMMY_KEY)
        self.assertEqual(claude.runtime()["api_key"], "another-dummy-claude-key")
        self.store.update({"delete_api_key":True})
        self.assertFalse(self.store.snapshot()["configured"])
        with self.assertRaises(SessionError): self.store.runtime()

    def test_unconfigured_snapshot_needs_no_secret_or_file(self):
        self.assertFalse(self.store.snapshot()["configured"])
        self.assertFalse(self.store.path.exists())
        self.assertNotIn("api_key", self.store.snapshot())

    def test_deletion_or_rotation_cannot_publish_the_old_key(self):
        for provider, factory in (("flagscale",store_at),
                                  ("claude",lambda path:AgentSettingsStore(path,encrypt=lambda value:value[::-1],decrypt=lambda value:value[::-1]))):
            with self.subTest(provider=provider):
                store=factory(self.root/provider)
                store.update({"api_key":DUMMY_KEY})
                for update in ({"delete_api_key":True,"model":DUMMY_KEY},
                               {"api_key":"another-dummy-test-key","base_url":"https://example.test/"+DUMMY_KEY}):
                    with self.assertRaises(SessionError): store.update(update)
                self.assertEqual(store.runtime()["api_key"],DUMMY_KEY)

    def test_reject_invalid_settings_credentials_and_permission_impersonation(self):
        for payload in ({"provider":"claude"}, {"permission_mode":"acceptEdits"}, {"allowed_tools":["Shell"]},
                        {"llm_provider":"codex"}, {"max_iterations":True}, {"max_continuations":-1},
                        {"max_output_tokens":0}, {"time_budget_sec":-1}, {"max_context_tokens":None},
                        {"thinking_budget":100}, {"llm_provider":"openai","thinking_budget":1024},
                        {"thinking_budget":8192}, {"api_key":"two tokens"},
                        {"base_url":"https://name:password@example.test"},
                        {"api_key":DUMMY_KEY,"model":DUMMY_KEY}):
            with self.subTest(payload=payload), self.assertRaises(SessionError):
                self.store.update(payload)
        self.assertEqual(validate_flagscale_settings({"thinking_budget":1024})["thinking_budget"],1024)

    @unittest.skipUnless(os.name == "nt", "Windows DPAPI roundtrip")
    def test_actual_dpapi_independent_file_has_no_plaintext(self):
        store = FlagScaleSettingsStore(self.root / "protected")
        store.update({"api_key":DUMMY_KEY})
        self.assertNotIn(DUMMY_KEY, store.path.read_text())
        self.assertEqual(store.runtime()["api_key"], DUMMY_KEY)


class WorkerIntegrationTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.cwd = self.root / "worktree"
        self.cwd.mkdir()
        self.package = native_fixture(self.root)
        self.events, self.commands, self.environments = [], [], []
        self.store = store_at(self.root / "settings")
        self.store.update({"api_key":DUMMY_KEY})

        def launch(command, **kwargs):
            self.commands.append(command)
            self.environments.append(kwargs["env"])
            bootstrap = "import sys,runpy;sys.path.insert(0," + repr(str(self.package)) + ");runpy.run_path(" + repr(command[-1]) + ",run_name='__main__')"
            return subprocess.Popen([sys.executable,"-B","-c",bootstrap], **kwargs)

        self.client = FlagScaleClient(sys.executable, self.cwd, self.events.append,
                                      settings=self.store.runtime(), state_dir=self.root / "state", popen=launch)
        self.thread = self.client.request("thread/start", {})["thread"]["id"]

    def tearDown(self):
        self.client.close()
        if self.client._reader: self.client._reader.join(5)
        self.temp.cleanup()

    def run_turn(self, text):
        value = self.client.request("turn/start", {"threadId":self.thread,"input":[{"type":"text","text":text}]})
        self.client._reader.join(6)
        self.assertFalse(self.client._reader.is_alive())
        return value

    def result(self):
        return next(event["params"] for event in reversed(self.events) if event["method"]=="flagscale/result")

    def test_real_worker_scrubs_native_history_output_and_key_environment(self):
        with patch.dict(os.environ, {"ANTHROPIC_API_KEY":"other-dummy-anthropic", "OPENAI_API_KEY":"other-dummy-openai",
                                     "FLFLOW_SHARE_PASSWORD":"other-dummy-dashboard", "FLAGSCALE_HOME":"foreign-root"}):
            self.run_turn("complete")
        self.assertEqual(self.events[-1]["params"]["turn"]["status"],"completed")
        self.assertEqual(self.result()["stop_reason"],"task_complete")
        self.assertEqual(self.result()["iterations"],3)
        self.assertNotIn(DUMMY_KEY," ".join(self.commands[0]))
        self.assertNotIn("complete",self.commands[0])
        self.assertNotIn("--resume",self.commands[0])
        self.assertNotIn("ANTHROPIC_API_KEY",self.environments[0])
        self.assertNotIn("OPENAI_API_KEY",self.environments[0])
        self.assertNotIn("FLFLOW_SHARE_PASSWORD",self.environments[0])
        self.assertNotIn("FLAGSCALE_HOME",self.environments[0])
        self.assertEqual(self.environments[0][KEY_ENV],DUMMY_KEY)
        self.assertNotIn(DUMMY_KEY,json.dumps(self.events))
        self.assertNotIn(DUMMY_KEY,"\n".join(self.client.stderr))
        self.assertEqual(list(self.cwd.iterdir()),[])
        for file in (self.root/"state").rglob("*.json"):
            self.assertNotIn(DUMMY_KEY,file.read_text())
        self.assertTrue(list((self.root/"state").rglob("conversation.json")))
        tools=next(event for event in self.events if event["method"]=="flagscale/tools")
        self.assertEqual(tools["params"]["names"],["Shell"])
        self.assertNotIn("arguments",json.dumps(tools))

    def test_exit_zero_swallowed_error_and_limits_never_complete(self):
        for prompt, status, reason in (("exception","failed","native_error"),
                                      ("outside-kernel-error","failed","native_error"),
                                      ("llm-error","failed","llm_error"),
                                      ("limit","waiting_input","iteration_limit"),
                                      ("continuations","waiting_input","max_continuations"),
                                      ("context","waiting_input","context_pressure"),
                                      ("guard","waiting_input","completion_gate_livelock"),
                                      ("need","waiting_input","need_user_input"),
                                      ("inverted","waiting_input","need_user_input"),
                                      ("wrong-signal","failed","unverified_completion_signal"),
                                      ("native-interrupted","interrupted","interrupted")):
            with self.subTest(prompt=prompt):
                self.run_turn(prompt)
                self.assertEqual(self.result()["status"],status)
                self.assertEqual(self.result()["stop_reason"],reason)
                self.assertNotEqual(self.events[-1]["params"]["turn"]["status"],"completed")
                self.assertNotIn(DUMMY_KEY,json.dumps(self.events))

    def test_followup_uses_new_native_id_with_same_dashboard_context(self):
        self.run_turn("complete")
        native=self.result()["native_session_id"]
        self.client.request("thread/resume",{"threadId":self.thread})
        self.run_turn("complete")
        self.assertNotEqual(native,self.result()["native_session_id"])
        self.assertEqual(self.result()["threadId"],self.thread)
        self.assertEqual(self.client.info["follow_up_mode"],"fresh_turn_with_context")
        self.assertFalse(self.client.info["supports_resume"])
        self.assertFalse(self.client.info["supports_mid_turn_steer"])

    def test_native_completion_override_and_hard_reset_save_are_supported(self):
        self.run_turn("override")
        self.assertEqual(self.result()["status"],"completed")
        self.run_turn("hard-reset")
        self.assertEqual(self.result()["status"],"completed")
        files=list((self.root/"state").rglob("conversation_full.json"))
        self.assertTrue(files)
        for file in files: self.assertNotIn(DUMMY_KEY,file.read_text())
        self.assertNotIn(DUMMY_KEY,json.dumps(self.events))

    def test_windows_utf8_prompt_roundtrip_preserves_chinese_context(self):
        prompt="继续核对本任务的模型验收结果与并发测试。"
        self.run_turn(prompt)
        self.assertEqual(self.events[-1]["params"]["turn"]["status"],"completed")
        history=json.loads(next((self.root/"state").rglob("conversation.json")).read_text())
        self.assertEqual(history["inputs"],[prompt])
        self.assertEqual(self.environments[0]["PYTHONIOENCODING"],"utf-8")

    def test_active_followup_rejected_and_interrupt_owns_only_fixture_child(self):
        self.client.request("turn/start",{"threadId":self.thread,"input":[{"type":"text","text":"slow"}]})
        child=self.client._child
        with self.assertRaises(SessionError): self.client.request("turn/steer",{"input":[]})
        with self.assertRaises(SessionError): self.client.request("turn/interrupt",{"threadId":"wrong","turnId":self.client._turn_id})
        stopped=[]
        self.client._stop_process=lambda owned:(stopped.append(owned),owned.terminate())
        self.client.request("turn/interrupt",{"threadId":self.thread,"turnId":self.client._turn_id})
        self.client._reader.join(5)
        self.assertEqual(stopped,[child])
        self.assertEqual(self.events[-1]["params"]["turn"]["status"],"interrupted")

    def test_secret_prompt_and_state_inside_worktree_rejected(self):
        with self.assertRaises(SessionError): self.run_turn("leak "+DUMMY_KEY)
        self.client.state_dir=self.cwd/"private"
        with self.assertRaises(SessionError): self.run_turn("complete")
        self.assertEqual(self.commands,[])

    def test_availability_imports_only_without_agent_or_api(self):
        calls=[]
        def runner(command,**kwargs):
            calls.append((command,kwargs))
            self.assertNotIn(KEY_ENV,kwargs["env"])
            if command[-1]=="--version":
                return subprocess.CompletedProcess(command,0,"FlagScale Agent 0.0.1","")
            bootstrap="import sys;sys.path.insert(0,"+repr(str(self.package))+");"+command[-1]
            return subprocess.run([sys.executable,"-B","-c",bootstrap],**kwargs)
        value=flagscale_availability(sys.executable,"optional-cli",runner=runner)
        self.assertTrue(value["available"])
        self.assertEqual(value["version"],"0.0.1")
        self.assertEqual(calls[0][0],["optional-cli","--version"])
        self.assertFalse((self.root/"state").exists())
        bad=lambda command,**kwargs:subprocess.CompletedProcess(command,1,"",DUMMY_KEY)
        failed=flagscale_availability(sys.executable,runner=bad)
        self.assertFalse(failed["available"])
        self.assertNotIn(DUMMY_KEY,json.dumps(failed))


PROTOCOL = r'''
import json,sys
request=json.loads(sys.stdin.read())
kind=request['prompt']
def emit(type, **kwargs):
    print(json.dumps({'type':type,'thread_id':request['thread_id'],'turn_id':request['turn_id'],**kwargs}),flush=True)
if kind=='missing': sys.exit(0)
if kind=='bad-json':
    print('not json'); sys.exit(0)
emit('init',native_session_id='1234abcd')
if kind=='wrong-id':
    request['thread_id']='wrong'
if kind=='duplicate-init': emit('init',native_session_id='1234abcd')
if kind=='invalid-success': emit('result',status='completed',stop_reason='iteration_limit',native_session_id='1234abcd')
else: emit('result',status='completed',stop_reason='task_complete',native_session_id='1234abcd')
if kind=='duplicate-result': emit('result',status='completed',stop_reason='task_complete',native_session_id='1234abcd')
if kind=='exit-error': sys.exit(1)
'''


class ProtocolTest(unittest.TestCase):
    def test_missing_malformed_identity_and_false_completion_fail_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory); worktree=root/"worktree"; worktree.mkdir()
            worker=root/"fixture.py"; worker.write_text(PROTOCOL)
            store=store_at(root/"settings"); store.update({"api_key":DUMMY_KEY})
            for kind in ("missing","bad-json","wrong-id","duplicate-init","invalid-success","duplicate-result","exit-error"):
                with self.subTest(kind=kind):
                    events=[]
                    client=FlagScaleClient(sys.executable,worktree,events.append,settings=store.runtime(),state_dir=root/"state",worker_path=worker)
                    try:
                        thread=client.request("thread/start",{})["thread"]["id"]
                        client.request("turn/start",{"threadId":thread,"input":[{"type":"text","text":kind}]})
                        client._reader.join(5)
                        self.assertEqual(events[-1]["params"]["turn"]["status"],"failed")
                    finally: client.close()


class ManagerTest(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(); self.root=Path(self.temp.name)
        self.repo=self.root/"plugin"; self.repo.mkdir()
        self.git("init","-b","main"); self.git("config","user.name","Test"); self.git("config","user.email","test@example.test")
        (self.repo/"README.md").write_text("source\n"); self.git("add","."); self.git("commit","-m","base")
        self.package=native_fixture(self.root)
        self.store=store_at(self.root/"settings"); self.store.update({"api_key":DUMMY_KEY})
        self.prompts=[]
        def factory(python,cwd,callback,**kwargs):
            def launch(command,**options):
                bootstrap="import sys,runpy;sys.path.insert(0,"+repr(str(self.package))+");runpy.run_path("+repr(command[-1])+",run_name='__main__')"
                return subprocess.Popen([sys.executable,"-B","-c",bootstrap],**options)
            client=FlagScaleClient(python,cwd,callback,**kwargs,popen=launch)
            original=client.request
            def request(method,params):
                if method=="turn/start": self.prompts.append(params["input"][0]["text"])
                return original(method,params)
            client.request=request
            return client
        self.config={"repo":str(self.repo),"branch":"main","engine":"sglang","sessions_dir":"runs/flagscale-sessions"}
        self.manager=FlagScaleSessionManager(self.config,self.root,self.store,flagscale_client_factory=factory,
                                              availability_checker=lambda *args:{"available":True,"error":None,**CAPABILITIES})
        self.payload={"provider":"flagscale","backend":"cuda","ssh_alias":"gpu_demo","remote_workspace":"/work/plugin","branch":"main"}

    def git(self,*args):
        return subprocess.check_output(["git","-C",str(self.repo),*args],stderr=subprocess.STDOUT,text=True).strip()

    def tearDown(self):
        self.manager.close()
        for worktree in (self.root/"runs/flagscale-sessions/worktrees").glob("*"):
            self.git("worktree","remove","--force",str(worktree))
        self.temp.cleanup()

    def settle(self,session_id):
        self.manager._workers[session_id].join(6)
        self.manager._clients[session_id]._reader.join(6)
        return self.manager._copy(session_id)

    def test_distinct_provider_store_policy_and_no_secret_in_persistence(self):
        for payload in ({**self.payload,"provider":"codex"},{**self.payload,"api_key":DUMMY_KEY},
                        {**self.payload,"goal":"accidental "+DUMMY_KEY}):
            with self.assertRaises(SessionError): self.manager.create(payload)
        session=self.manager.create(self.payload)
        self.assertEqual(session["provider"],"flagscale")
        self.assertIn("FlagScale Agent",session["prompt"])
        self.assertIn("AGENTS.md",session["prompt"])
        self.manager._launch(session["id"],"complete")
        self.assertEqual(self.settle(session["id"])["status"],"completed")
        self.assertNotIn(DUMMY_KEY,json.dumps(self.manager.snapshot()))
        for file in self.manager.root.rglob("*.json*"):
            self.assertNotIn(DUMMY_KEY,file.read_text(encoding="utf-8"))

    def test_limit_status_is_waiting_input_and_followup_has_real_task_context(self):
        session=self.manager.create(self.payload)
        self.manager._launch(session["id"],"limit")
        waiting=self.settle(session["id"])
        self.assertEqual(waiting["status"],"waiting_input")
        self.assertEqual(waiting["flagscale_result"]["stop_reason"],"iteration_limit")
        self.manager.message(session["id"],"check the previous evidence")
        resumed=self.settle(session["id"])
        self.assertEqual(resumed["status"],"completed")
        self.assertEqual(resumed["worktree"],waiting["worktree"])
        self.assertNotEqual(resumed["flagscale_session_id"],waiting["flagscale_session_id"])
        self.assertIn(session["prompt"],self.prompts[-1])
        self.assertIn("上一轮结果",self.prompts[-1])
        self.assertIn("check the previous evidence",self.prompts[-1])
        self.assertNotIn(DUMMY_KEY,self.prompts[-1])

    def test_peer_machine_reservation_blocks_flagscale_and_old_owner_rejected(self):
        class ExistingOwner:
            def allocation_sessions(self):
                return [{"id":"legacy","status":"waiting_approval","ssh_alias":"gpu_demo","ssh_connection":None}]
        self.manager.peer_managers=[self.manager,ExistingOwner()]
        with self.assertRaises(SessionError): self.manager.create(self.payload)
        self.assertEqual(self.manager.snapshot()["sessions"],[])
        with self.assertRaises(SessionError): FlagScaleSessionManager({**self.config,"sessions_api_url":"http://127.0.0.1:1234"},self.root,self.store)
        codex=CodexSessionManager({**self.config,"sessions_dir":"runs/codex-only"},self.root)
        try:
            with self.assertRaises(SessionError): codex.create(self.payload)
        finally: codex.close()

    def test_unconfigured_health_and_monitor_policy_do_not_start_agent(self):
        self.store.update({"delete_api_key":True})
        self.assertFalse(self.manager.snapshot()["agent_settings"]["configured"])
        self.assertFalse(self.manager.health()["configured"])
        with self.assertRaises(SessionError): self.manager.create(self.payload)
        self.assertEqual(self.prompts,[])
        self.store.update({"api_key":DUMMY_KEY})
        self.manager.backend_task_policies=self.manager._task_policies({"cuda":{"branch":"main","mode":"monitor","reason":"fixture"}})
        with self.assertRaises(SessionError): self.manager.create(self.payload)
        self.assertEqual(self.prompts,[])

    def test_multiple_followups_retain_earlier_user_requirements(self):
        session=self.manager.create(self.payload)
        self.manager._launch(session["id"],"complete")
        self.settle(session["id"])
        self.manager.message(session["id"],"first user requirement")
        self.settle(session["id"])
        self.manager.message(session["id"],"second user requirement")
        self.settle(session["id"])
        self.assertIn("first user requirement",self.prompts[-1])
        self.assertIn("second user requirement",self.prompts[-1])

    def test_waiting_input_projection_never_exposes_a_freed_machine_lease(self):
        session=self.manager.create(self.payload)
        sid=session["id"]
        with self.manager._lock:
            stored=self.manager._get(sid)
            stored.update(status="running",thread_id="12345678-1234-1234-1234-123456789abc",turn_id="fixture",
                          flagscale_result={"status":"waiting_input","stop_reason":"iteration_limit"})
        base_processed,release,observer_started,observer_done=(threading.Event() for _ in range(4))
        original=CodexSessionManager._event
        def pause_after_base(manager,*args):
            original(manager,*args)
            base_processed.set()
            release.wait(3)
        observations=[]
        def observe():
            observer_started.set()
            observations.extend(self.manager.allocation_sessions())
            observer_done.set()
        with patch.object(CodexSessionManager,"_event",pause_after_base):
            event_thread=threading.Thread(target=self.manager._event,args=(sid,{"method":"turn/completed","params":{
                "threadId":stored["thread_id"],"turn":{"id":"fixture","status":"failed","error":None}}}))
            event_thread.start()
            self.assertTrue(base_processed.wait(3))
            observer=threading.Thread(target=observe); observer.start()
            self.assertTrue(observer_started.wait(3))
            self.assertFalse(observer_done.wait(.05))
            release.set(); event_thread.join(3); observer.join(3)
        self.assertTrue(observer_done.is_set())
        self.assertEqual(observations[0]["status"],"waiting_input")


if __name__ == "__main__":
    unittest.main()
