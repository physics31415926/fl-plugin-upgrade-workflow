const assert=require('node:assert/strict'),fs=require('node:fs'),path=require('node:path'),vm=require('node:vm'),test=require('node:test');
const text=fs.readFileSync(path.join(__dirname,'..','sessions.html'),'utf8'),source=[...text.matchAll(/<script\b[^>]*>([\s\S]*?)<\/script>/g)][0][1];
const start=source.indexOf('  function renderSshSync('),end=source.indexOf('  function showProfiles(',start);assert(start>=0&&end>start);
function ui(){
  const nodes=new Map(),calls=[],context={state:{readOnly:false,sshSync:null,sshSyncBusy:false,profiles:[{id:'old'}],creating:false,checking:false},
    $:id=>{if(!nodes.has(id))nodes.set(id,{value:'draft '+id,textContent:'',disabled:false,hidden:false});return nodes.get(id);},
    post:async(url,body)=>{calls.push({url,body});return{profiles:[{id:'new'}],ssh_config_sync:{status:'synced',changed:true,added:1,updated:2,removed:1,checked_at:'fixture-time'}};},
    showProfiles:()=>{context.shown=(context.shown||0)+1;},invalidateCheck:()=>{context.invalidated=(context.invalidated||0)+1;},formatTime:value=>value,errorMessage:value=>typeof value==='string'?value:value?.message||'',notice:(id,value)=>{context.$(id).textContent=value;}};
  for(const id of ['ssh-host','ssh-user','ssh-port','identity-file','jump-alias','ssh-alias','remote-workspace','model-paths','goal','backend','branch'])context.$(id);
  vm.createContext(context);vm.runInContext(source.slice(start,end),context);return{context,nodes,calls};
}
test('SSH synchronization updates the machine inventory and summary without overwriting unsaved connection fields',async()=>{
  const {context,nodes,calls}=ui(),before=JSON.stringify([...nodes].map(([id,node])=>[id,node.value]));
  await context.syncSshConfig();assert.equal(calls.length,1);assert.equal(calls[0].url,'/api/codex-machines/sync-ssh');assert.equal(context.state.profiles[0].id,'new');assert.equal(context.shown,1);assert.equal(context.invalidated,1);
  assert.equal(JSON.stringify([...nodes].filter(([id])=>!['sync-ssh','ssh-sync-status'].includes(id)).map(([id,node])=>[id,node.value])),before);
  assert.match(context.$('ssh-sync-status').textContent,/fixture-time.*新增 1.*更新 2.*移除 1/);assert.equal(context.$('sync-ssh').disabled,false);
});
test('SSH synchronization preserves prior profiles and exposes failed refresh instead of clearing the error',async()=>{
  const {context}=ui();context.state.sshSync={status:'synced',changed:false,checked_at:'prior'};context.post=async()=>{throw Error('同步暂不可用');};await context.syncSshConfig();
  assert.equal(context.state.profiles[0].id,'old');assert.equal(context.$('ssh-sync-status').textContent,'同步暂不可用');assert.equal(context.$('sync-ssh').disabled,false);assert.equal(context.state.sshSyncBusy,false);
});
test('read-only and busy pages never issue SSH synchronization requests',async()=>{
  for(const state of [{readOnly:true},{sshSyncBusy:true},{creating:true},{checking:true}]){const {context,calls}=ui();Object.assign(context.state,state);context.renderSshSync();await context.syncSshConfig();assert.equal(calls.length,0);assert.equal(context.$('sync-ssh').disabled,true);if(state.readOnly)assert.equal(context.$('sync-ssh').hidden,true);}
});
test('snapshot sync metadata accepts counts or alias lists without triggering connection loads',()=>{
  const {context}=ui();context.renderSshSync({status:'synced',changed:true,added:['one'],updated:['two','three'],removed:[],checked_at:'snapshot-time'});assert.match(context.$('ssh-sync-status').textContent,/snapshot-time.*新增 1.*更新 2.*移除 0/);assert.equal(context.shown,undefined);assert.equal(context.invalidated,undefined);
});

