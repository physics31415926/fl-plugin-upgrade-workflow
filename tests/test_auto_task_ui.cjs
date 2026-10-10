const assert=require('node:assert/strict'),fs=require('node:fs'),path=require('node:path'),vm=require('node:vm'),test=require('node:test');
const html=fs.readFileSync(path.join(__dirname,'..','sessions.html'),'utf8'),source=[...html.matchAll(/<script\b[^>]*>([\s\S]*?)<\/script>/g)][0][1];
function between(start,end){const a=source.indexOf(start),b=source.indexOf(end,a);assert(a>=0&&b>a,`source exists: ${start}`);return source.slice(a,b);}
function ui(){
  const nodes=new Map(),calls=[],notices=[];
  const values={'agent-provider':'codex','profile':'gpu-demo','backend':'cuda','branch':'target-demo','ssh-alias':'gpu_demo','remote-workspace':'/work/plugin','connection-mode':'alias','model-paths':'','goal':'','ssh-host':'','ssh-user':'','ssh-port':'22'};
  const context={state:{readOnly:false,creating:false,checking:false,checkedKey:'',profiles:[{id:'gpu-demo',role:'compute',ssh_alias:'gpu_demo'}],profileKey:''},labels:{cuda:'NVIDIA'},API:'/api/codex-sessions',innerWidth:1440,
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
  context.$('custom-settings').checked=false;context.configureTaskMode();assert.equal(context.$('custom-fields').disabled,true);assert.equal(context.$('ssh-alias').required,false);assert.equal(context.$('branch').required,false);assert.equal(context.$('profile').required,true);
});
test('automatic preparation failures release the UI and preserve input without a fabricated session',async()=>{
  const {context,notices}=ui();context.$('goal').value='Keep this draft';context.post=async()=>{throw Error('设备准备暂不可用');};await context.create(true);assert.equal(context.state.creating,false);assert.equal(context.state.selected,undefined);assert.equal(context.$('goal').value,'Keep this draft');assert(notices.some(item=>item.error&&item.message==='设备准备暂不可用'));assert.equal(context.$('create-start').disabled,false);
});
test('advanced connection fields are collapsed and disabled in initial HTML',()=>{
  assert.match(html,/<details id="advanced-settings"[^>]*><summary>高级设置（可选）/);assert.doesNotMatch(html.match(/<details id="advanced-settings"[^>]*>/)[0],/\bopen\b/);assert.doesNotMatch(html.match(/<input id="custom-settings"[^>]*>/)[0],/\bchecked\b/);assert.match(html,/<fieldset id="custom-fields" disabled/);assert.match(html,/<select id="profile" required/);
  const custom=html.slice(html.indexOf('<fieldset id="custom-fields"'),html.indexOf('</fieldset>'));
  for(const id of ['backend','branch','remote-workspace','model-paths','ssh-host','ssh-user','ssh-port','save-profile','check-machine'])assert.match(custom,new RegExp('id="'+id+'"'));
  assert(!custom.includes('id="goal"'));assert(!custom.includes('id="agent-provider"'));
});
