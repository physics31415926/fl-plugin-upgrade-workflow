const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const test = require('node:test');
const root = path.resolve(__dirname, '..');
function script(name) { return [...fs.readFileSync(path.join(root,name),'utf8').matchAll(/<script\b[^>]*>([\s\S]*?)<\/script>/g)].map(match=>match[1]).join('\n'); }
function between(text,start,end) { const a=text.indexOf(start),b=text.indexOf(end,a);assert(a>=0&&b>a,`source exists: ${start}`);return text.slice(a,b); }
class Element {
  constructor(tag) { this.tagName=tag.toUpperCase();this.childNodes=[];this.className='';this._text='';this.style={};this.attributes={};this.listeners={};this.isConnected=true; }
  set textContent(value) { this._text=String(value);this.childNodes=[]; }
  get textContent() { return this._text+this.childNodes.map(node=>typeof node==='string'?node:node.textContent).join(''); }
  append(...nodes) { this.childNodes.push(...nodes); }
  replaceChildren(...nodes) { this._text='';this.childNodes=[...nodes]; }
  get options() { return this.childNodes; }
  setAttribute(name,value) { this.attributes[name]=String(value); }
  getAttribute(name) { return this.attributes[name]; }
  addEventListener(name,listener) { this.listeners[name]=listener; }
  showModal() { this.open=true;this.modal=true; }
  close() { this.open=false;this.listeners.close?.({}); }
  remove() { this.removed=true;this.isConnected=false; }
  focus() { this.focused=true; }
}
function descendants(node) { return [node,...node.childNodes.filter(child=>typeof child!=='string').flatMap(descendants)]; }
function ui(name) {
  const source=script(name),nodes=new Map(),href='https://example.invalid/team/dashboard/monitor?project=sglang';
  const context={URL,URLSearchParams,location:{href,origin:'https://example.invalid'},window:{location:{href,origin:'https://example.invalid',search:'?project=sglang'}},
    document:{body:new Element('body'),createElement:tag=>new Element(tag),getElementById:id=>{if(!nodes.has(id))nodes.set(id,new Element('div'));return nodes.get(id);}},
    readOnly:false,safe:value=>value==null||value===''?'—':String(value),shortSha:value=>String(value||'').slice(0,10),reasonText:value=>value,scenarioLabels:{}};
  context.byId=id=>context.document.getElementById(id);
  vm.createContext(context);
  if(name==='overview.html') {
    vm.runInContext(between(source,'  const labels=', '  function makeCard(')
      +between(source,'  function acceptanceLink(', '  function isActiveTask(')
      +between(source,'  function renderSnapshot(', '  async function loadSessions(')
      +'\nglobalThis.result=acceptanceResult;globalThis.status=effectiveGate;globalThis.raw=rawGateStatus;globalThis.anchor=acceptanceLink;globalThis.dialog=openAcceptance;globalThis.render=renderSnapshot;globalThis.readonly=value=>readOnly=value;',context);
  } else {
    vm.runInContext(between(source,'  let projectId =', '  const labels =')
      +between(source,'  const labels =', '  const scenarioLabels =')
      +between(source,'  const isPassed =', '  function taskPolicy(')
      +between(source,'  function githubPrLink(', '  function policyNotice(')
      +between(source,'  function effectiveCi(', '  function branchOptions(')
      +between(source,'  function renderSummary(', '  function renderHeader(')
      +between(source,'  function expectedRows(', '  function renderBackend(')
      +'\nglobalThis.result=acceptanceResult;globalThis.status=effectiveGate;globalThis.raw=rawGateStatus;globalThis.anchor=acceptanceLink;globalThis.dialog=openAcceptance;globalThis.primary=renderAcceptance;globalThis.summary=renderSummary;',context);
    context.backendOrder=(a,b)=>(a.id!=='cuda')-(b.id!=='cuda')||String(a.label||a.id).localeCompare(String(b.label||b.id));
    context.readonly=value=>{context.readOnly=value;};
  }
  return {context,nodes};
}
const pr='https://github.com/example/plugin/pull/123';
const primary={status:'passed',source:'pr_description',summary:'作者报告：模型 A eager 8 请求通过',pr_number:123,pr_url:pr,title:'PR 测试结果'};
for(const page of ['overview.html','index.html']) {
  test(`${page}: unified acceptance takes priority over raw and historical status without rewriting evidence`,()=>{
    const {context}=ui(page);
    for(const status of ['passed','partial','failed','pending','missing','reference','unknown']) {
      const backend={integration:{status:'merged'},acceptance:{...primary,status},acceptance_records:[{status:'passed',tested_commit:'historical'}],gate:{status:'passed',latest_commit:'old',tree_matches_head:false,passed:16},results:[{status:'passed',commit:'old'}]};
      const original=JSON.stringify(backend);
      assert.equal(context.status(backend,'current'),status);
      assert.equal(context.result(backend,'current'),backend.acceptance);
      assert.equal(context.raw(backend,'current'),'stale','raw per-case status remains bound to its old commit');
      assert.equal(JSON.stringify(backend),original);
    }
    assert.equal(context.status({acceptance:primary,gate:{status:'failed'}},'current'),'passed');
    assert.equal(context.status({acceptance:{...primary,status:'partial'},gate:{status:'missing'}},'current'),'partial');
  });
  test(`${page}: local archive links preserve identity while LAN links only to the PR`,()=>{
    const {context}=ui(page),record={...primary,source:'local_record',archive_available:true,archive_url:'/api/acceptance-record?backend=ascend&record=fixture'};
    const local=context.anchor(record,'sglang','dev/next');
    const archive=new URL(local.href);
    assert.equal(archive.origin,'https://example.invalid');
    assert.equal(archive.pathname,'/team/dashboard/api/acceptance-record');
    assert.equal(archive.searchParams.get('project'),'sglang');
    assert.equal(archive.searchParams.get('record'),'fixture');
    context.readonly(true);
    assert.equal(context.anchor(record,'sglang','dev/next').href,pr);
    assert.equal(context.anchor({...record,pr_url:null},'sglang','dev/next'),null);
  });
  test(`${page}: acceptance links reject arbitrary hosts, credentials and non-PR destinations`,()=>{
    const {context}=ui(page);
    for(const archive_url of ['https://other.invalid/api/acceptance-record','https://user:secret@example.invalid/api/acceptance-record','/api/codex-sessions','javascript:alert(1)']) {
      const anchor=context.anchor({...primary,archive_available:true,archive_url},'sglang','dev/next');
      assert.equal(anchor.href,pr,'unsafe archives fall back to the validated PR');
    }
    for(const pr_url of ['javascript:alert(1)','http://github.com/example/plugin/pull/123','https://github.com.evil.invalid/example/plugin/pull/123','https://user:secret@github.com/example/plugin/pull/123','https://github.com/example/plugin/actions/runs/123']) {
      assert.equal(context.anchor({...primary,pr_url},'sglang','dev/next'),null);
    }
  });
}
function card() {
  return {project:{id:'sglang'},branch:'dev/next',select:new Element('select'),engine:new Element('span'),meta:new Element('div'),
    counts:{merged:new Element('strong'),ci:new Element('strong'),gate:new Element('strong')},collection:new Element('div'),notice:new Element('div'),tbody:new Element('tbody'),detail:new Element('a'),taskLink:new Element('a')};
}
test('overview merges acceptance into one column and counts PR passes with no raw case list',()=>{
  const {context}=ui('overview.html'),view=card();
  const data={branch:'dev/next',head:'current',cases:[],backends:[
    {id:'corex',label:'iluvatar',acceptance:{...primary,status:'failed'}},
    {id:'ascend',label:'Ascend',acceptance:{...primary,status:'partial'}},
    {id:'cuda',label:'NVIDIA',acceptance:primary,gate:{status:'missing'}}]};
  const original=JSON.stringify(data);context.render(view,data);
  assert.equal(view.counts.gate.textContent,'1 / 3');
  assert.equal(view.notice.hidden,true,'absence of raw cases does not imply PR acceptance failure');
  assert.deepEqual(view.tbody.childNodes.map(row=>row.childNodes[0].childNodes[0].textContent),['NVIDIA','Ascend','iluvatar']);
  for(const row of view.tbody.childNodes)assert.equal(row.childNodes.length,4,'only one acceptance column');
  const first=view.tbody.childNodes[0].childNodes[3];
  assert.equal(descendants(first).filter(node=>node.tagName==='A').length,0,'acceptance status no longer navigates directly');
  const button=descendants(first).find(node=>node.tagName==='BUTTON');assert(button);assert.equal(button.attributes['aria-haspopup'],'dialog');button.listeners.click();
  const modal=context.document.body.childNodes[0];assert.equal(modal.tagName,'DIALOG');assert.equal(modal.modal,true);assert.equal(descendants(modal).find(node=>node.tagName==='A').href,pr);
  assert.match(first.textContent,/查看表格/);assert.match(first.textContent,/作者报告/);
  assert.equal(JSON.stringify(data),original);
  const html=fs.readFileSync(path.join(root,'overview.html'),'utf8');
  assert.match(html,/'验收结果'/);assert.match(html,/'验收通过'/);
  assert.doesNotMatch(html,/'当前 Gate'|'实验验收记录'/);
});
test('detail presents PR summary first and folds unchanged raw stale cases into auxiliary evidence',()=>{
  const {context,nodes}=ui('index.html');
  const data={head:'current',models:['Model A'],modes:['eager'],cases:[{scenario:'text_single',label:'Single request',requests:1}]};
  const backend={acceptance:primary,integration:{status:'merged'},gate:{status:'passed',latest_commit:'old',tree_matches_head:false,passed:1,total:1},
    results:[{model:'Model A',mode:'eager',scenario:'text_single',status:'passed',reason:'All requests passed'}],acceptance_records:[{status:'partial',summary:'历史子集',tested_commit:'historical',pr_url:pr,pr_number:123}]};
  const original=JSON.stringify(backend),section=context.primary(data,backend);
  assert.equal(section.childNodes[0].textContent,'验收结果');
  const main=section.childNodes[1];assert.match(main.textContent,/作者报告/);
  const open=descendants(main).find(node=>node.tagName==='BUTTON');assert(open);open.listeners.click();assert.equal(descendants(context.document.body.childNodes[0]).find(node=>node.tagName==='A').href,pr);
  const auxiliary=section.childNodes.find(node=>node.tagName==='DETAILS');
  assert(auxiliary);assert.notEqual(auxiliary.open,true,'raw cases are auxiliary and collapsed by default');
  assert.match(auxiliary.textContent,/历史测试版本/);assert.match(auxiliary.textContent,/证据过期/);
  assert.match(auxiliary.textContent,/不覆盖上方 PR/);
  context.summary({...data,backends:[backend]});
  assert.equal(nodes.get('gate-count').textContent,'1 / 1');
  assert.equal(nodes.get('case-count').textContent,'0 / 1','PR passes must not rewrite old raw-case coverage');
  assert.equal(JSON.stringify(backend),original);
  const empty=context.primary({head:'current',models:[],modes:[],cases:[]},{acceptance:primary,gate:{status:'missing'}});
  assert.equal(descendants(empty.childNodes[1]).find(node=>node.className==='pill passed').textContent,'通过');
  assert.match(empty.childNodes[1].textContent,/作者报告/);
});

