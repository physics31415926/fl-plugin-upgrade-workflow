const assert=require('node:assert/strict');
const fs=require('node:fs');
const path=require('node:path');
const vm=require('node:vm');
const test=require('node:test');
const template=name=>fs.readFileSync(path.join(__dirname,'..','web/templates',name),'utf8');
const script=name=>[...template(name).matchAll(/<script\b[^>]*>([\s\S]*?)<\/script>/g)].map(match=>match[1]).join('\n');
const settle=()=>new Promise(resolve=>setImmediate(resolve));
class Node {
  constructor(tag='div'){this.tag=tag;this.children=[];this.listeners={};this.attributes={};this._text='';this.value='';this.hidden=false;this.disabled=false;}
  set textContent(value){this._text=String(value);this.children=[];}
  get textContent(){return this._text+this.children.map(child=>child.textContent).join('');}
  append(...nodes){for(const node of nodes)if(node.tag==='#fragment')this.children.push(...node.children);else this.children.push(node);}
  replaceChildren(...nodes){this.children=[];this._text='';this.append(...nodes);}
  addEventListener(name,fn){this.listeners[name]=fn;}
  setAttribute(name,value){this.attributes[name]=value;}
  focus(){this.focused=true;}
  select(){this.selected=true;}
}
const token='a'.repeat(43),publicUrl='https://dashboard.example.test',sharedUrl=publicUrl+'/access#token='+token;
function manager(responseFn){
  const nodes=new Map(),calls=[],copies=[];
  const context={URL,Date,JSON,Number,encodeURIComponent,navigator:{clipboard:{writeText:async text=>copies.push(text)}},
    document:{getElementById:id=>{if(!nodes.has(id))nodes.set(id,new Node());return nodes.get(id);},createElement:tag=>new Node(tag),createDocumentFragment:()=>new Node('#fragment')},
    fetch:async(url,options)=>{calls.push({url,options});const result=responseFn?await responseFn(url,options):{available:true,public_url:publicUrl,shares:[]};return{ok:result.ok!==false,json:async()=>result};}};
  vm.createContext(context);vm.runInContext(script('access_shares.html'),context);
  const node=id=>context.document.getElementById(id);node('share-duration').value='24';
  return{context,node,calls,copies};
}

test('share management and recipient scripts parse and use no external resources',()=>{
  for(const name of ['access_shares.html','access.html']){new vm.Script(script(name));assert.doesNotMatch(template(name),/<script[^>]+src=|<link[^>]+href=/);assert.match(template(name),/name="referrer" content="no-referrer"/);}
});

test('management stays disabled when HTTPS sharing is not configured',async()=>{
  const ui=manager(()=>({available:false,public_url:'',shares:[]}));await settle();assert.equal(ui.node('create-share').disabled,true);assert.match(ui.node('notice').textContent,/尚未启用/);
  await ui.node('share-form').listeners.submit({preventDefault(){}});assert.equal(ui.calls.length,1);
});

test('creating a share reveals it once and only explicit copy sends it to clipboard',async()=>{
  const share={id:'example-id',label:'Review',active:true,expires_at:'2030-01-01T00:00:00Z'};
  const ui=manager((url,options)=>options.method==='POST'?{share,url:sharedUrl}:{available:true,public_url:publicUrl,shares:[]});await settle();
  ui.node('share-label').value=' Review ';await ui.node('share-form').listeners.submit({preventDefault(){}});
  assert.deepEqual(JSON.parse(ui.calls[1].options.body),{label:'Review',expires_in_hours:24});assert.equal(ui.node('share-url').value,sharedUrl);assert.equal(ui.node('fresh-share').hidden,false);assert.equal(ui.copies.length,0);
  await ui.node('copy-share').listeners.click();assert.deepEqual(ui.copies,[sharedUrl]);ui.node('dismiss-share').listeners.click();assert.equal(ui.node('share-url').value,'');assert.equal(ui.node('fresh-share').hidden,true);
  await ui.node('copy-share').listeners.click();assert.equal(ui.copies.length,1);
});

