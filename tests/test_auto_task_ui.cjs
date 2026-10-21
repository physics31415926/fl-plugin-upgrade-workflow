const assert=require('node:assert/strict'),fs=require('node:fs'),path=require('node:path'),vm=require('node:vm'),test=require('node:test');
const html=fs.readFileSync(path.join(__dirname,'..','web/templates/sessions.html'),'utf8'),source=[...html.matchAll(/<script\b[^>]*>([\s\S]*?)<\/script>/g)][0][1];
function between(start,end){const a=source.indexOf(start),b=source.indexOf(end,a);assert(a>=0&&b>a,`source exists: ${start}`);return source.slice(a,b);}
function ui(){
  const nodes=new Map(),calls=[],notices=[];
  const values={'agent-provider':'codex','profile':'gpu-demo','backend':'cuda','branch':'target-demo','branch-choice':'target-demo','ssh-alias':'gpu_demo','remote-workspace':'/work/plugin','connection-mode':'alias','model-paths':'','goal':'','ssh-host':'','ssh-user':'','ssh-port':'22'};
  const context={state:{selectionRevision:0,readOnly:false,creating:false,checking:false,checkedKey:'',profiles:[{id:'gpu-demo',role:'compute',ssh_alias:'gpu_demo'}],profileKey:''},labels:{cuda:'NVIDIA'},API:'/api/codex-sessions',innerWidth:1440,
    $:id=>{if(!nodes.has(id))nodes.set(id,{value:values[id]||'',textContent:'',checked:false,hidden:false,disabled:false,required:false,querySelectorAll:()=>[...nodes].filter(([id])=>Object.hasOwn(values,id)||['custom-settings','auto-monitor'].includes(id)).map(([,node])=>node),reportValidity:()=>true});return nodes.get(id);},
    taskPolicy:()=>({mode:'adapt'}),renderPolicy:()=>{},isCompute:profile=>(profile?.role||'compute')==='compute',providerAvailable:()=>true,renderSshSync:()=>{},showProfiles:()=>{},
    notice:(id,message,error)=>notices.push({id,message,error:!!error}),refresh:async()=>{},monitorUrl:(branch,id)=>'/monitor?branch='+branch+'&session='+id,
    window:{location:{assign:()=>{throw Error('unexpected navigation');}}},post:async(url,body)=>{calls.push({url,body,creating:context.state.creating,progress:context.$('create-start').textContent});return {session:{id:'task-demo',status:'prepared'}};},
    checkMachine:async()=>{throw Error('automatic mode must not preflight manually');},saveConnection:async()=>{throw Error('automatic mode must not save a profile');}};
  vm.createContext(context);vm.runInContext(between('  function fields(', '  async function saveConnection(')+between('  async function create(',"  $('custom-settings').addEventListener("),context);return{context,nodes,calls,notices};
}
test('automatic fields use device and agent without manual connection, backend or models',()=>{
  const {context}=ui();context.$('model-paths').value='invalid manual JSON';context.$('remote-workspace').value='';context.$('backend').value='';context.$('goal').value='  Optional instructions  ';
  const body=context.fields();assert.deepEqual(JSON.parse(JSON.stringify(body)),{provider:'codex',profile_id:'gpu-demo',automatic:true,branch:'target-demo',goal:'Optional instructions'});
  context.$('goal').value='  ';assert.equal(Object.hasOwn(context.fields(),'goal'),false);
});
test('automatic create prepares with one POST and no explicit preflight or profile save',async()=>{
  for(const start of [true,false]){const {context,calls,notices}=ui();context.$('remote-workspace').value='';context.$('model-paths').value='not JSON';await context.create(start);
    assert.equal(calls.length,1);assert.equal(calls[0].url,'/api/codex-sessions');assert.deepEqual(JSON.parse(JSON.stringify(calls[0].body)),{provider:'codex',profile_id:'gpu-demo',automatic:true,branch:'target-demo',start});
    assert.equal(calls[0].creating,true);assert.match(calls[0].progress,/正在准备设备与工作目录/);assert.equal(context.state.selected,'task-demo');assert.equal(context.state.creating,false);assert(notices.some(item=>/正在准备所选设备/.test(item.message)));}
});
test('missing, development and gateway devices cannot start an automatic task',async()=>{
  for(const role of ['missing','development','gateway']){const {context,calls,notices}=ui();if(role==='missing')context.$('profile').value='';else context.state.profiles[0].role=role;context.checkButtons();assert.equal(context.$('create-start').disabled,true);assert.equal(context.$('create-only').disabled,true);await context.create(true);assert.equal(calls.length,0);assert(notices.some(item=>item.error));}
});
test('automatic create requires ready agent and honors read-only or existing-PR monitor policy',async()=>{
  for(const variant of ['unavailable','readonly','monitor']){const {context,calls}=ui();if(variant==='unavailable')context.providerAvailable=()=>false;if(variant==='readonly')context.state.readOnly=true;if(variant==='monitor')context.taskPolicy=()=>({mode:'monitor'});context.checkButtons();assert.equal(context.$('create-start').disabled,true);await context.create(true);assert.equal(calls.length,0);}
});
test('only custom-mode checkbox enables legacy fields and restores manual preflight',async()=>{
  const {context,calls}=ui();context.$('advanced-settings').open=true;context.checkButtons();assert.equal(context.$('custom-fields').disabled,true);assert.equal(context.fields().automatic,true);
  context.$('custom-settings').checked=true;context.configureTaskMode();assert.equal(context.$('custom-fields').disabled,false);assert.equal(context.$('profile').required,false);assert.equal(context.$('branch').required,true);assert.equal(context.$('remote-workspace').required,true);assert.equal(context.$('ssh-alias').required,true);
  let checks=0;context.checkMachine=async()=>{checks++;return true;};await context.create(true);assert.equal(checks,1);assert.equal(calls.length,1);assert.equal(calls[0].body.require_preflight,true);assert.equal(calls[0].body.backend,'cuda');assert.equal(calls[0].body.remote_workspace,'/work/plugin');assert.equal(Object.hasOwn(calls[0].body,'automatic'),false);
  context.$('custom-settings').checked=false;context.configureTaskMode();assert.equal(context.$('custom-fields').disabled,true);assert.equal(context.$('ssh-alias').required,false);assert.equal(context.$('branch').required,true);assert.equal(context.$('profile').required,true);
});
test('automatic preparation failures release the UI and preserve input without a fabricated session',async()=>{
  const {context,notices}=ui();context.$('goal').value='Keep this draft';context.post=async()=>{throw Error('设备准备暂不可用');};await context.create(true);assert.equal(context.state.creating,false);assert.equal(context.state.selected,undefined);assert.equal(context.$('goal').value,'Keep this draft');assert(notices.some(item=>item.error&&item.message==='设备准备暂不可用'));assert.equal(context.$('create-start').disabled,false);
});
test('advanced connection fields are collapsed and disabled in initial HTML',()=>{
  assert.match(html,/<details id="advanced-settings"[^>]*><summary>高级设置（可选）/);assert.doesNotMatch(html.match(/<details id="advanced-settings"[^>]*>/)[0],/\bopen\b/);assert.doesNotMatch(html.match(/<input id="custom-settings"[^>]*>/)[0],/\bchecked\b/);assert.match(html,/<fieldset id="custom-fields" disabled/);assert.match(html,/<select id="profile" required/);
  const custom=html.slice(html.indexOf('<fieldset id="custom-fields"'),html.indexOf('</fieldset>'));
  for(const id of ['backend','remote-workspace','model-paths','ssh-host','ssh-user','ssh-port','save-profile','check-machine'])assert.match(custom,new RegExp('id="'+id+'"'));
  assert(!custom.includes('id="branch"'));assert(!custom.includes('id="goal"'));assert(!custom.includes('id="agent-provider"'));
});


