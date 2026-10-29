const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const test = require('node:test');
const root = path.resolve(__dirname, '..');
function source(name) { return [...fs.readFileSync(path.join(root,name),'utf8').matchAll(/<script\b[^>]*>([\s\S]*?)<\/script>/g)].map(match=>match[1]).join('\n'); }
function between(text,start,end) { const first=text.indexOf(start),last=text.indexOf(end,first);assert(first>=0&&last>first);return text.slice(first,last); }

test('all dashboard scripts parse', () => { for(const name of ['web/templates/index.html','web/templates/sessions.html','web/templates/overview.html','web/templates/workflow.html']) new vm.Script(source(name),{filename:name}); });
test('detail APIs and evidence retain SGLang identity with existing query fields', () => {
  const text=source('web/templates/index.html');const context={URL,URLSearchParams,window:{location:{href:'http://127.0.0.1:8766/monitor?project=sglang',search:'?project=sglang'}}};vm.createContext(context);
  vm.runInContext(between(text,'  let projectId =','  const labels =')+'\nglobalThis.route=appUrl;',context);
  const result=new URL(context.route('api/branch-evidence?branch=dev%2F0.5.18&backend=cuda&model=Qwen3.6-27B&mode=eager&scenario=concurrent_text_16'));
  assert.equal(result.searchParams.get('project'),'sglang');assert.equal(result.searchParams.get('branch'),'dev/0.5.18');assert.equal(result.searchParams.get('scenario'),'concurrent_text_16');
  const task=new URL(context.route('sessions?session=example&branch=dev%2F0.5.18'));assert.equal(task.searchParams.get('session'),'example');assert.equal(task.searchParams.get('project'),'sglang');
});
test('session follow-ups and navigation retain project without overwriting branch', () => {
  const text=source('web/templates/sessions.html');const context={URL,URLSearchParams,location:{href:'http://127.0.0.1:8766/sessions?project=sglang',search:'?project=sglang'}};vm.createContext(context);
  vm.runInContext(between(text,"  let projectId =",'  const labels =')+'\nglobalThis.route=projectUrl;globalThis.monitor=monitorUrl;',context);
  assert.equal(new URL(context.route('/api/codex-sessions/id/message')).searchParams.get('project'),'sglang');
  const target=new URL(context.monitor('dev/0.5.18','session-example'));assert.equal(target.pathname,'/monitor');assert.equal(target.searchParams.get('branch'),'dev/0.5.18');assert.equal(target.searchParams.get('session'),'session-example');assert.equal(target.searchParams.get('project'),'sglang');
});
test('overview uses separate repo branches and preserves stale acceptance status', () => {
  const text=source('web/templates/overview.html');const context={URL,location:{href:'https://example.invalid/team/dashboard/'}};vm.createContext(context);
  vm.runInContext(between(text,'  function appUrl(', '  function badge(')+'\nglobalThis.route=appUrl;globalThis.ci=effectiveCi;globalThis.gate=effectiveGate;',context);
  const a=new URL(context.route('monitor','vllm','0.4.0-dev')),b=new URL(context.route('monitor','sglang','dev/0.5.18'));assert.equal(a.pathname,'/team/dashboard/monitor');assert.equal(a.searchParams.get('project'),'vllm');assert.equal(b.searchParams.get('branch'),'dev/0.5.18');
  assert.equal(context.ci({ci:{status:'passed',scope:'branch',commit:'old'}},'new'),'stale');assert.equal(context.gate({gate:{status:'passed',latest_commit:'old'}},'new'),'stale');assert.equal(context.gate({gate:{status:'recorded_pass',tree_matches_head:true,latest_commit:'old'}},'new'),'recorded_pass');
});

test('SGLang source cases only appear for their own model and runtime mode', () => {
  const text=source('web/templates/index.html'),context={};vm.createContext(context);
  vm.runInContext(between(text,'  function expectedRows(', '  function renderMatrix(')+'\nglobalThis.rows=expectedRows;',context);
  const data={models:['model-a','model-b'],modes:['eager','graph'],cases:[{scenario:'a',models:['model-a'],modes:['eager']},{scenario:'b',models:['model-b'],modes:['eager']} ]};
  const result=context.rows(data,{results:[]});assert.equal(result.find(group=>group.model==='model-a'&&group.mode==='eager').rows.length,1);assert.equal(result.find(group=>group.model==='model-b'&&group.mode==='eager').rows[0].descriptor.scenario,'b');assert.equal(result.find(group=>group.mode==='graph').rows.length,0);
});