test('permanent links send an explicit lifetime and show revocable permanence',async()=>{
  const share={id:'permanent-id',label:'Ongoing',active:true,permanent:true,expires_at:null};
  const ui=manager((url,options)=>options.method==='POST'?{share,url:sharedUrl}:{available:true,public_url:publicUrl,shares:[share]});await settle();
  ui.node('share-duration').value='permanent';ui.node('share-label').value='Ongoing';await ui.node('share-form').listeners.submit({preventDefault(){}});
  assert.deepEqual(JSON.parse(ui.calls[1].options.body),{label:'Ongoing',permanent:true});
  assert.match(ui.node('fresh-expires').textContent,/永久有效.*撤销/);
  const row=ui.node('share-list').children[0];assert.equal(row.children[2].textContent,'永久有效（可撤销）');
  assert.equal(row.children[2].children.length,0);assert.equal(row.children[3].children[0].textContent,'撤销权限');
  assert.match(template('access_shares.html'),/<option value="24" selected>/);
  assert.match(template('access_shares.html'),/<option value="permanent">永久有效（可撤销）<\/option>/);
});

test('share creation rejects a link outside configured HTTPS origin without showing token',async()=>{
  const ui=manager((url,options)=>options.method==='POST'?{share:{id:'bad'},url:'https://unrelated.example/access#token='+token}:{available:true,public_url:publicUrl,shares:[]});await settle();
  await ui.node('share-form').listeners.submit({preventDefault(){}});assert.equal(ui.node('share-url').value,'');assert.match(ui.node('notice').textContent,/分享链接无效/);assert.equal(ui.copies.length,0);
});

test('clipboard denial selects the visible link for manual copying',async()=>{
  const ui=manager((url,options)=>options.method==='POST'?{share:{id:'id',expires_at:'2030-01-01'},url:sharedUrl}:{available:true,public_url:publicUrl,shares:[]});await settle();await ui.node('share-form').listeners.submit({preventDefault(){}});
  ui.context.navigator.clipboard.writeText=async()=>{throw Error('clipboard denied');};await ui.node('copy-share').listeners.click();assert.equal(ui.node('share-url').selected,true);assert.match(ui.node('notice').textContent,/手动复制/);
});

test('list renders untrusted labels as text and revokes the selected grant',async()=>{
  const share={id:'some-id',label:'<img src=x onerror=alert(1)>',active:true,expires_at:'2030-01-01T00:00:00Z'};
  const ui=manager((url,options)=>options.method==='POST'?{}:{available:true,public_url:publicUrl,shares:[share]});await settle();
  const row=ui.node('share-list').children[0];assert.equal(row.children[0].textContent,share.label);const button=row.children[3].children[0];await button.listeners.click();assert.equal(ui.calls[1].url,'/api/access-shares/some-id/revoke');assert.deepEqual(JSON.parse(ui.calls[1].options.body),{});assert.match(ui.node('notice').textContent,/权限已撤销/);
});

function recipient(fragment,{ok=true,networkError=false}={}){
  const nodes=new Map(),events=[];const context={URL,URLSearchParams,JSON,document:{getElementById:id=>{if(!nodes.has(id))nodes.set(id,new Node());return nodes.get(id);}},
    location:{hash:fragment,pathname:'/access',search:'',href:publicUrl+'/access'+fragment,replace:url=>events.push({kind:'redirect',url})},
    history:{replaceState:(_a,_b,url)=>events.push({kind:'clear',url})},fetch:async(url,options)=>{events.push({kind:'fetch',url:String(url),options});if(networkError)throw Error('offline');return{ok};}};
  vm.createContext(context);const done=vm.runInContext(script('access.html'),context);return{context,events,nodes,done};
}

