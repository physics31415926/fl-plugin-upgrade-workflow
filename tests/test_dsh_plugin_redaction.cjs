const assert=require('node:assert/strict');
const fs=require('node:fs');
const path=require('node:path');
const vm=require('node:vm');
const test=require('node:test');
const host=fs.readFileSync(path.join(__dirname,'..','integrations/dsh-plugin/index.js'),'utf8');
// The Host half is an ES module whose exports need a Harness context, so each
// test lifts the one pure function it exercises instead of importing it.
// A top-level `const` is not a property of the VM context, so each slice ends
// with an explicit export statement naming what the test needs.
function lift(start,end,names){const a=host.indexOf(start),b=host.indexOf(end,a);assert(a>=0&&b>a,'cannot locate '+start);const context={};vm.createContext(context);vm.runInContext(host.slice(a,b)+'\n'+names.map(n=>`this.${n}=${n};`).join(''),context);return context;}

test('free-form agent text loses local paths, hosts and accounts',()=>{
  const {redactText}=lift('function redactText(','function taskLabel(',['redactText']);
  // Absolute paths appear in agent prose in both spellings, and an earlier
  // backslash-only scan missed the forward-slash form.
  assert.equal(redactText('saved to C:\\Users\\someone\\repo\\out.txt',4000),'saved to [path]');
  assert.equal(redactText('saved to C:/Users/someone/repo/out.txt',4000),'saved to [path]');
  assert.equal(redactText('read \\\\fileserver\\share\\x',4000),'read [path]');
  assert.equal(redactText('cd /home/someone/work',4000),'cd [path]');
  assert.equal(redactText('cd /root/.flflow/tasks',4000),'cd [path]');
  assert.equal(redactText('ssh someone@gpu-host-01',4000),'ssh [host]');
  assert.equal(redactText('dialing 10.1.2.3 now',4000),'dialing [host] now');
  // Ordinary prose and relative paths must survive untouched.
  assert.equal(redactText('gate 12/20 passed in tests/test_x.py',4000),'gate 12/20 passed in tests/test_x.py');
  // A non-string carries no prose and must not become the string "undefined".
  assert.equal(redactText(undefined,4000),null);
  assert.equal(redactText(42,4000),null);
  assert.equal(redactText('x'.repeat(50),10).length,10);
});

test('a task always has a readable name even without an upstream title',()=>{
  const {taskLabel}=lift('function taskLabel(','/** Reduce one backend row',['taskLabel']);
  assert.equal(taskLabel({title:'THead 适配',backend:'thead'}),'THead 适配');
  assert.equal(taskLabel({thread_name:'proj · enflame · 0.4.0-dev'}),'proj · enflame · 0.4.0-dev');
  // Most dashboard sessions carry neither field; a bare 32-hex id is
  // unreadable and looks identical between rows.
  assert.equal(taskLabel({id:'a'.repeat(32),backend:'cuda',branch:'0.5.0-dev'}),'cuda · 0.5.0-dev');
  assert.equal(taskLabel({id:'a'.repeat(32),backend:'cuda'}),'cuda');
  assert.equal(taskLabel({id:'abcdef1234567890'.repeat(2)}),'#abcdef12');
  assert.equal(taskLabel({}),'—');
  assert.equal(taskLabel({title:'   ',backend:'musa',branch:'dev'}),'musa · dev');
});