test('automatic mode keeps baseline editable and submits the chosen branch without manual configuration',async()=>{
  const {context,calls}=ui();context.configureTaskMode();
  assert.equal(context.$('custom-fields').disabled,true);assert.equal(context.$('branch').disabled,false);assert.equal(context.$('branch').required,true);
  context.$('branch').value='dev/next';context.invalidateCheck();await context.create(false);
  assert.equal(calls[0].body.branch,'dev/next');assert.equal(calls[0].body.automatic,true);assert.equal(context.$('custom-settings').checked,false);
});

function branchUi(){
  const result=ui(),{context}=result;context.el=(tag,cls,text)=>({tag,textContent:text});
  const list=context.$('branch-choice');list.replaceChildren=(...items)=>{list.children=items;};
  context.$('branch').focus=()=>{context.$('branch').focused=true;};
  vm.runInContext(between('  function renderBranchOptions(', '  function fields('),context);
  context.renderBranchOptions();return {...result,list};
}

test('baseline uses a native dropdown with an explicit custom branch entry',()=>{
  assert.match(html,/<select id="branch-choice" required/);assert.doesNotMatch(html,/<datalist id="branch-options"/);
  const {context,list}=branchUi();assert.equal(context.$('custom-branch-field').hidden,true);assert.equal(context.$('branch').type,'hidden');
  assert.deepEqual(Array.from(list.children,item=>item.value),['','target-demo','__custom__']);
  context.$('branch-choice').value='__custom__';context.branchChoiceChanged();
  assert.equal(context.$('custom-branch-field').hidden,false);assert.equal(context.$('branch').type,'text');assert.equal(context.$('branch').focused,true);
});