function gateDisplayFor(name, field = 'gate') {
  const text=source(name),context={URL,location:{href:'https://example.invalid/dashboard/'}};
  vm.createContext(context);
  const implementation=name==='web/templates/overview.html'
    ? between(text,'  function appUrl(', '  function badge(')
    : between(text,'  const labels =', '  const scenarioLabels =')
      +between(text,'  const isPassed =', '  function prStatusText(')
      +between(text,'  function effectiveCi(', '  function branchOptions(');
  vm.runInContext(implementation+'\nglobalThis.gate=effectiveGate;globalThis.ci=effectiveCi;',context);
  return context[field];
}
for(const page of ['web/templates/overview.html','web/templates/index.html']) {
  test(`${page}: historical passed or partial acceptance displays passed with old or missing raw Gate`, () => {
    const display=gateDisplayFor(page);
    for(const acceptanceStatus of ['passed','partial']) {
      const records=[{status:'reference',source:'pull_request'},{status:'failed'},{status:acceptanceStatus,tested_commit:'historical',scope:'historical subset'}];
      for(const rawGate of [
        {status:'passed',latest_commit:'old',tree_matches_head:false,results:[{status:'passed'}]},
        {status:'missing',passed:0,total:16,results:[]},
        undefined
      ]) {
        const backend={integration:{status:'merged'},acceptance_records:records};
        if(rawGate)backend.gate=rawGate;
        const original=JSON.stringify(backend);
        assert.equal(display(backend,'current'),'passed',`${acceptanceStatus} acceptance, ${rawGate?.status||'no raw Gate'}`);
        assert.equal(JSON.stringify(backend),original,'display policy must leave raw Gate and per-case evidence unchanged');
      }
    }
  });
  test(`${page}: PR reference and failed records do not count as passed acceptance`, () => {
    const display=gateDisplayFor(page);
    for(const records of [[{status:'reference',source:'pull_request'}],[{status:'failed'}],[{status:'reference'},{status:'failed'}]]) {
      assert.equal(display({integration:{status:'merged'},gate:{status:'passed',latest_commit:'old'},acceptance_records:records},'current'),'stale');
      assert.equal(display({gate:{status:'missing'},acceptance_records:records},'current'),'missing');
      assert.equal(display({gate:{status:'failed'},acceptance_records:records},'current'),'failed');
    }
  });
  test(`${page}: absent acceptance records retain existing stale Gate behavior`, () => {
    const display=gateDisplayFor(page);
    for(const records of [undefined,[]]) {
      const backend={integration:{status:'merged'},gate:{status:'passed',latest_commit:'old',tree_matches_head:false}};
      if(records)backend.acceptance_records=records;
      assert.equal(display(backend,'current'),'stale');
      assert.equal(display({...backend,gate:{status:'passed',latest_commit:'current'}},'current'),'passed');
      assert.equal(display({...backend,gate:{status:'missing'}},'current'),'missing');
    }
  });
}


