const assert=require('node:assert/strict'),fs=require('node:fs'),path=require('node:path'),vm=require('node:vm'),test=require('node:test');
const html=fs.readFileSync(path.join(__dirname,'..','web/templates/sessions.html'),'utf8'),source=[...html.matchAll(/<script\b[^>]*>([\s\S]*?)<\/script>/g)][0][1];
const thread='00000000-1111-2222-3333-444444444444';
function between(start,end){const a=source.indexOf(start),b=source.indexOf(end,a);assert(a>=0&&b>a,`source exists: ${start}`);return source.slice(a,b);}
class Element{
  constructor(tag){this.tag=tag;this.children=[];this.listeners={};this.textContent='';this.hidden=false;this.disabled=false;this.value='';}
  append(...nodes){this.children.push(...nodes);}
  replaceChildren(...nodes){this.children=[...nodes];}
  setAttribute(name,value){this[name]=value;}
  addEventListener(name,handler){this.listeners[name]=handler;}
}
function ui(extras={}){
  const nodes=new Map(),calls=[],notices=[],session={id:'original-task',provider:'codex',status:'waiting_approval',thread_id:thread,backend:'enflame',branch:'target-demo',worktree:'/local/original-worktree',...extras};
  const context={state:{available:true,providers:{codex:{available:true,authenticated:true}},readOnly:false,actionBusy:false,sessions:[session],selected:session.id,pendingKey:''},API:'/api/codex-sessions',encodeURIComponent,
    $:id=>{if(!nodes.has(id))nodes.set(id,new Element('div'));return nodes.get(id);},el:(tag,cls,text)=>{const node=new Element(tag);node.className=cls||'';node.textContent=text||'';return node;},
    notice:(id,message,error)=>notices.push({id,message,error:!!error}),taskPolicy:()=>({mode:'adapt'}),checkButtons:()=>{},refresh:async()=>{},errorMessage:value=>typeof value==='string'?value:'',
    approvalDecisions:()=>['accept','decline'],decisionLabel:value=>value,
    post:async(url,body)=>{calls.push({url,body});return {session:{...session,execution_owner:'codex_desktop'},desktop_url:'codex://threads/'+thread};}};
  vm.createContext(context);vm.runInContext(between('  function providerName(', '  function renderProviderSettings(')
    +between('  function current(', '  function taskPolicy(')
    +between('  async function respond(', '  function renderPersistenceNotice(')
    +between('  function renderTaskActions(', '  function renderDetail(')
    +between('  async function act(', '  async function create('),context);
  context.renderDetail=()=>{const current=context.current();context.renderTaskActions(current,false,false);context.renderPending(current);};
  return {context,nodes,calls,notices,session};
}
test('desktop continuation is restricted to local Codex tasks with a valid thread UUID',async()=>{
  for(const extras of [{thread_id:undefined},{thread_id:'codex://threads/'+thread},{thread_id:'../another-thread'},{thread_id:'not-a-uuid'},{provider:'claude'},{provider:'flagscale'}]){
    const {context,calls,session}=ui(extras);context.renderDetail();assert.equal(context.$('desktop-session').hidden,true);await context.act(session,'desktop');assert.equal(calls.length,0);
  }
  const {context}=ui({thread_id:thread.toUpperCase()});context.renderDetail();assert.equal(context.$('desktop-session').hidden,false);assert.equal(context.$('desktop-session').textContent,'在 Codex 中继续');
});
test('unstarted and failed originals explain when desktop continuation becomes available',()=>{
  for(const status of ['prepared','failed']){const {context}=ui({status,thread_id:null});context.renderDetail();assert.equal(context.$('desktop-session').hidden,true);assert.match(context.$('desktop-note').textContent,/启动/);assert.doesNotMatch(context.$('desktop-note').textContent,/新建|重新创建/);if(status==='failed')assert.match(context.$('desktop-note').textContent,/原任务/);}
});
test('running desktop transfer warns that it pauses this turn and preserves remote tmux',()=>{
  const {context}=ui();context.renderDetail();assert.match(context.$('desktop-note').textContent,/暂停当前轮次/);assert.match(context.$('desktop-note').textContent,/tmux 不会停止/);
});
test('desktop transfer posts once to the original task and keeps the same selected identity and worktree',async()=>{
  const {context,calls,session}=ui();assert.equal(await context.act(session,'desktop'),true);assert.equal(calls.length,1);assert.equal(calls[0].url,'/api/codex-sessions/original-task/desktop');assert.deepEqual(JSON.parse(JSON.stringify(calls[0].body)),{});assert.equal(context.state.selected,session.id);assert.equal(context.state.sessions.length,1);assert.equal(context.current().worktree,session.worktree);assert.equal(context.current().execution_owner,'codex_desktop');assert.equal(context.state.actionBusy,false);
});
test('a busy transfer blocks duplicate requests and releases the button when it finishes',async()=>{
  const {context,calls,session}=ui();let release;context.post=async(url,body)=>{calls.push({url,body});return await new Promise(resolve=>{release=resolve;});};
  const first=context.act(session,'desktop');assert.equal(context.state.actionBusy,true);assert.equal(context.$('desktop-session').disabled,true);await context.act(session,'desktop');assert.equal(calls.length,1);release({session:{...session,execution_owner:'codex_desktop'},desktop_url:'codex://threads/'+thread});await first;assert.equal(context.$('desktop-session').disabled,false);
});
test('a mismatched response cannot replace the original task or cause browser navigation',async()=>{
  const {context,session,notices}=ui();context.post=async()=>({session:{id:'different-task'},desktop_url:'https://untrusted.example.test/'});const original=JSON.stringify(session);assert.equal(await context.act(session,'desktop'),false);assert.equal(JSON.stringify(context.current()),original);assert.equal(context.state.selected,session.id);assert(notices.some(item=>item.error&&/不一致/.test(item.message)));assert.equal(context.state.actionBusy,false);
});
test('client launch failure preserves desktop ownership and shows the returned error with a safe reopen path',async()=>{
  const {context,session,calls,notices}=ui();context.post=async(url,body)=>{calls.push({url,body});return {session:{...session,execution_owner:'codex_desktop'},desktop_url:'codex://threads/'+thread,desktop_opened:false,desktop_open_error:'The registered application could not be opened'};};
  assert.equal(await context.act(session,'desktop'),true);assert.equal(context.current().execution_owner,'codex_desktop');assert.equal(context.state.selected,session.id);assert.equal(context.state.actionBusy,false);assert.equal(context.$('start-session').disabled,true);assert.equal(context.$('send-message').disabled,true);assert.equal(context.$('desktop-session').disabled,false);
  const last=notices.at(-1);assert.equal(last.error,true);assert.match(last.message,/The registered application could not be opened/);assert.match(last.message,/重试打开/);assert.doesNotMatch(last.message,/已在 Codex 客户端打开/);
  await context.act(context.current(),'start');assert.equal(calls.length,1,'failed app launch cannot reclaim dashboard execution');
  context.post=async(url,body)=>{calls.push({url,body});return {session:context.current(),desktop_url:'codex://threads/'+thread,desktop_opened:true,desktop_open_error:null};};
  assert.equal(await context.act(context.current(),'desktop'),true);assert.equal(calls.length,2);assert.equal(notices.at(-1).error,false);assert.match(notices.at(-1).message,/已在 Codex 客户端打开/);
});
test('desktop owned tasks retain detail and desktop reopening while all dashboard controls and approvals are blocked',async()=>{
  const pending={id:'approval-demo',method:'item/commandExecution/requestApproval',params:{command:'echo fixture'}};
  const {context,calls,session}=ui({execution_owner:'codex_desktop',pending_requests:[pending]});context.renderDetail();assert.equal(context.current(),session);assert.equal(context.$('desktop-session').hidden,false);assert.equal(context.$('desktop-session').disabled,false);
  for(const id of ['start-session','interrupt-session','send-message','message'])assert.equal(context.$(id).disabled,true,id);assert.equal(context.$('message-form').hidden,true);assert.equal(context.$('pending-requests').children[0].children.some(child=>child.tag==='div'),false);assert.equal(context.$('pending-requests').children[0].children.some(child=>child.tag==='button'),false);
  for(const action of ['start','message','interrupt','respond'])await context.act(session,action);assert.equal(calls.length,0);assert.equal(await context.act(session,'desktop'),true);assert.equal(calls.length,1);
});
test('ownership changes invalidate cached approval controls while leaving native status untouched',()=>{
  const {context,session}=ui({pending_requests:[{id:'approval-demo',method:'item/permissions/requestApproval',params:{permissions:{}}}]});context.renderDetail();assert(context.$('pending-requests').children[0].children.some(child=>child.tag==='div'));session.execution_owner='codex_desktop';session.status='waiting_input';context.renderDetail();assert.equal(context.$('pending-requests').children[0].children.some(child=>child.tag==='div'),false);assert.equal(session.status,'waiting_input');
});
test('LAN read-only pages expose no desktop transfer or task controls and never issue mutations',async()=>{
  const {context,calls,session}=ui({pending_requests:[{id:'approval-demo',method:'item/permissions/requestApproval',params:{permissions:{}}}]});context.state.readOnly=true;context.renderDetail();assert.equal(context.$('desktop-session').hidden,true);assert.equal(context.$('desktop-note').hidden,true);for(const id of ['start-session','interrupt-session','message-form'])assert.equal(context.$(id).hidden,true,id);for(const action of ['desktop','start','message','interrupt','respond'])await context.act(session,action);assert.equal(calls.length,0);
});
test('desktop button delegates to the server action without directly launching a custom URI in the browser',()=>{
  assert.match(source,/\$\('desktop-session'\)\.addEventListener\('click',\(\)=>act\(current\(\),'desktop'\)\)/);assert.doesNotMatch(source,/codex:\/\/threads\//);assert.match(html,/<button[^>]*id="desktop-session"[^>]*hidden/);
});