test('choosing a baseline submits that exact branch in automatic and manual task modes',async()=>{
  for(const manual of [false,true]){
    const {context,calls}=branchUi();context.state.branchNames=['target-demo','dev/next'];context.renderBranchOptions();
    context.$('custom-settings').checked=manual;context.state.checkedKey='old-check';context.$('branch-choice').value='dev/next';context.branchChoiceChanged();
    assert.equal(context.$('branch').value,'dev/next');assert.equal(context.state.branchEdited,true);assert.equal(context.state.checkedKey,'');
    if(manual)context.checkMachine=async()=>true;
    await context.create(false);assert.equal(calls[0].body.branch,'dev/next');assert.equal(context.$('custom-branch-field').hidden,true);
  }
});

test('lightweight branch loading preserves a selection made while the request is pending',async()=>{
  const {context,list}=branchUi();let resolve;
  context.request=path=>{assert.equal(path,'/api/branches');return new Promise(done=>{resolve=done;});};
  const pending=context.loadBranchOptions();assert.equal(context.$('branch-choice').disabled,false);assert.equal(context.$('reload-branches').disabled,true);
  context.$('branch-choice').value='dev/next';context.branchChoiceChanged();
  resolve({branch:'target-demo',branches:['main','dev/next','main',null]});await pending;
  assert.equal(context.$('branch').value,'dev/next');assert.equal(context.$('branch-choice').value,'dev/next');
  assert.deepEqual(Array.from(list.children,item=>item.value),['','dev/next','main','__custom__']);
});

test('branch reload and failure preserve custom input and keep other branches available',async()=>{
  const {context,list}=branchUi();let resolve;context.request=()=>new Promise(done=>{resolve=done;});
  const pending=context.loadBranchOptions();context.$('branch-choice').value='__custom__';context.branchChoiceChanged();context.$('branch').value='user/next';
  resolve({branches:['main','dev/next']});await pending;
  assert.equal(context.$('branch').value,'user/next');assert.equal(context.$('branch-choice').value,'__custom__');assert.equal(context.$('custom-branch-field').hidden,false);
  assert.deepEqual(Array.from(list.children,item=>item.value),['','main','dev/next','__custom__']);
  context.request=async()=>{throw Error('offline');};await context.loadBranchOptions();
  assert.equal(context.$('branch').value,'user/next');assert.equal(context.$('branch-choice').value,'__custom__');assert.equal(context.$('branch').disabled,false);
  assert.equal(context.$('reload-branches').disabled,false);assert.match(context.$('branch-hint').textContent,/可重试或选择/);
  context.$('branch-choice').value='main';context.branchChoiceChanged();assert.equal(context.fields().branch,'main');assert.equal(context.$('custom-branch-field').hidden,true);
});

test('branch loading never overrides a URL branch and does not run in read-only mode',async()=>{
  const {context,list}=branchUi();context.$('branch').value='url/branch';context.request=async()=>({branch:'default',branches:['main']});
  await context.loadBranchOptions();assert.equal(context.$('branch').value,'url/branch');assert.equal(context.$('branch-choice').value,'url/branch');
  assert.deepEqual(Array.from(list.children,item=>item.value),['','url/branch','main','__custom__']);
  context.state.readOnly=true;context.request=async()=>assert.fail('read-only pages must not request local branch choices');await context.loadBranchOptions();
});



test('creating a task replaces initial deep-link selection before refreshing',async()=>{
  const {context}=ui();context.state.selected='deleted-deep-link';context.state.view='trash';let refreshed=false;
  context.refresh=async()=>{refreshed=true;assert.equal(context.state.selectionRevision,1);assert.equal(context.state.view,'active');assert.equal(context.state.selected,'task-demo');};
  await context.create(false);assert.equal(refreshed,true);assert.equal(context.state.selected,'task-demo');
});