for(const page of ['overview.html','index.html']) {
  test(`${page}: acceptance modal renders sanitized text rows, bounds scope, and restores keyboard focus`,()=>{
    const {context}=ui(page),opener=new Element('button'),rows=[{label:'Model A',configuration:'eager / single',result:'作者报告 1/1',status:'passed'},{label:'<img src=x onerror=alert(1)>',configuration:'graph',result:'未覆盖',status:'pending'}];
    context.readonly(true);context.fetch=()=>{throw Error('modal must not request archive data');};
    const original=JSON.stringify(rows),modal=context.dialog({...primary,rows,rows_total:120,rows_truncated:true,archive_available:true,archive_url:'/api/acceptance-record?record=private'},'NVIDIA',opener);
    assert.equal(modal.tagName,'DIALOG');assert.equal(modal.modal,true);assert.match(modal.attributes['aria-label'],/NVIDIA/);
    const table=descendants(modal).find(node=>node.tagName==='TABLE'),body=table.childNodes.find(node=>node.tagName==='TBODY');
    assert.equal(body.childNodes.length,2);assert.equal(body.childNodes[0].childNodes[1].textContent,'eager / single');assert.equal(body.childNodes[1].childNodes[0].textContent,rows[1].label);
    assert.equal(descendants(modal).filter(node=>node.tagName==='IMG').length,0);assert.match(modal.textContent,/PR 作者报告/);assert.match(modal.textContent,/2 条 \/ 共 120 条/);
    const links=descendants(modal).filter(node=>node.tagName==='A');assert.equal(links.length,1);assert.equal(links[0].href,pr);assert.match(links[0].textContent,/原 PR/);
    const close=descendants(modal).find(node=>node.tagName==='BUTTON');assert.equal(close.type,'button');assert.equal(close.autofocus,true);
    let prevented=false;modal.listeners.cancel({preventDefault(){prevented=true;}});assert.equal(prevented,true);assert.equal(modal.open,false);assert.equal(modal.removed,true);assert.equal(opener.focused,true);assert.equal(JSON.stringify(rows),original);
  });
  test(`${page}: empty and capped row lists explain missing detail and keep the original PR secondary`,()=>{
    const {context}=ui(page),empty=context.dialog({...primary,rows:[]},'Ascend',new Element('button'));
    assert.match(empty.textContent,/暂无可识别的验收表格/);assert.equal(descendants(empty).filter(node=>node.tagName==='TABLE').length,0);assert.equal(descendants(empty).find(node=>node.tagName==='A').href,pr);
    const large=context.dialog({...primary,rows:Array.from({length:130},(_,i)=>({label:String(i),status:'passed'})),rows_total:130},'Ascend',new Element('button'));
    assert.equal(descendants(large).find(node=>node.tagName==='TBODY').childNodes.length,100);assert.match(large.textContent,/100 条 \/ 共 130 条/);
  });
}
