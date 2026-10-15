const assert=require('node:assert/strict'),fs=require('node:fs'),path=require('node:path'),vm=require('node:vm'),test=require('node:test');
const html=fs.readFileSync(process.env.FL_SESSION_DRAFT_SOURCE||path.join(__dirname,'..','web/templates/sessions.html'),'utf8');
const start='  // BEGIN session message drafts',end='  // END session message drafts';
const begin=html.indexOf(start),finish=html.indexOf(end,begin);
assert(begin>=0&&finish>begin,'session message draft helper exists');
const source=html.slice(begin,finish+end.length);
function ui(){
  const input={value:'',selectionStart:0,selectionEnd:0},sessions=[{id:'task-a',provider:'codex'},{id:'task-b',provider:'claude'}],calls=[];
  const context={state:{readOnly:false,actionBusy:false,selected:'task-a',sessions},$:id=>{assert.equal(id,'message');return input;},current:()=>sessions.find(session=>session.id===context.state.selected),act:async(session,action,body)=>{calls.push({session,action,body});return true;}};
  vm.createContext(context);vm.runInContext(source,context);
  const select=id=>{context.state.selected=id;context.syncMessageDraft(context.current());};
  const type=text=>{input.value=text;context.rememberMessageDraft();};
  const submit=()=>context.submitMessage({preventDefault(){}});
  select('task-a');
  return {context,input,sessions,calls,select,type,submit};
}
function deferred(){let release;const promise=new Promise(resolve=>{release=resolve;});return {promise,release};}
test('each selected task restores its own in-memory followup draft',()=>{
  const {input,select,type}=ui();type('Requirements for A');select('task-b');assert.equal(input.value,'');type('Requirements for B');select('task-a');assert.equal(input.value,'Requirements for A');select('task-b');assert.equal(input.value,'Requirements for B');
});
test('polling the same task preserves input text and caret without rewriting the textarea',()=>{
  const {context,input,type}=ui();type('A draft being edited');input.selectionStart=3;input.selectionEnd=8;let writes=0,text=input.value;
  Object.defineProperty(input,'value',{get:()=>text,set:value=>{writes++;text=value;}});context.syncMessageDraft({...context.current(),status:'running'});
  assert.equal(writes,0);assert.equal(input.value,'A draft being edited');assert.equal(input.selectionStart,3);assert.equal(input.selectionEnd,8);
});
test('a successful message uses the selected task and clears only its sent draft',async()=>{
  const {input,calls,type,submit,select}=ui();type('  Continue task A  ');assert.equal(await submit(),true);assert.equal(calls.length,1);assert.equal(calls[0].session.id,'task-a');assert.equal(calls[0].action,'message');assert.deepEqual(JSON.parse(JSON.stringify(calls[0].body)),{text:'Continue task A'});assert.equal(input.value,'');select('task-b');type('B stays here');select('task-a');assert.equal(input.value,'');
});
test('changing tasks during a pending message cannot redirect it or clear the new selection',async()=>{
  const {context,input,calls,type,submit,select}=ui(),pending=deferred();context.act=async(session,action,body)=>{calls.push({session,action,body});return pending.promise;};
  type('Send to A');const sending=submit();select('task-b');type('Do not send B yet');pending.release(true);assert.equal(await sending,true);assert.equal(calls.length,1);assert.equal(calls[0].session.id,'task-a');assert.equal(calls[0].body.text,'Send to A');assert.equal(input.value,'Do not send B yet');select('task-a');assert.equal(input.value,'');select('task-b');assert.equal(input.value,'Do not send B yet');
});
test('a newer edit to the same task survives completion of an earlier message',async()=>{
  const {context,input,calls,type,submit,select}=ui(),pending=deferred();context.act=async(session,action,body)=>{calls.push({session,action,body});return pending.promise;};
  type('First version');const sending=submit();type('New revised version');pending.release(true);assert.equal(await sending,true);assert.equal(calls[0].body.text,'First version');assert.equal(input.value,'New revised version');select('task-b');select('task-a');assert.equal(input.value,'New revised version');
});
test('editing and restoring the original text still counts as a new unsent draft version',async()=>{
  const {context,input,type,submit}=ui(),pending=deferred();context.act=async()=>pending.promise;type('Original version');const sending=submit();type('Replacement');type('Original version');pending.release(true);await sending;assert.equal(input.value,'Original version');
});
test('a failed message keeps both its draft and another task draft',async()=>{
  const {context,input,calls,type,submit,select}=ui(),pending=deferred();context.act=async(session,action,body)=>{calls.push({session,action,body});return pending.promise;};
  type('Retry A later');const sending=submit();select('task-b');type('B in progress');pending.release(false);assert.equal(await sending,false);assert.equal(input.value,'B in progress');select('task-a');assert.equal(input.value,'Retry A later');assert.equal(calls.length,1);
});
test('read-only and busy pages cannot dispatch or remove draft instructions',async()=>{
  for(const stateName of ['readOnly','actionBusy']){const {context,input,calls,type,submit}=ui();type('Keep this draft');context.state[stateName]=true;assert.equal(await submit(),false);assert.equal(calls.length,0);assert.equal(input.value,'Keep this draft');}
});
test('empty messages and missing selected tasks never dispatch',async()=>{
  const {context,calls,input,type,submit,select}=ui();type('   ');assert.equal(await submit(),false);assert.equal(calls.length,0);type('Keep A');select('missing');assert.equal(input.value,'');assert.equal(await submit(),false);assert.equal(calls.length,0);select('task-a');assert.equal(input.value,'Keep A');
});
test('a changed selected task object does not mutate the target captured for sending',async()=>{
  const {context,input,calls,sessions,type,submit}=ui(),pending=deferred();context.act=async(session,action,body)=>{calls.push({session,action,body});return pending.promise;};
  type('Original target');const sending=submit();sessions[0].provider='flagscale';pending.release(true);await sending;assert.equal(calls[0].session.provider,'codex');assert.equal(input.value,'');
});
test('draft storage remains in memory and never uses browser persistent storage',()=>{
  assert.doesNotMatch(source,/localStorage|sessionStorage|indexedDB|document\.cookie/);
});
