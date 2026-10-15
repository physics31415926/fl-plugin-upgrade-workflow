const assert=require('node:assert/strict');
const fs=require('node:fs');
const path=require('node:path');
const vm=require('node:vm');
const test=require('node:test');
const root=path.resolve(__dirname,'..');
const html=fs.readFileSync(path.join(root,'web/templates/sessions.html'),'utf8');
function script(name){return [...fs.readFileSync(path.join(root,name),'utf8').matchAll(/<script\b[^>]*>([\s\S]*?)<\/script>/g)].map(match=>match[1]).join('\n');}
const source=script('web/templates/sessions.html');
function between(text,start,end){const a=text.indexOf(start),b=text.indexOf(end,a);assert(a>=0&&b>a,`source exists: ${start}`);return text.slice(a,b);}
class Element {
  constructor(tag){this.tag=tag;this.children=[];this.parent=null;this.listeners={};this.hidden=false;this.checked=false;this._text='';}
  set textContent(value){this._text=String(value);this.children=[];}
  get textContent(){return this._text+this.children.map(child=>child.textContent).join('');}
  append(...nodes){for(const node of nodes){node.remove();node.parent=this;this.children.push(node);}}
  replaceChildren(...nodes){for(const child of this.children)child.parent=null;this.children=[];this._text='';this.append(...nodes);}
  remove(){if(this.parent){const i=this.parent.children.indexOf(this);if(i>=0)this.parent.children.splice(i,1);this.parent=null;}}
  setAttribute(name,value){this[name]=value;}
  addEventListener(name,callback){this.listeners[name]=callback;}
  contains(node){return node===this||this.children.some(child=>child.contains(node));}
  get firstElementChild(){return this.children[0]||null;}
  get nextElementSibling(){return this.parent?.children[this.parent.children.indexOf(this)+1]||null;}
  insertBefore(node,cursor){node.remove();const i=cursor?this.children.indexOf(cursor):this.children.length;this.children.splice(i<0?this.children.length:i,0,node);node.parent=this;}
}
function task(id,status,extras={}){return {id,status,backend:'cuda',branch:'main',ssh_alias:'gpu-demo',...extras};}
function ui(sessions){
  const nodes=new Map(),calls=[];
  const context={document:{activeElement:null},sessionListCards:new Map(),selectionTarget:{session:null,branch:null,backend:null},labels:{cuda:'NVIDIA / CUDA'},statuses:Object.fromEntries(['prepared','starting','running','waiting_approval','waiting_input','interrupting','completed','failed','interrupted','disconnected'].map(value=>[value,value])),API:'/api/codex-sessions',encodeURIComponent,
    $:id=>{if(!nodes.has(id))nodes.set(id,new Element('div'));return nodes.get(id);},
    el:(tag,cls,text)=>{const node=new Element(tag);node.className=cls||'';if(text!==undefined)node.textContent=text;return node;},
    notice:()=>{},renderDetail:()=>{},refresh:async()=>{},post:async(url,body)=>calls.push({url,body}),providerAvailable:()=>true,
    statusLabel:status=>status,providerName:()=> 'Codex',formatTime:()=> 'test-time',reportedProvenance:()=>null,sessionProgress:()=>'',latestSessionMessage:()=>'',planMeta:()=>'',progressWarningText:()=>''};
  vm.createContext(context);
  const declaration=source.match(/const state = (\{[^\n]*\});/);
  assert(declaration,'actual page state initializer');
  vm.runInContext('globalThis.state = '+declaration[1]+';',context);
  context.state.sessions=sessions;
  vm.runInContext(between(source,'  function current(', '  function approvalDecisions(')
    +between(source,'  function renderList(', '  function eventOrigin(')
    +between(source,'  async function act(', '  async function create(')
    +between(source,"  $('show-history').addEventListener(","  $('profile').addEventListener("),context);
  const listed=()=>context.$('session-list').children.map(button=>[...context.sessionListCards].find(([,entry])=>entry.button===button)[0]);
  return {context,nodes,calls,listed};
}

