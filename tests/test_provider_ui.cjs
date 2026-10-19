const assert=require('node:assert/strict');
const fs=require('node:fs');
const path=require('node:path');
const vm=require('node:vm');
const test=require('node:test');
const html=fs.readFileSync(path.join(__dirname,'..','web/templates/sessions.html'),'utf8');
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
  context.$('custom-settings').checked=true;context.state.profiles=[];context.labels={cuda:'NVIDIA / CUDA'};
  vm.runInContext(between('  function fields(', '  function connectionMode('),context);
  const body=context.fields();assert.equal(body.provider,'claude');assert.equal(body.backend,'cuda');
  assert.equal(JSON.stringify(body).includes('explicit-dummy-key'),false);assert.equal(Object.hasOwn(body,'api_key'),false);
});
test('backend ordering keeps NVIDIA first and sorts iluvatar by display label',()=>{
  const context={document:{querySelector:()=>null},$(){return{append(){}}}};
  vm.createContext(context);
  vm.runInContext(between('  const labels =', '  function providerName(')+'\nglobalThis.order=orderedBackends;',context);
  assert.deepEqual(Array.from(context.order),['cuda','ascend','enflame','hygon','corex','kunlunxin','metax','musa','sunrise','thead','tsingmicro']);
  const staticOptions=html.match(/<select id="backend"[^>]*>([\s\S]*?)<\/select>/)[1];
  assert.deepEqual([...staticOptions.matchAll(/<option value="([^"]+)"/g)].map(match=>match[1]),Array.from(context.order));
});
test('Claude denial state can be followed up without fake approval IDs',()=>{
  const {context}=ui();
  assert.equal(context.claudeDenied({provider:'claude',status:'waiting_input',claude_permission_denials:[{tool_name:'Bash'}]}),true);
  assert.equal(context.claudeDenied({provider:'codex',status:'waiting_input',pending_requests:[{id:1}]}),false);
});

function flagscaleForm(context){
  const values={'flagscale-llm-provider':'anthropic','flagscale-base-url':'https://api.example.test','flagscale-model':'model-demo','flagscale-max-iterations':'200','flagscale-max-continuations':'20','flagscale-max-output-tokens':'8192','flagscale-thinking-budget':'0','flagscale-time-budget-sec':'0'};
  for(const [id,value]of Object.entries(values))context.$(id).value=value;
}
test('FlagScale readiness and configuration remain independent from Codex and Claude',()=>{
  const {context}=ui();context.$('agent-provider').value='flagscale';context.state.providers.claude.configured=true;
  assert.equal(context.providerAvailable(),false);
  context.state.providers.flagscale={available:true,configured:false};assert.equal(context.providerAvailable(),false);
  context.state.providers.flagscale.configured=true;assert.equal(context.providerAvailable(),true);
  context.providerChanged();assert.equal(context.$('flagscale-settings-panel').hidden,false);assert.equal(context.$('claude-settings-panel').hidden,true);
  assert.match(context.$('provider-hint').textContent,/跟进继续使用同一工作目录/);
  assert.equal(context.providerName('flagscale'),'FlagScale Agent');
});
test('FlagScale safe settings initialize public fields and preserve key and edited fields during polling',()=>{
  const {context}=ui();context.$('flagscale-api-key').value='explicit-dummy-flagscale-key';
  context.renderFlagscaleSettings({provider:'flagscale',configured:true,base_url:'https://api.example.test',model:'model-demo',llm_provider:'openai',max_iterations:10,max_continuations:0,max_output_tokens:4096,thinking_budget:0,time_budget_sec:0});
  assert.equal(context.$('flagscale-time-budget-sec').value,0);assert.equal(context.$('flagscale-llm-provider').value,'openai');
  context.$('flagscale-model').value='pending-model';context.$('flagscale-base-url').value='https://pending.example.test';
  context.renderFlagscaleSettings({provider:'flagscale',configured:true,model:'server-model'});
  assert.equal(context.$('flagscale-model').value,'pending-model');assert.equal(context.$('flagscale-base-url').value,'https://pending.example.test');
  assert.equal(context.$('flagscale-api-key').value,'explicit-dummy-flagscale-key');assert.match(context.$('flagscale-key-state').textContent,/不会回填/);
  assert.equal(context.state.agentSettings,undefined,'FlagScale metadata must not replace Claude settings');
});
test('FlagScale saves only its settings payload and clears key after success or failure',async()=>{
  for(const fail of [false,true]){
    const {context}=ui();flagscaleForm(context);context.$('flagscale-api-key').value='explicit-dummy-flagscale-key';context.$('claude-api-key').value='explicit-dummy-claude-key';let request;
    context.post=async(url,body)=>{request={url,body};if(fail)throw Error('simulated error');return {settings:{...body,configured:true,api_key:undefined}};};
    await context.saveFlagscaleSettings();assert.equal(request.url,'/api/agent-settings');assert.equal(request.body.provider,'flagscale');assert.equal(request.body.llm_provider,'anthropic');assert.equal(request.body.time_budget_sec,0);
    assert.equal(request.body.api_key,'explicit-dummy-flagscale-key');assert.equal(JSON.stringify(request.body).includes('explicit-dummy-claude-key'),false);
    assert.equal(context.$('flagscale-api-key').value,'');assert.equal(context.$('claude-api-key').value,'explicit-dummy-claude-key');assert.equal(context.$('save-flagscale-settings').disabled,false);assert.equal(context.$('flagscale-settings-notice').error,fail);
  }
});
test('FlagScale delete-key request never submits a pending replacement key',async()=>{
  const {context}=ui();flagscaleForm(context);context.$('flagscale-api-key').value='explicit-dummy-replacement-key';let body;
  context.post=async(url,value)=>{body=value;return {settings:{...value,configured:false}};};
  await context.saveFlagscaleSettings(true);assert.equal(body.provider,'flagscale');assert.equal(body.delete_api_key,true);assert.equal(Object.hasOwn(body,'api_key'),false);assert.equal(context.$('flagscale-api-key').value,'');
});
test('FlagScale rejects invalid budgets and read-only pages cannot save either provider configuration',async()=>{
  for(const [id,value]of [['flagscale-max-iterations','5001'],['flagscale-time-budget-sec',''],['flagscale-thinking-budget','8192']]){
    const {context}=ui();flagscaleForm(context);context.$(id).value=value;context.$('flagscale-api-key').value='explicit-dummy-key';let calls=0;context.post=async()=>{calls++;};await context.saveFlagscaleSettings();assert.equal(calls,0);assert.equal(context.$('flagscale-api-key').value,'');assert.equal(context.$('flagscale-settings-notice').error,true);
  }
  const {context}=ui();flagscaleForm(context);context.state.readOnly=true;context.$('agent-provider').value='flagscale';let calls=0;context.post=async()=>{calls++;};await context.saveFlagscaleSettings();await context.saveClaudeSettings();context.providerChanged();
  assert.equal(calls,0);assert.equal(context.$('flagscale-settings-panel').hidden,true);assert.equal(context.$('claude-settings-panel').hidden,true);
});
test('FlagScale new-task payload includes provider and never copies either settings key',()=>{
  const {context}=ui();const values={'agent-provider':'flagscale','profile':'','backend':'cuda','branch':'main','ssh-alias':'gpu_demo','remote-workspace':'/work/plugin','connection-mode':'alias','model-paths':'','goal':'test'};
  for(const [id,value]of Object.entries(values))context.$(id).value=value;
  context.$('flagscale-api-key').value='explicit-dummy-flagscale-key';context.$('claude-api-key').value='explicit-dummy-claude-key';context.$('custom-settings').checked=true;context.state.profiles=[];context.labels={cuda:'NVIDIA / CUDA'};
  vm.runInContext(between('  function fields(', '  function connectionMode('),context);
  const body=context.fields();assert.equal(body.provider,'flagscale');assert.equal(Object.hasOwn(body,'api_key'),false);assert.doesNotMatch(JSON.stringify(body),/explicit-dummy/);
});