test('transcript events expose agent prose only',()=>{
  const context=lift('const TELEMETRY_METHODS','/** Allowed read paths',['TELEMETRY_METHODS']);
  const {redactText}=lift('function redactText(','function taskLabel(',['redactText']);
  const {TELEMETRY_METHODS}=context;
  // Mirrors the summarizer: allowlist delta/item.text, drop telemetry.
  const project=events=>events
    .filter(event=>!TELEMETRY_METHODS.has(String(event&&event.method)))
    .map(event=>({method:event.method,text:redactText(event?.params?.delta??event?.params?.item?.text,4000)}));
  const out=project([
    {method:'item/agentMessage/delta',params:{delta:'checking the gate'}},
    {method:'item/commandExecution/outputDelta',params:{command:'ssh gpu "ls /root/secret"',cwd:'C:\\Users\\someone',aggregatedOutput:'/root/secret/key.pem'}},
    {method:'turn/diff/updated',params:{diff:'--- a/C:/Users/someone/x\n+++ b/x'}},
    {method:'account/rateLimits/updated',params:{rateLimits:{plan:'pro',credits:12}}},
    {method:'thread/tokenUsage/updated',params:{tokenUsage:{total:999}}},
  ]);
  assert.equal(out.length,3,'telemetry events are dropped');
  const serialized=JSON.stringify(out);
  for(const leak of ['/root/secret','C:\\\\Users','C:/Users','key.pem','aggregatedOutput','rateLimits','credits','ssh gpu'])
    assert.equal(serialized.includes(leak),false,'leaked '+leak);
  assert.equal(out[0].text,'checking the gate');
  // An event with no prose still reaches the panel, which labels it.
  assert.equal(out[1].text,null);
});

test('the plugin does not send its own configured paths to the browser',()=>{
  // Redaction covers agent prose, but the plugin's own payloads were building
  // `repoPath` straight from config. An absolute path carries the account name
  // in it, so these two routes are where that rule has to hold.
  const snapshot=host.slice(host.indexOf('function snapshot()'),host.indexOf('async function syncAdoption'));
  assert.equal(/repoPath: config\.repoPath/.test(snapshot),false,
    'the state payload must not carry the configured path');
  assert(/repoConfigured: Boolean\(config\.repoPath\)/.test(snapshot),
    'a boolean answers the only question the panel asks');
  // `expected` was an absolute path to a file that does not exist yet.
  assert.equal(/configExpected/.test(snapshot),false,'must not name an expected path');

  const preflight=host.slice(host.indexOf('function deployPreflight()'),host.indexOf('* Validate one deploy request field'));
  assert.equal(/detail: config\.repoPath/.test(preflight),false,
    'the preflight must not carry the configured path');
  assert(/basename\(config\.repoPath\)/.test(preflight),
    'the directory name is the identifying detail the browser may have');
});

test('dashboard output reaches the browser redacted',()=>{
  // This is the least predictable text the plugin forwards: a Python traceback
  // names every file on the way down, and it was going out raw.
  const read=host.slice(host.indexOf('function readOutput(handle)'),host.indexOf('const disposeStop'));
  assert(/redactText\(/.test(read),'the process output must be redacted');

  const {redactText}=lift('function redactText(','function taskLabel(',['redactText']);
  const traceback=[
    'Traceback (most recent call last):',
    '  File "C:\\Users\\someone\\repo\\scripts\\flflow.py", line 12, in <module>',
    'SystemExit: 1',
  ].join('\n');
  const out=redactText(traceback,8000);
  // The diagnosis has to survive; only the location goes.
  assert.equal(out.includes('C:\\Users'),false,'the path must not survive');
  assert(out.includes('Traceback (most recent call last):'),'keeps the first line');
  assert(out.includes('SystemExit: 1'),'keeps the error');
  assert(out.includes('line 12'),'keeps the line number');
});

test('a first run says what is wrong and what to do about it',()=>{
  const client=fs.readFileSync(path.join(__dirname,'..','integrations/dsh-plugin/client.js'),'utf8');
  // The two failures are distinct and need different instructions: no checkout
  // configured at all, versus a configured directory that is the wrong one.
  for(const key of ['deployNeedRepo','deployWhereConfig','deployNeedScript','deployFoundDir'])
    assert(client.includes(key+':'),'missing locale key '+key);
  // Naming the setting alone leaves someone stuck in a file they have never
  // opened, so the path to the setting is spelled out.
  for(const dict of ['Plugins → FL adaptation workbench → configure','插件 → FL 适配工作台 → 配置'])
    assert(client.includes(dict),'the guidance must say where the setting lives');
  // Cloning is the real first step, and the plugin never runs it itself.
  assert(/git clone https:\/\/github\.com\/[\w-]+\/fl-plugin-upgrade-workflow/.test(client),
    'offers the clone command as copyable text');
  // The wrong-directory case names the directory, never its path.
  assert(/repoCheck\.detail/.test(client),'shows the configured directory name');
});