test('sessions list defaults to active tasks and history checkbox reveals retained records',()=>{
  const active=['prepared','starting','running','waiting_approval','waiting_input','interrupting'];
  const inactive=['completed','failed','interrupted','disconnected','unknown'];
  const sessions=[...inactive.map(status=>task(status,status)),...active.map(status=>task(status,status)),task('stale-running','running',{stale:true}),task('historical-running','running',{historical:true})];
  const original=JSON.stringify(sessions),{context,listed}=ui(sessions);
  assert.equal(context.state.showHistory,false);
  assert.doesNotMatch(html.match(/<input id="show-history"[^>]*>/)[0],/\bchecked\b/);
  context.renderList();
  assert.deepEqual(listed(),[...active,'stale-running']);
  assert.equal(context.$('session-count').textContent,'7');
  context.$('show-history').checked=true;context.$('show-history').listeners.change();
  assert.equal(listed().length,sessions.length);
  assert.equal(context.$('session-count').textContent,String(sessions.length));
  context.$('show-history').checked=false;context.$('show-history').listeners.change();
  assert.deepEqual(listed(),[...active,'stale-running']);
  assert.equal(JSON.stringify(context.state.sessions),original,'list projection must not delete or alter archived tasks');
});

test('default selection skips ended tasks while explicit history links keep original detail identity',async()=>{
  const sessions=[task('history','completed',{thread_id:'old-thread'}),task('active','waiting_approval')];
  const {context,calls}=ui(sessions),target=context.selectionTarget;
  assert.equal(context.selectSessionId(sessions,null,target),'active');
  assert.equal(context.selectSessionId(sessions,null,{session:'history'}),'history');
  assert.equal(context.selectSessionId(sessions,'history',target),'history');
  context.state.selected='history';assert.equal(context.current().id,'history');
  context.renderList();assert.equal(context.sessionListCards.has('history'),false);
  await context.act(context.current(),'message',{text:'continue the original task'});
  assert.equal(calls[0].url,'/api/codex-sessions/history/message');
  assert.equal(calls[0].body.text,'continue the original task');
  assert.equal(context.state.sessions[0].thread_id,'old-thread');
});

test('all-ended list offers history and only selects a record after explicit opt-in',()=>{
  const sessions=[task('history','interrupted')],{context,listed}=ui(sessions);
  assert.equal(context.selectSessionId(sessions,null,context.selectionTarget),null);
  assert.equal(context.selectSessionId(sessions,null,{backend:'cuda',branch:'main'}),null);
  context.renderList();assert.deepEqual(listed(),[]);
  assert.equal(context.$('no-sessions').hidden,false);
  assert.equal(context.$('no-sessions-title').textContent,'暂无活跃任务');
  assert.match(context.$('no-sessions-note').textContent,/历史任务/);
  context.$('show-history').checked=true;context.$('show-history').listeners.change();
  assert.equal(context.state.selected,'history');assert.deepEqual(listed(),['history']);
  context.$('show-history').checked=false;context.$('show-history').listeners.change();
  assert.deepEqual(listed(),[]);assert.equal(context.current().id,'history','hiding a selected card keeps its resumable detail');
});

test('completed turns leave the active list without switching an open task detail',()=>{
  const running=task('open','running'),{context,listed}=ui([running]);context.state.selected='open';context.renderList();assert.deepEqual(listed(),['open']);
  running.status='completed';context.renderList();assert.deepEqual(listed(),[]);
  assert.equal(context.selectSessionId(context.state.sessions,context.state.selected,context.selectionTarget),'open');
  assert.equal(context.current().status,'completed');assert.equal(context.state.sessions.length,1);
});

for(const page of ['web/templates/overview.html','web/templates/index.html']) {
  test(`${page}: task visibility hides ended and historical tasks but keeps pending approvals and stale active tasks`,()=>{
    const text=script(page),context={};vm.createContext(context);
    const end=page==='web/templates/overview.html'?'  function renderTasks(':'  function renderObservedTasks(';
    vm.runInContext(between(text,'  function isActiveTask(',end)+'\nglobalThis.active=isActiveTask;',context);
    for(const status of ['prepared','starting','running','waiting_approval','waiting_input','interrupting'])assert.equal(context.active({status}),true,status);
    for(const status of ['completed','failed','interrupted','disconnected','unknown',undefined])assert.equal(context.active({status}),false,String(status));
    assert.equal(context.active({status:'running',stale:true}),true,'active tasks waiting for refresh remain visible');
    assert.equal(context.active({status:'running',historical:true}),false,'historical records cannot masquerade as active');
    assert.equal(context.active(null),false);
  });
}