test('overview hides branch-wide CI while preserving backend CI and coverage', () => {
  class Node {
    constructor(tag) { this.tagName=tag.toUpperCase();this.className='';this.textContent='';this.childNodes=[]; }
    append(...nodes) { this.childNodes.push(...nodes); }
    replaceChildren(...nodes) { this.textContent='';this.childNodes=[...nodes]; }
    get options() { return this.childNodes; }
    setAttribute(name,value) { this[name]=String(value); }
    addEventListener(name,listener) { (this.listeners||={})[name]=listener; }
  }
  const textOf=node=>node.textContent+node.childNodes.map(textOf).join('');
  const text=source('web/templates/overview.html'),context={URL,$:()=>null,location:{href:'https://example.invalid/dashboard/'},document:{createElement:tag=>new Node(tag)}};
  vm.createContext(context);
  const implementation=between(text,'  const labels=', '  function makeCard(')
    +between(text,'  function acceptanceLink(', '  function isActiveTask(')
    +between(text,'  function renderSnapshot(', '  async function loadSessions(');
  vm.runInContext(implementation+'\nglobalThis.render=renderSnapshot;',context);
  const card={project:{id:'vllm'},branch:'dev',select:new Node('select'),engine:new Node('span'),meta:new Node('div'),
    counts:{merged:new Node('strong'),ci:new Node('strong'),gate:new Node('strong')},collection:new Node('div'),
    notice:new Node('div'),tbody:new Node('tbody'),detail:new Node('a'),taskLink:new Node('a')};
  const data={branch:'dev',branches:['dev'],head:'current',observed_at:'2026-01-01T00:00:00Z',cases:[{scenario:'fixture_case'}],
    branch_ci:{status:'failed',url:'https://github.com/owner/repo/actions/runs/123'},
    backends:[
      {id:'cuda',label:'NVIDIA',integration:{status:'merged'},ci:{status:'passed',scope:'accepted-tree'},
        gate:{status:'missing',total:20,requests_total:104}},
      {id:'ascend',label:'Ascend',integration:{status:'merged'},ci:{status:'failed',scope:'branch',commit:'current'},
        gate:{status:'missing',total:20,requests_total:104}}
    ]};
  for(const branchStatus of ['failed','running','passed']) {
    data.branch_ci.status=branchStatus;
    const original=JSON.stringify(data);
    context.render(card,data);
    assert.equal(textOf(card.collection),'\u9010\u9879\u8986\u76d6 20 \u573a\u666f \u00b7 104 \u8bf7\u6c42 / backend');
    assert.equal(card.collection.childNodes.length,1,'collection must contain coverage alone');
    assert.equal(card.collection.childNodes[0].tagName,'SPAN');
    assert.equal(card.collection.childNodes[0].className,'','coverage is not a CI status badge');
    assert.equal(card.counts.ci.textContent,'1 / 2');
    assert.equal(card.counts.merged.textContent,'2 / 2');
    assert.equal(card.tbody.childNodes[0].childNodes[0].childNodes[0].textContent,'NVIDIA');
    assert.equal(card.tbody.childNodes[0].childNodes[2].childNodes[0].className,'pill passed');
    assert.equal(card.tbody.childNodes[1].childNodes[2].childNodes[0].className,'pill failed');
    assert.equal(JSON.stringify(data),original,'display must not alter CI data');
  }
});

for (const page of ['web/templates/overview.html','web/templates/index.html']) {
  test(`${page}: merged adaptation backend CI keeps its PR result across later branch revisions`, () => {
    const display = gateDisplayFor(page, 'ci');
    const backend = {integration:{status:'merged'},ci:{status:'passed',scope:'merged-pr',commit:'tested-pr'},
      current_ci:{status:'failed',scope:'branch',commit:'later-head'}};
    const original = JSON.stringify(backend);
    for (const head of ['merge-head','later-head','another-head']) assert.equal(display(backend,head),'passed');
    assert.equal(JSON.stringify(backend),original);
    for (const status of ['failed','unknown','running','partial','skipped']) {
      assert.equal(display({...backend,ci:{...backend.ci,status}},'later-head'),status);
    }
    assert.equal(display({ci:{status:'passed',scope:'branch',commit:'old-head'}},'later-head'),'stale');
  });
}


test('overview aligns cards within each visual row when more projects are added', () => {
  const html=source('web/templates/overview.html');
  const makeRoot=(top,height)=>{
    const nodes=new Map(['.project-head','.metrics','.collection','.notice','.table-wrap'].map(key=>[key,{style:{},getBoundingClientRect:()=>({top:top+height,height})}]));
    return {nodes,getBoundingClientRect:()=>({top}),querySelector:key=>nodes.get(key),querySelectorAll:()=>[]};
  };
  const roots=[makeRoot(0,30),makeRoot(0,50),makeRoot(500,70)];
  const context={cards:new Map(roots.map((root,i)=>[i,{root}])),window:{innerWidth:1200}};
  vm.createContext(context);
  vm.runInContext(between(html,'  function alignProjects(){','  function scheduleAlignment()')+'\nalignProjects();',context);
  assert.equal(roots[0].nodes.get('.table-wrap').style.paddingTop,'20px');
  assert.equal(roots[1].nodes.get('.table-wrap').style.paddingTop,'0px');
  assert.equal(roots[2].nodes.get('.table-wrap').style.paddingTop,'0px');
});
