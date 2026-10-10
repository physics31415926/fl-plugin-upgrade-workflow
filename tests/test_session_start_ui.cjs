const assert=require('node:assert/strict'),fs=require('node:fs'),path=require('node:path'),vm=require('node:vm'),test=require('node:test');
const html=fs.readFileSync(path.join(__dirname,'..','sessions.html'),'utf8'),source=[...html.matchAll(/<script\b[^>]*>([\s\S]*?)<\/script>/g)][0][1];
function between(start,end){const a=source.indexOf(start),b=source.indexOf(end,a);assert(a>=0&&b>a,`source exists: ${start}`);return source.slice(a,b);}
function ui(info={available:true,authenticated:true,error:null}){
  const nodes=new Map(),calls=[],notices=[],session={id:'original-task',provider:'codex',status:'failed',thread_id:null,backend:'enflame',branch:'target-demo',worktree:'/local/original-worktree'};
  const context={state:{available:true,providers:{codex:info},readOnly:false,actionBusy:false,sessions:[session],selected:session.id},API:'/api/codex-sessions',encodeURIComponent,
    $:id=>{if(!nodes.has(id))nodes.set(id,{value:id==='agent-provider'?'codex':'',disabled:false,textContent:'',hidden:false});return nodes.get(id);},
    notice:(id,message,error)=>notices.push({id,message,error:!!error}),taskPolicy:()=>({mode:'adapt'}),errorMessage:value=>typeof value==='string'?value:'',checkButtons:()=>{},refresh:async()=>{},
    post:async(url,body)=>{calls.push({url,body});return {session};},request:async()=>({providers:{codex:{available:true,authenticated:true,error:null}},available:true})};
  vm.createContext(context);
  vm.runInContext(between('  function providerName(', '  function renderProviderSettings(')
    +between('  function current(', '  function taskPolicy(')
    +between('  function renderTaskActions(', '  function renderDetail(')
    +between('  async function act(', '  async function create(')
    +between('  function renderHealth(', "  $('older-events').addEventListener(")
    +between('  async function checkAgentHealth(', "  $('health-check').addEventListener("),context);
  context.renderDetail=()=>context.renderTaskActions(session,false,false);
  return {context,nodes,calls,notices,session};
}
test('Codex launch readiness rejects explicit failed authentication and health errors',()=>{
  for(const info of [{available:true,authenticated:false},{available:true,authenticated:true,error:'Cannot initialize the CLI'},{available:false,authenticated:true}]){
    const {context}=ui(info);assert.equal(context.providerAvailable('codex'),false);context.renderDetail();assert.equal(context.$('start-session').disabled,true);
  }
  for(const info of [{available:true},{available:true,authenticated:null,error:null},{available:true,authenticated:true,error:null}])assert.equal(ui(info).context.providerAvailable('codex'),true,'unknown authentication remains compatible with unprobed snapshots');
});
test('legacy top-level health replaces prior ready metadata instead of ignoring authentication failure',()=>{
  const {context}=ui();context.renderHealth({available:true,authenticated:false,error:'Please sign in locally'});context.renderDetail();
  assert.equal(context.providerAvailable('codex'),false);assert.equal(context.$('health-status').textContent,'Codex 尚未登录');assert.equal(context.$('start-session').disabled,true);
});
test('failed initial launch offers retry on its original identity and retains the working directory',async()=>{
  const {context,calls,session}=ui();context.renderDetail();assert.equal(context.$('start-session').textContent,'重试启动原任务');assert.equal(context.$('start-session').disabled,false);assert.equal(context.$('send-message').disabled,true);assert.match(context.$('message-hint').textContent,/无需重新创建/);
  const original=JSON.stringify(session);assert.equal(await context.act(session,'start'),true);assert.equal(calls.length,1);assert.equal(calls[0].url,'/api/codex-sessions/original-task/start');assert.equal(JSON.stringify(session),original);
});
test('unavailable agents and read-only observers cannot dispatch a retry or followup',async()=>{
  for(const readonly of [false,true]){
    const {context,calls,notices,session}=ui({available:true,authenticated:false});context.state.readOnly=readonly;context.renderDetail();assert.equal(context.$('start-session').disabled,true);
    await context.act(session,'start');await context.act(session,'message',{text:'continue'});assert.equal(calls.length,0);if(!readonly)assert(notices.some(item=>item.error));
  }
});
test('health recovery immediately restores the selected original task retry without creating another task',async()=>{
  const {context,calls,session}=ui({available:false,authenticated:null,error:'CLI was replaced'});context.renderDetail();assert.equal(context.$('start-session').disabled,true);assert.match(context.$('message-hint').textContent,/连接检查/);
  await context.checkAgentHealth();assert.equal(context.$('start-session').disabled,false);assert.equal(context.$('start-session').textContent,'重试启动原任务');assert.equal(context.state.selected,session.id);assert.equal(context.state.sessions.length,1);assert.equal(calls.length,0);assert.equal(context.$('health-status').textContent,'Codex 已登录，可启动任务');
});
test('failed health requests disable stale ready controls until a successful retry',async()=>{
  const {context,calls}=ui();context.renderDetail();assert.equal(context.$('start-session').disabled,false);context.request=async()=>{throw Error('Service temporarily unreachable');};await context.checkAgentHealth();assert.equal(context.$('start-session').disabled,true);assert.equal(context.$('health-status').textContent,'Codex 连接检查失败');assert.equal(context.$('health-check').disabled,false);assert.equal(calls.length,0);
});
test('failed tasks remain outside the active list while an explicit selection retains retry identity',()=>{
  const {context,session}=ui();context.state.showHistory=false;context.prioritizedSessions=rows=>rows;context.renderDetail();assert.equal(context.activeTask(session),false);assert.equal(context.current(),session);assert.equal(context.$('start-session').textContent,'重试启动原任务');
  session.thread_id='established-thread';context.renderDetail();assert.equal(context.$('start-session').textContent,'继续任务');assert.equal(context.$('send-message').disabled,false);
});
