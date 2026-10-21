const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const test = require('node:test');
function source(name) { return fs.readFileSync(path.join(__dirname, '../web/templates', name), 'utf8').match(/<script>([\s\S]*?)<\/script>/)[1]; }
function between(text, start, end) { return text.slice(text.indexOf(start), text.indexOf(end, text.indexOf(start))); }
class Element {
  constructor(tag) { this.tagName=tag;this.childNodes=[];this.textContent='';this.listeners={};this.style={}; }
  append(...nodes) { this.childNodes.push(...nodes); }
  replaceChildren(...nodes) { this.textContent='';this.childNodes=[...nodes]; }
  get options() { return this.childNodes; }
  setAttribute(name,value) { this[name]=value; }
  addEventListener(name,listener) { this.listeners[name]=listener; }
}
function deferred() { let resolve,reject;const promise=new Promise((a,b)=>{resolve=a;reject=b;});return {promise,resolve,reject}; }
const tick=()=>new Promise(resolve=>setImmediate(resolve));
function response(data) { return {ok:true,json:async()=>data}; }
function overview() {
  const nodes=new Map(),pending=[];
  const context={URL,AbortSignal,location:{href:'http://localhost/'},window:{addEventListener(){}},
    document:{createElement:tag=>new Element(tag),getElementById:id=>{if(!nodes.has(id))nodes.set(id,new Element('div'));return nodes.get(id);}},
    fetch:async url=>{if(new URL(url).pathname==='/api/adaptation-tasks')return response({tasks:[]});const item={url:new URL(url),...deferred()};pending.push(item);return item.promise;}};
  vm.createContext(context);
  vm.runInContext(source('overview.html').replace('  initialize();','  globalThis.ui={makeCard,loadCard,loadSessions,renderSnapshot,update,cards};'),context);
  const card=context.ui.makeCard({id:'vllm',engine:'vllm',default_branch:'next',label:'vLLM'});
  context.ui.cards.set('vllm',card);
  context.ui.renderSnapshot(card,{branch:'next',branches:['next','older'],engine:'vllm',upstream_version:'0.28.0',backends:[]});
  return {context,card,pending};
}
test('overview branch selection clears the previous version and ignores an older polling response',async()=>{
  const {context,card,pending}=overview();
  const polling=context.ui.loadCard(card);
  card.select.value='older';const switching=card.select.listeners.change();
  assert.equal(card.engine.textContent,'vLLM · 读取中');
  pending[1].resolve(response({branch:'older',engine:'vllm',upstream_version:'0.24.0',backends:[]}));await switching;
  pending[0].resolve(response({branch:'next',engine:'vllm',upstream_version:'0.28.0',backends:[]}));await polling;
  assert.equal(card.branch,'older');assert.equal(card.select.value,'older');assert.equal(card.engine.textContent,'vLLM 0.24.0');
  assert.match(card.engine.title,/所选分支.*pyproject.toml/);
});
test('overview failed branch selection shows an empty state for the selected branch',async()=>{
  const {card,pending}=overview();card.select.value='older';const switching=card.select.listeners.change();
  pending[0].reject(new Error('branch unavailable'));await switching;
  assert.equal(card.engine.textContent,'vLLM · 版本未识别');assert.equal(card.select.value,'older');
  assert.doesNotMatch(card.notice.textContent,/保留|next/);assert.equal(card.snapshot,null);assert.match(card.tbody.childNodes[0].childNodes[0].textContent,/此分支尚无/);assert.equal(card.feedback.textContent,'分支读取失败，可重试');
});
test('overview ignores aggregate snapshots started before a branch switch even after switching back',async()=>{
  const {context,card,pending}=overview();const polling=context.ui.update();
  card.select.value='older';let switching=card.select.listeners.change();pending[1].resolve(response({branch:'older',engine:'vllm',upstream_version:'0.24.0',backends:[]}));await switching;
  card.select.value='next';switching=card.select.listeners.change();pending[2].resolve(response({branch:'next',engine:'vllm',upstream_version:'0.29.0',backends:[]}));await switching;
  pending[0].resolve(response({projects:[{id:'vllm',status:{branch:'next',engine:'vllm',upstream_version:'0.28.0',backends:[]}}]}));await polling;
  assert.equal(card.engine.textContent,'vLLM 0.29.0');
});
function detail() {
  const nodes=new Map(),pending=[],notices=[];const byId=id=>{if(!nodes.has(id))nodes.set(id,new Element('div'));return nodes.get(id);};
  const context={URL,byId,put:(id,value)=>{byId(id).textContent=value;},appUrl:path=>'http://localhost/'+path,
    window:{location:{href:'http://localhost/monitor'},history:{replaceState(){}}},document:{hidden:false},
    latestBranchSnapshot:{branch:'next',engine:'vllm',upstream_version:'0.28.0'},sessionsEnabled:false,nativeTasksEnabled:false,taskCards:new Map(),el:(tag,style,text)=>{const node=new Element(tag);node.textContent=text||'';return node;},
    loadTasks(){},errorMessage:value=>value||'',showNotice:value=>{if(value)notices.push(value);},
    render:data=>{byId('vllm-version').textContent=data.upstream_version;byId('branch-select').value=data.branch;},
    fetch:async url=>{const item={url:new URL(url),...deferred()};pending.push(item);return item.promise;}};
  byId('branch-select').value='next';byId('vllm-version').textContent='0.28.0';
  vm.createContext(context);vm.runInContext(between(source('index.html'),'  let fetching = false;','  const taskLabels =')+'\nglobalThis.load=loadBranch;',context);
  return {context,pending,notices,byId};
}
test('detail clears old version immediately, ignores obsolete errors, and loads the selected branch',async()=>{
  const {context,pending,notices,byId}=detail();const polling=context.load('next');
  byId('branch-select').value='older';byId('branch-select').listeners.change();assert.equal(byId('vllm-version').textContent,'读取中…');
  pending[0].reject(new Error('obsolete failure'));await polling;await tick();assert.equal(notices.length,0);
  pending[1].resolve(response({branch:'older',upstream_version:'0.24.0'}));await tick();
  assert.equal(byId('branch-select').value,'older');assert.equal(byId('vllm-version').textContent,'0.24.0');
});
test('detail failed selected branch cannot retain the previous branch version',async()=>{
  const {pending,notices,byId}=detail();byId('branch-select').value='older';byId('branch-select').listeners.change();
  pending[0].reject(new Error('branch unavailable'));await tick();
  assert.equal(byId('vllm-version').textContent,'版本未识别');assert.doesNotMatch(notices[0],/保留|next/);assert.match(byId('backend-list').childNodes[0].textContent,/此分支尚无/);
});

