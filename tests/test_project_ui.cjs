const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const test = require('node:test');
const root = path.resolve(__dirname, '..');
function source(name) { return [...fs.readFileSync(path.join(root,name),'utf8').matchAll(/<script\b[^>]*>([\s\S]*?)<\/script>/g)].map(match=>match[1]).join('\n'); }
function between(text,start,end) { const first=text.indexOf(start),last=text.indexOf(end,first);assert(first>=0&&last>first);return text.slice(first,last); }

test('all dashboard scripts parse', () => { for(const name of ['index.html','sessions.html','overview.html','workflow.html']) new vm.Script(source(name),{filename:name}); });
test('detail APIs and evidence retain SGLang identity with existing query fields', () => {
  const text=source('index.html');const context={URL,URLSearchParams,window:{location:{href:'http://127.0.0.1:8766/monitor?project=sglang',search:'?project=sglang'}}};vm.createContext(context);
  vm.runInContext(between(text,'  let projectId =','  const labels =')+'\nglobalThis.route=appUrl;',context);
  const result=new URL(context.route('api/branch-evidence?branch=dev%2F0.5.18&backend=cuda&model=Qwen3.6-27B&mode=eager&scenario=concurrent_text_16'));
  assert.equal(result.searchParams.get('project'),'sglang');assert.equal(result.searchParams.get('branch'),'dev/0.5.18');assert.equal(result.searchParams.get('scenario'),'concurrent_text_16');
  const task=new URL(context.route('sessions?session=example&branch=dev%2F0.5.18'));assert.equal(task.searchParams.get('session'),'example');assert.equal(task.searchParams.get('project'),'sglang');
});
test('session follow-ups and navigation retain project without overwriting branch', () => {
  const text=source('sessions.html');const context={URL,URLSearchParams,location:{href:'http://127.0.0.1:8766/sessions?project=sglang',search:'?project=sglang'}};vm.createContext(context);
  vm.runInContext(between(text,"  let projectId =",'  const labels =')+'\nglobalThis.route=projectUrl;globalThis.monitor=monitorUrl;',context);
  assert.equal(new URL(context.route('/api/codex-sessions/id/message')).searchParams.get('project'),'sglang');
  const target=new URL(context.monitor('dev/0.5.18','session-example'));assert.equal(target.pathname,'/monitor');assert.equal(target.searchParams.get('branch'),'dev/0.5.18');assert.equal(target.searchParams.get('session'),'session-example');assert.equal(target.searchParams.get('project'),'sglang');
});
test('overview uses separate repo branches and preserves stale acceptance status', () => {
  const text=source('overview.html');const context={URL,location:{href:'https://example.invalid/team/dashboard/'}};vm.createContext(context);
  vm.runInContext(between(text,'  function appUrl(', '  function badge(')+'\nglobalThis.route=appUrl;globalThis.ci=effectiveCi;globalThis.gate=effectiveGate;',context);
  const a=new URL(context.route('monitor','vllm','0.4.0-dev')),b=new URL(context.route('monitor','sglang','dev/0.5.18'));assert.equal(a.pathname,'/team/dashboard/monitor');assert.equal(a.searchParams.get('project'),'vllm');assert.equal(b.searchParams.get('branch'),'dev/0.5.18');
  assert.equal(context.ci({ci:{status:'passed',scope:'branch',commit:'old'}},'new'),'stale');assert.equal(context.gate({gate:{status:'passed',latest_commit:'old'}},'new'),'stale');assert.equal(context.gate({gate:{status:'recorded_pass',tree_matches_head:true,latest_commit:'old'}},'new'),'recorded_pass');
});

test('SGLang source cases only appear for their own model and runtime mode', () => {
  const text=source('index.html'),context={};vm.createContext(context);
  vm.runInContext(between(text,'  function expectedRows(', '  function renderMatrix(')+'\nglobalThis.rows=expectedRows;',context);
  const data={models:['model-a','model-b'],modes:['eager','graph'],cases:[{scenario:'a',models:['model-a'],modes:['eager']},{scenario:'b',models:['model-b'],modes:['eager']} ]};
  const result=context.rows(data,{results:[]});assert.equal(result.find(group=>group.model==='model-a'&&group.mode==='eager').rows.length,1);assert.equal(result.find(group=>group.model==='model-b'&&group.mode==='eager').rows[0].descriptor.scenario,'b');assert.equal(result.find(group=>group.mode==='graph').rows.length,0);
});
