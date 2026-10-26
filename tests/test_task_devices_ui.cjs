const assert=require('node:assert/strict'),fs=require('node:fs'),path=require('node:path'),vm=require('node:vm'),test=require('node:test');
const html=fs.readFileSync(path.join(__dirname,'..','web/templates/sessions.html'),'utf8'),source=[...html.matchAll(/<script\b[^>]*>([\s\S]*?)<\/script>/g)][0][1];
function between(start,end){const a=source.indexOf(start),b=source.indexOf(end,a);assert(a>=0&&b>a,`source exists: ${start}`);return source.slice(a,b);}
class Element{
  constructor(tag='div'){this.tag=tag;this.children=[];this._text='';this._value='';this.checked=false;this.disabled=false;this.hidden=false;this.listeners={};this.rebuilds=0;}
  get textContent(){return this._text+this.children.map(child=>child.textContent).join('');}
  set textContent(value){this._text=String(value);this.children=[];}
  get options(){return this.children.flatMap(child=>child.tag==='optgroup'?child.children:child.tag==='option'?[child]:[]);}
  get value(){return this._value;}
  set value(value){const text=String(value);this._value=this.tag==='select'&&this.children.length&&!this.options.some(option=>option.value===text)?'':text;}
  get firstChild(){return this.children[0];}
  append(...nodes){for(const node of nodes)this.children.push(...(node.tag==='#fragment'?node.children:[node]));}
  replaceChildren(...nodes){this.children=[];this._text='';this.rebuilds++;this.append(...nodes);}
  addEventListener(event,handler){(this.listeners[event]??=[]).push(handler);}
  dispatch(event){for(const handler of this.listeners[event]||[])handler({target:this});}
  reportValidity(){return true;}
  querySelectorAll(){return [];}
}
function profile(id,backend='cuda',extra={}){return{id,label:id,ssh_alias:id,role:'compute',backend,remote_workspace:'/work/plugin',...extra};}
function device(id,backend='cuda',extra={}){return profile(id,backend,{task_supported:true,device_observation:{backend,verified:true,stale:false,updated_at:'2026-01-01T12:00:00Z'},...extra});}
function ui(){
  const nodes=new Map(),notices=[];
  const context={state:{profiles:[],taskDevices:null,profileKey:'',profileRenderPending:false,checkedKey:'',readOnly:false,creating:false,checking:false,profileRevision:0,sessionRevision:0,selectionRevision:0,providers:{}},labels:{cuda:'NVIDIA / 英伟达',ascend:'Ascend / 昇腾',musa:'Moore Threads / 摩尔线程'},orderedBackends:['cuda','ascend','musa'],
    document:{activeElement:null,createDocumentFragment:()=>new Element('#fragment')},
    $:id=>{if(!nodes.has(id))nodes.set(id,new Element(id==='profile'||id==='backend'?'select':'div'));return nodes.get(id);},
    el:(tag,cls,text)=>{const node=new Element(tag);node.className=cls;if(text!==undefined)node.textContent=text;return node;},
    projectUrl:url=>url+'?project=fixture',formatTime:value=>value,errorMessage:value=>typeof value==='string'?value:value?.message||'',
    taskPolicy:()=>({mode:'adapt'}),renderPolicy:()=>{},providerAvailable:()=>true,renderSshSync:()=>{},notice:(id,message,error)=>notices.push({id,message,error}),
    requestedProfile:'',profileLinkHandled:false,API:'/api/codex-sessions',selectionTarget:{},prioritizedSessions:rows=>rows,selectSessionId:()=>null,
    renderProviderSettings:()=>{},renderFlagscaleSettings:()=>{},renderHealth:()=>{},renderList:()=>{},renderDetail:()=>{},
  };
  vm.createContext(context);
  vm.runInContext(between('  function fields(', '  async function saveConnection(')+between('  function sshConfigText(', '  function renderSshSync(')+between('  function showProfiles(', '  function renderList(')+between('  function applyProfileSelection(',"  $('branch').addEventListener(")+between('  async function refresh(', '  async function act('),context);
  for(const value of ['',...Object.keys(context.labels)]){const option=new Element('option');option.value=value;context.$('backend').append(option);}
  context.$('branch').value='chosen/branch';context.$('agent-provider').value='codex';context.$('connection-mode').value='alias';context.$('ssh-port').value='22';
  return{context,nodes,notices};
}
function select(context,id){context.$('profile').value=id;context.$('profile').dispatch('change');}
function ids(context){return context.$('profile').options.map(option=>option.value).filter(Boolean);}