const textOf=node=>node.textContent+node.childNodes.map(textOf).join('');
test('overview clears PR rows, counts, metadata and tasks immediately and updates branch links',async()=>{
  const {context,card,pending}=overview();
  context.ui.renderSnapshot(card,{branch:'next',branches:['next','older'],head:'old-head',head_url:'https://github.com/owner/repo/commit/old-head',backends:[{id:'cuda',label:'NVIDIA',pr:{number:487,url:'https://github.com/owner/repo/pull/487'},integration:{status:'merged'},ci:{status:'passed'},acceptance:{status:'passed',summary:'old acceptance'}}]});
  card.tasks.append(new Element('old-task'));
  assert.match(textOf(card.tbody),/PR #487/);
  card.select.value='older';const switching=card.select.listeners.change();
  assert.equal(card.snapshot,null);assert.doesNotMatch(textOf(card.tbody),/487|old acceptance/);
  assert.equal(card.tasks.childNodes.length,0);assert.equal(card.meta.childNodes.length,0);
  for(const node of Object.values(card.counts))assert.equal(node.textContent,'—');
  for(const node of [card.detail,card.taskLink,card.createLink])assert.equal(new URL(node.href).searchParams.get('branch'),'older');
  pending[0].resolve(response({branch:'older',backends:[]}));await switching;
});
test('overview same-branch collection failure keeps its valid rows and snapshot',async()=>{
  const {context,card,pending}=overview();const prior=card.snapshot;
  card.tbody.append(new Element('existing-row'));const priorRows=[...card.tbody.childNodes];
  const loading=context.ui.loadCard(card);pending[0].reject(new Error('temporary failure'));await loading;
  assert.equal(card.snapshot,prior);assert.deepEqual(card.tbody.childNodes,priorRows);assert.match(card.notice.textContent,/保留此分支/);
});
test('detail clears prior PR/CI results, counters and task controls before loading another branch',async()=>{
  const {context,pending,byId}=detail();
  for(const id of ['backend-list','task-list','observed-task-list']){const node=new Element('old');node.textContent='PR #487';byId(id).append(node);}
  context.taskCards.set('old',{});
  byId('branch-select').value='older';byId('branch-select').listeners.change();
  assert.equal(context.latestBranchSnapshot,null);assert.equal(context.taskCards.size,0);
  assert.doesNotMatch(textOf(byId('backend-list')),/487/);
  for(const id of ['task-list','observed-task-list'])assert.equal(byId(id).childNodes.length,0);
  for(const id of ['merged-count','ci-count','gate-count','case-count','head','adaptation-ci','branch-ci'])assert.equal(byId(id).textContent,'—');
  for(const id of ['repository-link','adaptation-ci-link','branch-ci-link'])assert.equal(byId(id).hidden,true);
  for(const id of ['header-new-task','new-task-link'])assert.equal(new URL(byId(id).href).searchParams.get('branch'),'older');
  pending[0].resolve(response({branch:'older',upstream_version:'0.24.0'}));await tick();
});
test('detail same-branch collection failure retains its valid snapshot',async()=>{
  const {context,pending,notices,byId}=detail();const prior=context.latestBranchSnapshot;
  const node=new Element('pr');node.textContent='PR #487';byId('backend-list').append(node);
  const loading=context.load('next');pending[0].reject(new Error('temporary failure'));await loading;
  assert.equal(context.latestBranchSnapshot,prior);assert.match(textOf(byId('backend-list')),/487/);assert.match(notices[0],/保留此分支/);
});
for(const page of ['overview','detail'])test(`${page} manual refresh rejects a snapshot for a different branch`,async()=>{
  const state=page==='overview'?overview():detail();const prior=page==='overview'?state.card.snapshot:state.context.latestBranchSnapshot;
  const refreshing=page==='overview'?state.card.refresh.listeners.click():state.byId('refresh-remote').listeners.click();
  state.pending[0].resolve(response({branch:'other',upstream_version:'wrong',backends:[]}));await refreshing;
  assert.equal(page==='overview'?state.card.snapshot:state.context.latestBranchSnapshot,prior);
  assert.match(page==='overview'?state.card.notice.textContent:state.notices[0],/分支不一致/);
});

test('detail task response cannot restore old branch cards after selection changes during JSON parsing',async()=>{
  const nodes=new Map(),pending=[],rendered=[],notices=[];
  const byId=id=>{if(!nodes.has(id)){const node=new Element('div');node.querySelectorAll=()=>[];nodes.set(id,node);}return nodes.get(id);};
  byId('branch-select').value='next';
  const context={byId,branchGeneration:0,initialBranch:'next',tasksFetching:false,sessionsEnabled:true,nativeTasksEnabled:false,
    taskCards:new Map(),backendTaskPolicies:{},taskSessions:[],latestBranchSnapshot:null,appUrl:path=>path,
    renderObservedTasks:()=>{},renderMachineProfiles:()=>{},prioritizedSessions:rows=>rows,isActiveTask:()=>true,
    render:()=>assert.fail('a stale task response must not rerender a snapshot'),
    reconcileTaskNodes:()=>rendered.push(byId('branch-select').value),
    el:(tag,style,text)=>{const node=new Element(tag);node.textContent=text||'';return node;},
    errorMessage:value=>value||'',taskNotice:message=>notices.push(message),
    fetch:async()=>{const item=deferred();pending.push(item);return {ok:true,json:()=>item.promise};}};
  vm.createContext(context);vm.runInContext(between(source('index.html'),'  async function loadTasks() {','  const requestedBranch =')+'\nglobalThis.load=loadTasks;',context);
  const old=context.load();await tick();
  byId('branch-select').value='older';context.branchGeneration++;await context.load();
  pending[0].resolve({sessions:[{id:'old-task',branch:'next',status:'running'}]});await old;await tick();
  assert.equal(pending.length,2,'load the latest branch after ignoring the old response');assert.equal(context.taskSessions.length,0);assert.equal(rendered.length,0);
  pending[1].resolve({sessions:[]});await tick();
  assert.deepEqual(rendered,['older']);assert.equal(notices.length,0);
});

for(const failure of [false,true])test(`overview ignores a late task ${failure?'error':'snapshot'} from the previous selection`,async()=>{
  const {context,card}=overview(),pending=deferred();context.fetch=()=>pending.promise;
  const loading=context.ui.loadSessions(card);card.generation++;card.branch='older';
  const selected=new Element('task');selected.textContent='selected branch task';card.tasks.replaceChildren(selected);
  if(failure)pending.reject(new Error('obsolete failure'));else pending.resolve(response({tasks:[]}));
  await loading;assert.equal(card.tasks.childNodes[0],selected);
});
