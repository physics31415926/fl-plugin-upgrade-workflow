const assert=require('node:assert/strict');
const fs=require('node:fs');
const path=require('node:path');
const vm=require('node:vm');
const test=require('node:test');
const html=fs.readFileSync(path.join(__dirname,'..','sessions.html'),'utf8');
const source=[...html.matchAll(/<script\b[^>]*>([\s\S]*?)<\/script>/g)][0][1];
function between(start,end){const a=source.indexOf(start),b=source.indexOf(end,a);assert(a>=0&&b>a);return source.slice(a,b);}
function ui(){
  const values={'agent-provider':'codex','claude-base-url':'https://api.example.test','claude-model':'sonnet','claude-permission-mode':'acceptEdits','claude-allowed-tools':'Read\nBash(ssh *)','claude-max-turns':'80','claude-api-key':''};
  const nodes=new Map(Object.entries(values).map(([id,value])=>[id,{value,textContent:'',hidden:false,disabled:false}]));
  const context={state:{available:true,providers:{codex:{available:true},claude:{available:true,configured:false}},settingsInitialized:false,settingsBusy:false},$:id=>{if(!nodes.has(id))nodes.set(id,{value:'',textContent:'',hidden:false,disabled:false});return nodes.get(id);},notice:(id,text,error)=>{const node=context.$(id);node.textContent=text;node.error=!!error;},checkButtons:()=>{},renderHealth:()=>{},refresh:async()=>{}};
  vm.createContext(context);vm.runInContext(between('  function providerName(', "  $('agent-provider').addEventListener("),context);
  return {context,nodes};
}
test('provider availability does not infer Claude readiness from Codex',()=>{
  const {context,nodes}=ui();assert.equal(context.providerAvailable(),true);
  nodes.get('agent-provider').value='claude';assert.equal(context.providerAvailable(),false);
  context.state.providers.claude.configured=true;assert.equal(context.providerAvailable(),true);
  context.providerChanged();assert.equal(context.$('claude-settings-panel').hidden,false);
});
test('safe settings render never refills key and does not overwrite pending edits on polling',()=>{
  const {context,nodes}=ui();nodes.get('claude-api-key').value='local-dummy-key';
  context.renderProviderSettings({configured:true,base_url:'https://api.example.test',model:'opus',allowed_tools:['Read'],permission_mode:'dontAsk',max_turns:5});
  assert.equal(nodes.get('claude-api-key').value,'local-dummy-key');
  nodes.get('claude-model').value='pending-edit';
  context.renderProviderSettings({configured:true,model:'server-value'});
  assert.equal(nodes.get('claude-model').value,'pending-edit');
  assert.match(context.$('claude-key-state').textContent,/不会回填/);
});
test('settings only submit to settings API and key is cleared after success and failure',async()=>{
  for(const fail of [false,true]){
    const {context,nodes}=ui();nodes.get('claude-api-key').value='explicit-dummy-key';
    let request;
    context.post=async(url,body)=>{request={url,body};if(fail)throw Error('simulated failure');return {provider:'claude',configured:true,model:'sonnet',allowed_tools:['Read'],permission_mode:'acceptEdits',max_turns:80};};
    await context.saveClaudeSettings();
    assert.equal(request.url,'/api/agent-settings');assert.equal(request.body.provider,'claude');
    assert.equal(request.body.api_key,'explicit-dummy-key');
    assert.equal(nodes.get('claude-api-key').value,'');
    assert.equal(context.$('save-claude-settings').disabled,false);
    assert.equal(context.$('claude-settings-notice').error,fail);
  }
});
test('new task fields send provider but never include the settings key',()=>{
  const {context,nodes}=ui();
  const values={'agent-provider':'claude','profile':'','backend':'cuda','branch':'main','ssh-alias':'gpu_demo','remote-workspace':'/work/plugin','connection-mode':'alias','model-paths':'','goal':'test'};
  for(const [id,value]of Object.entries(values))context.$(id).value=value;
  context.$('claude-api-key').value='explicit-dummy-key';
  context.state.profiles=[];context.labels={cuda:'NVIDIA / CUDA'};
  vm.runInContext(between('  function fields(', '  function connectionMode('),context);
  const body=context.fields();assert.equal(body.provider,'claude');assert.equal(body.backend,'cuda');
  assert.equal(JSON.stringify(body).includes('explicit-dummy-key'),false);assert.equal(Object.hasOwn(body,'api_key'),false);
});
test('backend ordering keeps NVIDIA first and sorts iluvatar by display label',()=>{
  const context={document:{querySelector:()=>null},$(){return{append(){}}}};
  vm.createContext(context);
  vm.runInContext(between('  const labels =', '  function providerName(')+'\nglobalThis.order=orderedBackends;',context);
  assert.deepEqual(Array.from(context.order),['cuda','ascend','enflame','hygon','corex','kunlunxin','metax','musa','thead']);
});
test('Claude denial state can be followed up without fake approval IDs',()=>{
  const {context}=ui();
  assert.equal(context.claudeDenied({provider:'claude',status:'waiting_input',claude_permission_denials:[{tool_name:'Bash'}]}),true);
  assert.equal(context.claudeDenied({provider:'codex',status:'waiting_input',pending_requests:[{id:1}]}),false);
});
