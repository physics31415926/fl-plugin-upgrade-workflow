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