test('automatic picker uses only collected task devices and explicit empty results never reveal archived profiles',()=>{
  const {context}=ui();context.state.profiles=[profile('archived'),profile('observed','ascend')];context.state.taskDevices=[device('observed','musa')];context.showProfiles();
  assert.deepEqual(ids(context),['observed']);select(context,'observed');assert.equal(context.fields().profile_id,'observed');assert.equal(context.$('backend').value,'musa');assert.match(context.$('profile-backend').textContent,/Moore Threads/);
  context.state.taskDevices=[];context.showProfiles();context.checkButtons();assert.deepEqual(ids(context),[]);assert.equal(context.$('profile').value,'');assert.equal(context.$('create-start').disabled,true);assert.match(context.$('machine-inventory').textContent,/设备资源页刷新采集/);assert.equal(context.$('profile-resources-link').href,'/devices?project=fixture');
  context.$('profile')._value='archived';assert.throws(()=>context.fields(),/请选择验证设备/);
});

test('old servers fall back to saved profiles while custom mode deliberately exposes the full inventory',()=>{
  const {context}=ui();context.state.profiles=[profile('archived'),profile('observed')];context.showProfiles();assert.deepEqual(ids(context),['archived','observed']);
  context.state.taskDevices=[device('observed')];context.showProfiles();assert.deepEqual(ids(context),['observed']);
  context.$('custom-settings').checked=true;context.configureTaskMode();assert.deepEqual(ids(context),['archived','observed']);assert.equal(context.$('custom-fields').hidden,false);assert.equal(context.$('custom-fields').disabled,false);
  select(context,'archived');context.$('custom-settings').checked=false;context.configureTaskMode();assert.deepEqual(ids(context),['observed']);assert.equal(context.$('profile').value,'');assert.equal(context.$('custom-fields').hidden,true);assert.equal(context.$('create-start').disabled,true);
});

test('polling a newly identified backend for the same ID updates automatic matching without changing other drafts',async()=>{
  const {context}=ui();context.state.profiles=[profile('machine','ascend')];context.state.taskDevices=[device('machine','',{device_observation:{verified:false,stale:false}})];context.showProfiles();select(context,'machine');
  assert.equal(context.$('backend').value,'');context.$('goal').value='draft instruction';context.$('ssh-host').value='draft.example.test';
  context.request=async()=>({sessions:[],machine_profiles:[profile('machine','ascend')],task_devices:[device('machine','musa')]});await context.refresh(true);
  assert.equal(context.$('profile').value,'machine');assert.equal(context.$('backend').value,'musa');assert.match(context.$('profile-backend').textContent,/Moore Threads/);assert.match(context.$('profile-observation').textContent,/2026-01-01T12:00:00Z.*已核验实际硬件/);
  assert.equal(context.$('goal').value,'draft instruction');assert.equal(context.$('ssh-host').value,'draft.example.test');assert.equal(context.$('branch').value,'chosen/branch');
});

test('manual backend and connection drafts survive polling even when the stored backend changes',async()=>{
  const {context}=ui();context.state.profiles=[profile('machine')];context.state.taskDevices=[device('machine')];context.$('custom-settings').checked=true;context.showProfiles();select(context,'machine');
  context.$('backend').value='ascend';context.$('ssh-host').value='draft.example.test';context.$('remote-workspace').value='/draft/project';context.$('model-paths').value='{"fixture":"/draft/model"}';
  context.request=async()=>({sessions:[],machine_profiles:[profile('machine','musa')],task_devices:[device('machine','musa')]});await context.refresh(true);
  assert.equal(context.$('backend').value,'ascend');assert.equal(context.$('ssh-host').value,'draft.example.test');assert.equal(context.$('remote-workspace').value,'/draft/project');assert.equal(context.$('model-paths').value,'{"fixture":"/draft/model"}');assert.match(context.$('profile-observation').textContent,/手动选择的 Backend 为准/);
});

test('focused device menus defer polling rebuilds until blur and preserve a newer user selection',()=>{
  const {context}=ui();context.state.taskDevices=[device('one'),device('two')];context.showProfiles();select(context,'one');const menu=context.$('profile'),before=menu.rebuilds;context.document.activeElement=menu;
  context.state.taskDevices=[device('one','musa'),device('two','ascend',{label:'Updated two'})];context.showProfiles();assert.equal(menu.rebuilds,before);assert.equal(context.state.profileRenderPending,true);assert.equal(context.$('backend').value,'musa');
  select(context,'two');assert.equal(context.$('backend').value,'ascend');context.document.activeElement=null;menu.dispatch('blur');assert.equal(menu.rebuilds,before+1);assert.equal(menu.value,'two');assert.match(menu.textContent,/Updated two/);assert.equal(context.state.profileRenderPending,false);
});