test('recipient removes bearer token from history before exchanging it for a session',async()=>{
  const ui=recipient('#token='+token);await ui.done;assert.deepEqual(ui.events.map(item=>item.kind),['clear','fetch','redirect']);assert.equal(ui.events[0].url,'/access');assert.equal(ui.events[1].url,publicUrl+'/api/share-login');assert.equal(ui.events[1].options.redirect,'error');assert.equal(ui.events[1].options.credentials,'same-origin');assert.equal(ui.events[1].options.referrerPolicy,'no-referrer');assert.deepEqual(JSON.parse(ui.events[1].options.body),{token});assert.equal(ui.events[2].url,publicUrl+'/sessions');
});

test('recipient rejects missing or malformed tokens without a request',async()=>{
  for(const fragment of ['','#token=short','#token='+token+'%3A']){const ui=recipient(fragment);await ui.done;assert.equal(ui.events.length,1);assert.equal(ui.events[0].kind,'clear');assert.match(ui.nodes.get('message').textContent,/到期、被撤销或不完整/);}
});

test('expired or revoked shares never redirect into a private workspace',async()=>{
  const ui=recipient('#token='+token,{ok:false});await ui.done;assert.deepEqual(ui.events.map(item=>item.kind),['clear','fetch']);assert.match(ui.nodes.get('title').textContent,/不可用/);assert.doesNotMatch(ui.nodes.get('message').textContent,new RegExp(token));
});

test('network failure gives a retry path without keeping token in history or text',async()=>{
  const ui=recipient('#token='+token,{networkError:true});await ui.done;assert.deepEqual(ui.events.map(item=>item.kind),['clear','fetch']);assert.match(ui.nodes.get('message').textContent,/重新打开.*完整链接/);assert.doesNotMatch(ui.nodes.get('message').textContent,new RegExp(token));
});

test('workbench exposes link management only to the local capability and supports explicit logout',()=>{
  const source=script('sessions.html');assert.match(source,/\$\('access-shares-link'\)\.hidden=capabilities\.access_shares!==true/);assert.match(source,/\$\('shared-access-mode'\)\.hidden=capabilities\.shared_access!==true/);assert.match(source,/\$\('share-logout'\)\.addEventListener\('click',logoutShare\)/);
  const overview=script('overview.html');assert.match(overview,/\$\('access-shares-link'\)\.hidden=capabilities\.access_shares!==true/);
});


test('share logout uses a global same-origin endpoint regardless of selected project',async()=>{
  const source=script('sessions.html'),start=source.indexOf('  async function logoutShare('),end=source.indexOf('  initialize();',start);assert(start>=0&&end>start);
  for(const projectId of ['vllm','sglang']){
    const button=new Node('button'),calls=[],redirects=[],context={projectId,$:()=>button,post:()=>{throw Error('project request must not be used');},projectUrl:()=>{throw Error('project URL must not be used');},notice:()=>{},location:{href:publicUrl+'/sessions?project='+projectId,replace:url=>redirects.push(url)},fetch:async(url,options)=>{calls.push({url,options});return{ok:true};}};
    vm.createContext(context);vm.runInContext(source.slice(start,end),context);await button.listeners.click();
    assert.equal(calls.length,1);assert.equal(calls[0].url,'/api/share-logout');assert.equal(calls[0].options.credentials,'same-origin');assert.equal(calls[0].options.redirect,'error');assert.equal(calls[0].options.headers['Content-Type'],'application/json');assert.deepEqual(JSON.parse(calls[0].options.body),{});assert.deepEqual(redirects,['/']);assert.equal(button.disabled,true);
  }
});

test('failed logout keeps the page open and enables retry',async()=>{
  const source=script('sessions.html'),start=source.indexOf('  async function logoutShare('),end=source.indexOf('  initialize();',start);
  const button=new Node('button'),notices=[],context={$:()=>button,location:{replace:()=>{throw Error('must not navigate on failure');}},notice:(_id,message,error)=>notices.push({message,error}),fetch:async()=>({ok:false})};vm.createContext(context);vm.runInContext(source.slice(start,end),context);await button.listeners.click();assert.equal(button.disabled,false);assert.equal(notices.length,1);assert.match(notices[0].message,/退出管理未完成/);assert.equal(notices[0].error,true);
});
