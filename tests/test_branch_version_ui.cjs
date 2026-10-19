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
  vm.runInContext(source('overview.html').replace('  initialize();','  globalThis.ui={makeCard,loadCard,renderSnapshot,update,cards};'),context);
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
test('overview failed branch selection shows unknown version and identifies retained snapshot',async()=>{
  const {card,pending}=overview();card.select.value='older';const switching=card.select.listeners.change();
  pending[0].reject(new Error('branch unavailable'));await switching;
  assert.equal(card.engine.textContent,'vLLM · 版本未识别');assert.equal(card.select.value,'older');
  assert.match(card.notice.textContent,/下方保留 next/);assert.equal(card.feedback.textContent,'分支读取失败，可重试');
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
    latestBranchSnapshot:{branch:'next',engine:'vllm',upstream_version:'0.28.0'},sessionsEnabled:false,nativeTasksEnabled:false,
    loadTasks(){},errorMessage:value=>value||'',showNotice:value=>notices.push(value),
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
  assert.equal(byId('vllm-version').textContent,'版本未识别');assert.match(notices[0],/下方保留 next/);
});