test('AMD is visibly monitor-only and cannot become an automatic or custom task backend',()=>{
  const {context}=ui();const amd=device('amd','rocm',{task_supported:false});context.state.profiles=[amd];context.state.taskDevices=[amd,device('nvidia'),device('ascend','ascend')];context.showProfiles();
  assert.deepEqual(context.$('profile').children.slice(1).map(group=>group.label),['NVIDIA / 英伟达','AMD / 超威半导体','Ascend / 昇腾']);const option=context.$('profile').options.find(item=>item.value==='amd');assert.equal(option.disabled,true);assert.match(option.textContent,/仅资源监控/);
  select(context,'amd');context.checkButtons();assert.equal(context.$('backend').value,'');assert.equal(context.$('create-start').disabled,true);assert.match(context.$('profile-backend').textContent,/AMD \/ 超威半导体 · 仅资源监控/);assert.throws(()=>context.fields(),/仅支持资源监控/);
  context.$('custom-settings').checked=true;context.configureTaskMode();context.$('backend').value='cuda';assert.throws(()=>context.fields(),/仅用于资源监控/);assert.equal(context.$('backend').options.some(item=>item.value==='rocm'),false);
});

test('stale and unverified observations remain selectable with explicit revalidation guidance',()=>{
  for(const observation of [{verified:true,stale:true,updated_at:'2026-01-01T00:00:00Z'},{verified:false,stale:false,updated_at:'2026-01-01T00:00:00Z'}]){
    const {context}=ui();context.state.taskDevices=[device('pending','',{device_observation:observation})];context.showProfiles();select(context,'pending');context.checkButtons();
    assert.equal(context.$('profile').options.find(item=>item.value==='pending').disabled,false);assert.equal(context.$('create-start').disabled,false);assert.equal(context.fields().automatic,true);assert.match(context.$('profile-observation').textContent,observation.stale?/已过期.*重新确认/:/尚未核验.*重新识别/);assert.doesNotMatch(context.$('profile-observation').textContent,/已核验实际硬件/);
  }
});

test('both backend input and change events invalidate preflight in custom mode',()=>{
  const {context}=ui();context.$('custom-settings').checked=true;context.configureTaskMode();assert.equal(context.$('backend').disabled,false);
  for(const event of ['input','change']){context.state.checkedKey='previous-preflight';context.$('backend').value='musa';context.$('backend').dispatch(event);assert.equal(context.state.checkedKey,'');assert.equal(context.$('connection-check').hidden,true);}
});

test('device deep links use the collected source and reject archived or monitor-only targets',()=>{
  for(const [id,devices] of [['archived',[device('observed')]],['amd',[device('amd','rocm',{task_supported:false})]]]){
    const {context,notices}=ui();context.state.profiles=[profile(id)];context.state.taskDevices=devices;context.requestedProfile=id;context.showProfiles();context.applyRequestedProfile();assert.equal(context.$('profile').value,'');assert.equal(context.$('new-task').open,undefined);assert(notices.some(item=>/尚未采集/.test(item.message)));
  }
  const {context}=ui();context.state.profiles=[];context.state.taskDevices=[device('observed','musa')];context.requestedProfile='observed';context.showProfiles();context.applyRequestedProfile();assert.equal(context.$('profile').value,'observed');assert.equal(context.$('backend').value,'musa');assert.equal(context.$('new-task').open,true);assert.equal(context.$('branch').value,'chosen/branch');
});

test('task snapshot parsing distinguishes an omitted task_devices field from an explicit empty or invalid source',async()=>{
  const {context}=ui();for(const [extra,expected] of [[{},['archive']],[{task_devices:[]},[]],[{task_devices:null},[]]]){
    context.request=async()=>({sessions:[],machine_profiles:[profile('archive')],...extra});await context.refresh(true);assert.deepEqual(ids(context),expected);
  }
});

test('automatic backend summary is outside advanced settings and the task backend dropdown never gains AMD',()=>{
  assert(html.indexOf('id="profile-backend-summary"')<html.indexOf('id="advanced-settings"'));assert.match(html,/<fieldset id="custom-fields" disabled hidden/);
  assert.doesNotMatch(html.match(/<select id="backend">([\s\S]*?)<\/select>/)[1],/rocm|AMD/);
});


test('a pending deep link waits for a focused dropdown to rebuild without overriding a user choice',()=>{
  for(const chooseManually of [false,true]){
    const {context}=ui();context.state.taskDevices=[];context.showProfiles();context.requestedProfile='target';context.document.activeElement=context.$('profile');context.state.taskDevices=[device('target')];context.showProfiles();context.applyRequestedProfile();assert.equal(context.profileLinkHandled,false);
    if(chooseManually){context.$('profile').dispatch('change');}
    context.document.activeElement=null;context.$('profile').dispatch('blur');assert.equal(context.$('profile').value,chooseManually?'':'target');assert.equal(context.profileLinkHandled,true);
  }
});

test('complete sessions script remains syntactically valid',()=>{assert.doesNotThrow(()=>new vm.Script(source));});