class Element {
  constructor(tag){this.tag=tag;this.children=[];this.value='';this._text='';this.hidden=false;}
  set textContent(value){this._text=String(value);this.children=[];}
  get textContent(){return this._text+this.children.map(child=>child.textContent).join('');}
  get firstChild(){return this.children[0];}
  append(...nodes){for(const node of nodes)if(node.tag==='#fragment')this.children.push(...node.children);else this.children.push(node);}
  replaceChildren(...nodes){this.children=[];this._text='';this.append(...nodes);}
}
function machineUi(){
  const nodes=new Map(),context={state:{readOnly:false,profiles:[],profileKey:'',checkedKey:'prior-check'},labels:{cuda:'NVIDIA'},orderedBackends:['cuda'],document:{createDocumentFragment:()=>new Element('#fragment')},
    $:id=>{if(!nodes.has(id))nodes.set(id,new Element('div'));return nodes.get(id);},
    el:(tag,cls,value)=>{const node=new Element(tag);node.className=cls;if(value!==undefined)node.textContent=value;return node;},
    formatTime:value=>value,errorMessage:value=>typeof value==='string'?value:value?.message||'',invalidateCheck:()=>{context.state.checkedKey='';context.invalidated=(context.invalidated||0)+1;}};
  const a=source.indexOf('  function sshConfigText('),b=source.indexOf('  function renderSshSync(',a),c=source.indexOf('  function showProfiles('),d=source.indexOf('  function renderList(',c);assert(a>=0&&b>a&&d>c);
  vm.createContext(context);vm.runInContext(source.slice(a,b)+source.slice(c,d),context);return{context,nodes};
}
test('latest SSH config address is rendered in inventory, selection and guidance without overwriting unsaved fields',()=>{
  const {context}=machineUi();context.state.profiles=[{id:'imported-demo',ssh_alias:'gpu_demo',label:'Demo GPU',backend:'cuda',remote_workspace:'/work/plugin',ssh_config_changed:true,ssh_config_connection:{host:'gpu-new.example.test',user:'demo_user',port:2222,identity_file:'/private/demo-key'},last_check:{ok:true,checked_at:'2026-01-01T00:00:00Z',checks:[{name:'ssh',status:'passed'}]}}];
  context.$('profile').value='imported-demo';context.$('ssh-host').value='pending-host.example.test';context.$('ssh-user').value='pending_user';context.$('ssh-port').value='2200';
  context.showProfiles();assert.match(context.$('machine-inventory').textContent,/主机 gpu-new\.example\.test.*用户 demo_user.*端口 2222/);assert.match(context.$('profile').textContent,/gpu-new\.example\.test/);assert.match(context.$('profile-guidance').textContent,/主机 gpu-new\.example\.test/);assert.match(context.$('profile-guidance').textContent,/SSH 配置已更新，请重新检查/);
  assert.doesNotMatch(context.$('machine-inventory').textContent,/private\/demo-key|预检通过/);assert.equal(context.$('ssh-host').value,'pending-host.example.test');assert.equal(context.$('ssh-user').value,'pending_user');assert.equal(context.$('ssh-port').value,'2200');assert.equal(context.invalidated,1);assert.equal(context.state.checkedKey,'');
});
test('read-only machine projection omits local SSH host, user, port and private key metadata',()=>{
  const {context}=machineUi();context.state.readOnly=true;context.state.profiles=[{id:'demo',ssh_alias:'gpu_demo',backend:'cuda',ssh_config_connection:{host:'gpu-private.example.test',user:'private_user',port:2299,identity_file:'/private/demo-key'}}];context.$('profile').value='demo';context.showProfiles();
  for(const id of ['profile','machine-inventory','profile-guidance'])assert.doesNotMatch(context.$(id).textContent,/gpu-private|private_user|2299|demo-key/);
});
