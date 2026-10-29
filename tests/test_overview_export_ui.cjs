const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const test = require('node:test');
const html = fs.readFileSync(path.join(__dirname, '../web/templates/overview.html'), 'utf8');
class Element {
  constructor(tag) { this.tagName=tag;this.childNodes=[];this.textContent='';this.listeners={};this.style={}; }
  append(...nodes) { this.childNodes.push(...nodes); }
  replaceChildren(...nodes) { this.textContent='';this.childNodes=[...nodes]; }
  get options() { return this.childNodes; }
  setAttribute(name,value) { this[name]=value; }
  addEventListener(name,fn) { this.listeners[name]=fn; }
}
function harness({module=true}={}) {
  const nodes=new Map(),downloads=[];
  const node=id=>{if(!nodes.has(id))nodes.set(id,new Element('div'));return nodes.get(id);};
  const context={URL,location:{href:'https://example.invalid/dashboard/'},window:{addEventListener(){}},
    document:{createElement:tag=>new Element(tag),getElementById:node},
    fetch(){throw new Error('Export must not fetch or refresh');}};
  if(module)context.FLOverviewExport={download:(projects,options)=>downloads.push({projects,options})};
  vm.createContext(context);
  vm.runInContext(html.match(/<script>([\s\S]*?)<\/script>/)[1].replace('  initialize();','  globalThis.ui={cards,makeCard,renderSnapshot,clearSelectedBranch,showError,exportProjects,updateExportState,update};'),context);
  const card=(id,branch)=>{const c=context.ui.makeCard({id,label:id,default_branch:branch,engine:id,sessions:false});context.ui.cards.set(id,c);return c;};
  return {context,node,downloads,card,click:()=>node('export-overview').listeners.click()};
}
const snapshot=(branch,extra={})=>({branch,branches:[branch],head:'a'.repeat(40),observed_at:'2026-01-02T03:04:05Z',engine:'vllm',upstream_version:'0.28.0',
  collection:{has_snapshot:true,cached:true,stale:false,refreshing:false,error:null},
  backends:[{id:'cuda',label:'NVIDIA',pr:{number:12,title:'Adaptation',url:'https://github.com/example/plugin/pull/12'},integration:{status:'merged'},ci:{status:'passed'},acceptance:{status:'passed',source:'pr_description',summary:'验证通过',rows:[{label:'text',result:'passed'}]}}],...extra});

test('HTML export is visible with a relative module and waits for usable snapshots',()=>{
  assert.match(html,/<script src="assets\/overview-export\.js"><\/script>/);
  assert.match(html,/<button id="export-overview"[^>]*disabled/);
  const h=harness(),a=h.card('vllm','next');h.context.ui.updateExportState();
  assert.equal(h.node('export-overview').disabled,true);
  h.context.ui.renderSnapshot(a,snapshot('next'));
  assert.equal(h.node('export-overview').disabled,false);
});
test('read-only export captures both selected branches with zero requests',()=>{
  const h=harness(),a=h.card('vllm','chosen-v'),b=h.card('sglang','chosen-s');
  h.context.ui.renderSnapshot(a,snapshot('chosen-v'));
  h.context.ui.renderSnapshot(b,snapshot('chosen-s',{engine:'sglang',upstream_version:'0.5.18',read_only:true}));
  h.click();assert.equal(h.downloads.length,1);
  const rows=h.downloads[0].projects;
  assert.deepEqual(Array.from(rows,p=>p.branch),['chosen-v','chosen-s']);
  assert.equal(rows[1].version,'0.5.18');assert.equal(rows[0].observed_at,'2026-01-02T03:04:05Z');
  assert.match(h.node('export-feedback').textContent,/已生成 HTML/);
});
test('branch changes cannot export the previous branch snapshot',()=>{
  const h=harness(),a=h.card('vllm','before'),b=h.card('sglang','stable');
  h.context.ui.renderSnapshot(a,snapshot('before'));h.context.ui.renderSnapshot(b,snapshot('stable'));
  a.branch='after';h.context.ui.clearSelectedBranch(a);h.click();
  assert.equal(h.downloads[0].projects[0].branch,'after');
  assert.equal(h.downloads[0].projects[0].unavailable,true);
  assert.equal(h.downloads[0].projects[0].backends,undefined);
  b.branch='also-loading';h.context.ui.clearSelectedBranch(b);h.click();
  assert.equal(h.downloads.length,1);assert.equal(h.node('export-overview').disabled,true);
});
test('cached and failed refresh states remain explicit without exporting diagnostics',()=>{
  const h=harness(),a=h.card('vllm','next');
  h.context.ui.renderSnapshot(a,snapshot('next',{collection:{has_snapshot:true,cached:true,stale:true,refreshing:true,error:'private diagnostic'}}));
  h.context.ui.showError(a,'private host address');h.click();
  const row=h.downloads[0].projects[0];assert.equal(row.stale,true);assert.equal(row.refreshing,true);assert.equal(row.hasError,true);
  assert.equal(JSON.stringify(row).includes('private'),false);
});
test('allowlisted report data never copies task, device or archive provenance',()=>{
  const h=harness(),a=h.card('vllm','next');const data=snapshot('next');
  data.sessions=[{message:'private-message'}];data.api_key='private-key';data.backends[0].acceptance.raw_body='private-body';
  data.backends[0].acceptance.archive_url='/api/private-archive';data.backends[0].acceptance.rows[0].provenance='private-provenance';
  data.backends[0].acceptance.description_blocks=[{type:'paragraph',text:'private-prose'},{type:'table',rows:[['case','result'],['test','passed']]}];
  h.context.ui.renderSnapshot(a,data);h.click();const output=JSON.stringify(h.downloads[0].projects);
  assert.doesNotMatch(output,/private-/);assert.match(output,/"type":"table"/);
});
test('export retains the overview stale-CI and acceptance fallback semantics',()=>{
  const h=harness(),a=h.card('vllm','next');const data=snapshot('next');
  data.backends[0].ci={status:'passed',scope:'branch',commit:'b'.repeat(40)};
  delete data.backends[0].acceptance;data.backends[0].gate={status:'missing'};data.backends[0].acceptance_records=[{status:'passed',summary:'recorded validation'}];
  h.context.ui.renderSnapshot(a,data);h.click();const backend=h.downloads[0].projects[0].backends[0];
  assert.equal(backend.ci,'stale');assert.equal(backend.acceptance.status,'passed');assert.equal(backend.acceptance.source,'local_record');
});
test('unavailable module and download failures show recoverable feedback',()=>{
  let h=harness({module:false}),a=h.card('vllm','next');h.context.ui.renderSnapshot(a,snapshot('next'));
  assert.equal(h.node('export-overview').disabled,true);h.click();assert.equal(h.downloads.length,0);assert.match(h.node('export-feedback').textContent,/未完成/);
  h=harness();a=h.card('vllm','next');h.context.ui.renderSnapshot(a,snapshot('next'));
  h.context.FLOverviewExport.download=()=>{throw new Error('private download error');};h.click();
  assert.match(h.node('export-feedback').textContent,/未完成/);assert.doesNotMatch(h.node('export-feedback').textContent,/private/);
});

test('aggregate network failure marks a retained export as an incomplete refresh',async()=>{
  const h=harness(),a=h.card('vllm','next');h.context.ui.renderSnapshot(a,snapshot('next'));
  await h.context.ui.update();h.click();assert.equal(h.downloads[0].projects[0].hasError,true);
  h.context.ui.renderSnapshot(a,snapshot('next'));h.click();assert.equal(h.downloads[1].projects[0].hasError,false);
});
