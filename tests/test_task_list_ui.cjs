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
  constructor(tag){this.tag=tag;this.children=[];this.parent=null;this.listeners={};this.hidden=false;this.checked=false;this._text='';this.value='';}
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
    +between(source,'  // BEGIN task management controls','  // END task management controls'),context);
  const listed=()=>context.$('session-list').children.map(button=>[...context.sessionListCards].find(([,entry])=>entry.button===button)[0]);
  return {context,nodes,calls,listed};
}

test('sessions list defaults to active tasks and all view reveals retained records',()=>{
  const active=['prepared','starting','running','waiting_approval','waiting_input','interrupting'];
  const inactive=['completed','failed','interrupted','disconnected','unknown'];
  const sessions=[...inactive.map(status=>task(status,status)),...active.map(status=>task(status,status)),task('stale-running','running',{stale:true}),task('historical-running','running',{historical:true})];
  const original=JSON.stringify(sessions),{context,listed}=ui(sessions);
  assert.equal(context.state.showHistory,false);
  assert.equal(context.state.view,'active');
  context.renderList();
  assert.deepEqual(listed(),[...active,'stale-running']);
  assert.equal(context.$('session-count').textContent,'7');
  context.$('view-all').listeners.click();
  assert.equal(listed().length,sessions.length);
  assert.equal(context.$('session-count').textContent,String(sessions.length));
  context.$('view-active').listeners.click();
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
  context.$('view-all').listeners.click();
  assert.equal(context.state.selected,'history');assert.deepEqual(listed(),['history']);
  context.$('view-active').listeners.click();
  assert.deepEqual(listed(),[]);assert.equal(context.current(),undefined,'manual navigation to an empty view clears the panel');
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


test('manual filters select a matching task and leave source data intact',()=>{
  const sessions=[task('one','running',{provider:'claude',name:'升级尝试',branch:'dev/0.28'}),task('two','waiting_approval',{provider:'codex'}),task('three','failed',{provider:'flagscale'})];
  const {context,listed}=ui(sessions);context.state.selected='two';context.$('view-all').listeners.click();
  context.$('task-search').value='0.28';context.$('task-search').listeners.input();assert.deepEqual(listed(),['one']);assert.equal(context.current().id,'one');
  context.$('task-search').value='';context.$('task-search').listeners.input();context.$('task-status').value='waiting';context.$('task-status').listeners.change();assert.deepEqual(listed(),['two']);
  context.$('task-status').value='';context.$('task-status').listeners.change();context.$('task-agent').value='flagscale';context.$('task-agent').listeners.change();assert.deepEqual(listed(),['three']);assert.equal(context.current().id,'three');
  assert.equal(context.state.sessions.length,3);
});

test('card refresh includes task name and provider changes',()=>{
  const session=task('one','running',{name:'original',provider:'codex'}),{context}=ui([session]);context.renderList();
  const card=context.sessionListCards.get('one');assert.match(card.button.textContent,/original/);
  session.name='renamed';session.provider='claude';context.renderList();assert.match(card.button.textContent,/renamed/);assert.doesNotMatch(card.button.textContent,/original/);assert.match(card.contentKey,/claude/);
});

test('view changes keep selection inside the displayed list',()=>{
  const deleted=task('d'.repeat(32),'completed',{deleted_at:'2026-10-20T12:00:00Z'}),{context,listed}=ui([task('one','running')]);context.state.deletedSessions=[deleted];
  context.state.selected='one';context.$('view-trash').listeners.click();assert.deepEqual(listed(),[deleted.id]);assert.equal(context.current(),deleted);
  assert.equal(context.selectSessionId([...context.state.sessions,deleted],deleted.id,{}),deleted.id);context.$('view-all').listeners.click();assert.deepEqual(listed(),['one']);assert.equal(context.current().id,'one');
});

test('deletion requires confirmation and rechecks current status before writing',async()=>{
  const session=task('a'.repeat(32),'completed'),{context,calls}=ui([session]);context.state.selected=session.id;
  let opened=0;context.$('delete-dialog').showModal=()=>opened++;context.$('delete-dialog').close=()=>{};context.$('cancel-delete').focus=()=>{};
  context.openDeleteDialog();assert.equal(opened,1);assert.equal(calls.length,0);context.$('cancel-delete').listeners.click();assert.equal(context.state.deleteTarget,null);assert.equal(calls.length,0);
  context.openDeleteDialog();session.status='running';await context.manageTask('delete',session.id);assert.equal(calls.length,0,'task that resumed while confirmation was open cannot be deleted');
  for(const status of ['starting','running','waiting_approval','waiting_input','interrupting']){session.status=status;assert.equal(context.canDeleteTask(session),false,status);}
  session.status='prepared';assert.equal(context.canDeleteTask(session),true);context.state.readOnly=true;assert.equal(context.canDeleteTask(session),false);
});

test('delete and restore preserve task ID and never dispatch start',async()=>{
  const session=task('a'.repeat(32),'completed',{thread_id:'original-thread'}),{context,calls,listed}=ui([session]);context.state.selected=session.id;
  context.post=async(url,body)=>{calls.push({url,body});return {...session,deleted_at:url.endsWith('/delete')?'2026-10-20T12:00:00Z':undefined};};
  assert.equal(await context.manageTask('delete',session.id),true);assert.equal(context.state.sessions.length,0);assert.equal(context.current().thread_id,'original-thread');assert.deepEqual(listed(),[session.id]);assert.equal(context.state.view,'trash');
  await context.act(context.current(),'message',{text:'should not send'});assert.equal(calls.length,1);
  assert.equal(await context.manageTask('restore',session.id),true);assert.equal(context.state.deletedSessions.length,0);assert.equal(context.current().thread_id,'original-thread');assert.equal(context.state.view,'all');
  assert.deepEqual(calls.map(c=>c.url),['/api/codex-sessions/'+session.id+'/delete','/api/codex-sessions/'+session.id+'/restore']);
});

test('read-only and native tasks cannot invoke management; errors retain records',async()=>{
  const session=task('a'.repeat(32),'completed'),{context,calls}=ui([session,task('native-1','completed')]);
  context.state.readOnly=true;assert.equal(await context.manageTask('delete',session.id),false);context.state.readOnly=false;assert.equal(await context.manageTask('delete','native-1'),false);assert.equal(calls.length,0);
  context.post=async()=>{throw new Error('still running');};assert.equal(await context.manageTask('delete',session.id),false);assert.equal(context.state.sessions.length,2);assert.equal(context.state.deletedSessions.length,0);assert.equal(context.state.actionBusy,false);
});


test('an old poll cannot resurrect a deleted task and management waits for a fresh snapshot',async()=>{
  const session=task('a'.repeat(32),'completed'),deleted={...session,deleted_at:'2026-10-20T12:00:00Z'},{context}=ui([session]);context.state.selected=session.id;
  let finishOld,requests=0,renderedDeleted=false;const old=new Promise(resolve=>finishOld=resolve);
  context.request=()=>++requests===1?old:Promise.resolve({sessions:[],deleted_sessions:[deleted]});context.post=async()=>deleted;
  for(const name of ['renderSshSync','renderProviderSettings','renderFlagscaleSettings','showProfiles','renderHealth','checkButtons'])context[name]=()=>{};
  context.renderDetail=()=>{if(context.current()?.deleted_at)renderedDeleted=true;else if(renderedDeleted)assert.fail('old snapshot resurrected the deleted task');};
  vm.runInContext(between(source,'  async function refresh(', '  async function act('),context);
  const poll=context.refresh(true),management=context.manageTask('delete',session.id);await Promise.resolve();finishOld({sessions:[session],deleted_sessions:[]});
  await Promise.all([poll,management]);assert.equal(requests,2);assert.equal(context.state.sessions.length,0);assert.equal(context.current().deleted_at,deleted.deleted_at);assert.equal(context.state.refreshPromise,null);
});


function realDetail(context){
  for(const name of ['syncMessageDraft','renderProvenance','renderPolicy','renderTaskActions','renderPersistenceNotice','renderPending','renderEvents','renderEventHistory'])context[name]=()=>{};
  context.errorMessage=value=>value||'';context.claudeDenied=()=>false;
  vm.runInContext(between(source,'  function renderDetail(', '  async function refresh('),context);
}

test('the panel follows card selection after leaving trash and clears for empty filters',()=>{
  const deleted=task('d'.repeat(32),'completed',{deleted_at:'2026-10-20T12:00:00Z',name:'deleted'}),{context}=ui([task('one','running',{name:'first task'}),task('two','prepared',{name:'second task'})]);context.state.deletedSessions=[deleted];realDetail(context);
  context.$('view-trash').listeners.click();assert.equal(context.$('deleted-task-detail').hidden,false);assert.equal(context.$('session-detail').hidden,true);
  context.$('view-active').listeners.click();assert.equal(context.$('deleted-task-detail').hidden,true);assert.equal(context.$('detail-title').textContent,'first task');
  context.sessionListCards.get('two').button.listeners.click();assert.equal(context.$('detail-title').textContent,'second task');assert.equal(context.$('session-detail').hidden,false);assert.equal(context.$('deleted-task-detail').hidden,true);
  context.$('task-search').value='no-match';context.$('task-search').listeners.input();assert.equal(context.state.selected,null);assert.equal(context.$('session-detail').hidden,true);assert.equal(context.$('deleted-task-detail').hidden,true);assert.equal(context.$('no-selection').hidden,false);
});

test('polling cannot bring back a trash deep link after manual navigation',async()=>{
  const deleted=task('d'.repeat(32),'completed',{deleted_at:'2026-10-20T12:00:00Z'}),session=task('one','running'),{context}=ui([session]);context.state.deletedSessions=[deleted];context.selectionTarget.session=deleted.id;
  for(const name of ['renderSshSync','renderProviderSettings','renderFlagscaleSettings','showProfiles','renderHealth','checkButtons'])context[name]=()=>{};
  context.request=async()=>({sessions:[session],deleted_sessions:[deleted]});vm.runInContext(between(source,'  async function refresh(', '  async function act('),context);
  await context.refresh(true);assert.equal(context.state.view,'trash');assert.equal(context.current(),deleted);
  context.$('view-active').listeners.click();await context.refresh(true);assert.equal(context.state.view,'active');assert.equal(context.current(),session);
  context.$('task-search').value='nothing';context.$('task-search').listeners.input();await context.refresh(true);assert.equal(context.state.selected,null);assert.equal(context.current(),undefined);
});

for(const action of ['delete','restore'])test(`late ${action} response does not replace the user's newer task selection`,async()=>{
  const original=task('a'.repeat(32),'completed',action==='restore'?{deleted_at:'2026-10-20T12:00:00Z'}:{}),other=task('b'.repeat(32),'running'),{context}=ui(action==='delete'?[original,other]:[other]);
  if(action==='restore'){context.state.deletedSessions=[original];context.$('view-trash').listeners.click();}else context.$('view-all').listeners.click();
  context.state.selected=original.id;let finish;context.post=()=>new Promise(resolve=>finish=resolve);
  const operation=context.manageTask(action,original.id);context.$('view-active').listeners.click();context.sessionListCards.get(other.id).button.listeners.click();
  finish({...original,deleted_at:action==='delete'?'2026-10-20T12:01:00Z':undefined});await operation;
  assert.equal(context.state.view,'active');assert.equal(context.current().id,other.id);assert.equal(context.state.selected,other.id);
});
