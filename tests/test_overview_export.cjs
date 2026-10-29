const assert=require('node:assert/strict');
const fs=require('node:fs');
const path=require('node:path');
const vm=require('node:vm');
const test=require('node:test');
const source=fs.readFileSync(path.join(__dirname,'..','web/assets/overview-export.js'),'utf8');
const exportedAt='2026-10-29T04:05:06.000Z';
function api(extra={}){const context={URL,Date,...extra};vm.createContext(context);vm.runInContext(source,context);return context.FLOverviewExport;}
function project(overrides={}){return {id:'vllm',label:'vLLM-Plugin-FL',branch:'release/next',engine:'vllm',version:'0.28.0',head:'a'.repeat(40),head_url:'https://github.com/example/plugin/commit/'+ 'a'.repeat(40),observed_at:'2026-10-28T03:04:05Z',backends:[{id:'cuda',label:'NVIDIA / 英伟达',pr:{number:42,title:'Upgrade',url:'https://github.com/example/plugin/pull/42'},integration:'merged',ci:'passed',ci_url:'https://github.com/example/plugin/actions/runs/123/job/456',acceptance:{status:'passed',source:'pr_description',summary:'20/20 通过',pr_url:'https://github.com/example/plugin/pull/42',pr_number:42,rows:[{label:'单请求',configuration:'eager / Qwen',result:'1/1 通过',status:'passed'},{label:'并发请求',result:'8/8 通过',status:'passed'}],rows_total:2}}],...overrides};}
const build=projects=>api().buildReport(projects,{exportedAt});

test('report contains selected projects, independent branches, timestamps and offline acceptance rows',()=>{
 const html=build([project(),project({id:'sglang',label:'SGLang-Plugin-FL',branch:'dev/next',engine:'sglang',version:'0.5.18',observed_at:'2026-10-27T02:03:04Z'})]);
 for(const value of ['release/next','dev/next','vLLM-Plugin-FL','SGLang-Plugin-FL','0.28.0','0.5.18','2026-10-29 04:05:06 UTC','2026-10-28 03:04:05 UTC','2026-10-27 02:03:04 UTC','验收项','结果','单请求','eager / Qwen','8/8 通过','不会自动更新'])assert.ok(html.includes(value),value);
 assert.match(html,/<details open id="acceptance-0-0">/);assert.match(html,/href="#acceptance-1-0"/);
 assert.doesNotMatch(html,/<script\b|<link\b|<img\b|<iframe\b|@import|url\(/i);
 assert.match(html,/default-src 'none'/);assert.match(html,/@media print/);
});

test('backend order puts NVIDIA first and alphabetizes the other displayed names without mutating input',()=>{
 const p=project();p.backends=[{...p.backends[0],id:'musa',label:'Moore Threads'},{...p.backends[0],id:'ascend',label:'Ascend'},p.backends[0]];
 const before=JSON.stringify(p);const html=build([p]);
 assert.ok(html.indexOf('<strong>NVIDIA')<html.indexOf('<strong>Ascend'));assert.ok(html.indexOf('<strong>Ascend')<html.indexOf('<strong>Moore'));
 assert.equal(JSON.stringify(p),before);
});

test('failure and pending states retain their meaning and summaries use supplied backend CI status',()=>{
 const p=project();p.backends[0].ci='failed';p.backends[0].acceptance.status='partial';p.backends.push({...p.backends[0],id:'metax',label:'MetaX',integration:'open',ci:'stale',acceptance:{status:'pending'}});
 const html=build([p]);assert.match(html,/<span class="pill failed">未通过/);assert.match(html,/<span class="pill stale">证据过期/);assert.match(html,/<span class="pill partial">部分通过/);assert.match(html,/<span class="pill pending">待验收/);
 assert.match(html,/适配 CI 通过<\/span><strong>0/);
});

test('cached, collecting, failed and unavailable projects cannot appear newly collected',()=>{
 for(const [flags,notice]of [[{stale:true},'历史缓存'],[{refreshing:true},'后台仍在采集'],[{hasError:true},'最近刷新未完成']])assert.ok(build([project(flags)]).includes(notice));
 const html=build([project({unavailable:true})]);assert.match(html,/尚无采集结果/);assert.doesNotMatch(html,/PR #42|8\/8 通过|2026-10-28 03:04:05/);
});

test('only expected GitHub links survive and no report link can carry a management token',()=>{
 const p=project(),row=p.backends[0];row.pr.url='javascript:alert(1)';row.ci_url='https://github.com.evil.invalid/example/plugin/actions/runs/123';row.acceptance.pr_url='https://github.com/example/plugin/pull/42?token=private-grant';p.head_url='https://name:password@github.com/example/plugin/commit/'+ 'a'.repeat(40);
 const html=build([p]);assert.doesNotMatch(html,/javascript:|private-grant|github.com.evil|name:password|href="https:/);
 const good=build([project()]);assert.match(good,/href="https:\/\/github.com\/example\/plugin\/pull\/42"/);assert.match(good,/noopener noreferrer/);
});

test('malicious text is escaped and unknown raw or nested private fields never enter the report',()=>{
 const p=project({label:'<svg onload="boom">',sessions:[{prompt:'SESSION_CONTENT'}],device_config:{password:'DEVICE_PASSWORD'},private_origin:'http://10.1.2.3:8766'});
 p.backends[0].acceptance.rows=[{label:'<script>alert(1)</script>',configuration:'<img onerror="boom">',result:'<a href="javascript:boom">passed</a>'}];
 p.backends[0].acceptance.raw_body='RAW_PR_BODY';p.backends[0].acceptance.provenance={thread:'THREAD_SECRET'};p.backends[0].gate={source_thread:'ANOTHER_THREAD'};
 const html=build([p]);assert.ok(html.includes('&lt;svg onload=&quot;boom&quot;&gt;'));assert.ok(html.includes('&lt;script&gt;alert(1)&lt;/script&gt;'));
 for(const value of ['SESSION_CONTENT','DEVICE_PASSWORD','RAW_PR_BODY','THREAD_SECRET','ANOTHER_THREAD'])assert.ok(!html.includes(value),value);
 assert.doesNotMatch(html,/<svg\b|<script\b|<img\b/i);
});

test('freeform local summaries and rows redact credentials, paths, machine addresses and commands',()=>{
 const p=project(),r=p.backends[0].acceptance;r.source='local_record';r.summary='API_KEY=example-password sk-abcdefghijklmnopqrstuv D:\\private\\results 10.20.30.40 /home/person/run https://internal.invalid/path';
 r.rows=[{label:'验证',configuration:'password: local-password',result:'ssh host run-tests'}];
 const html=build([p]);for(const value of ['example-password','sk-abcdefghijklmnopqrstuv','private\\results','10.20.30.40','/home/person/run','internal.invalid','local-password','ssh host'])assert.ok(!html.includes(value),value);
 assert.match(html,/凭据已隐藏/);assert.match(html,/路径已隐藏/);assert.match(html,/测试命令（见原 PR）/);
});

test('normalized acceptance rows omit unrelated description tables and local prose',()=>{
 const p=project(),r=p.backends[0].acceptance;r.description_blocks=[{type:'paragraph',text:'LOCAL_CONTEXT'},{type:'heading',text:'Environment'},{type:'table',rows:[['Host','Name'],['ENVIRONMENT_ONLY','value']]},{type:'heading',text:'Tests'},{type:'table',rows:[['Case','Result'],['DUPLICATE_TABLE','passed']]}];
 const html=build([p]);assert.doesNotMatch(html,/LOCAL_CONTEXT|ENVIRONMENT_ONLY|DUPLICATE_TABLE/);assert.match(html,/8\/8 通过/);
});

test('fallback uses only test-context tables and converts all retained cells to two columns',()=>{
 const p=project(),r=p.backends[0].acceptance;r.rows=[];r.rows_total=0;r.description_blocks=[{type:'heading',text:'Environment'},{type:'table',rows:[['Host','Name'],['ENVIRONMENT_ONLY','value']]},{type:'heading',text:'CI results'},{type:'table',rows:[['Check','Status'],['CI_ONLY','passed']]},{type:'heading',text:'Validation results'},{type:'table',rows:[['Model','Mode','Result'],['Qwen','graph','8/8 通过']]},{type:'paragraph',text:'PRIVATE_PROSE'}];
 const html=build([p]);assert.match(html,/Qwen · graph/);assert.match(html,/8\/8 通过/);assert.doesNotMatch(html,/ENVIRONMENT_ONLY|CI_ONLY|PRIVATE_PROSE/);
 const table=html.match(/<table class="acceptance">([\s\S]*?)<\/table>/)[1];assert.equal((table.match(/<th scope=/g)||[]).length,2);
});

test('public acceptance tables and row truncation preserve honest limits',()=>{
 const p=project(),r=p.backends[0].acceptance;r.rows_truncated=true;r.rows_total=107;r.acceptance_tables=[{type:'table',rows:[['Case','Result'],['补充并发','16/16 通过']]}];
 const html=build([p]);assert.match(html,/摘要列出 2 条 \/ 共 107 条/);assert.match(html,/补充并发/);assert.match(html,/完整范围请查看原 PR/);
});

test('download creates a UTF-8 HTML file and releases the object URL after the browser has time to save',()=>{
 const blobs=[],revoked=[],timers=[],anchors=[];class TestBlob{constructor(parts,options){this.parts=parts;this.type=options.type;blobs.push(this);}}
 class TestURL extends URL{}TestURL.createObjectURL=blob=>(assert.equal(blob,blobs[0]),'blob:report');TestURL.revokeObjectURL=url=>revoked.push(url);
 const report=api({Blob:TestBlob,URL:TestURL,document:{body:{append:anchor=>anchors.push(anchor)},createElement:tag=>({tag,click(){this.clicked=true;},remove(){this.removed=true;}})},setTimeout:(fn,delay)=>timers.push({fn,delay})});
 const name=report.download([project()],{exportedAt});assert.equal(name,'FL-适配进展-20261029-040506Z.html');assert.equal(blobs[0].type,'text/html;charset=utf-8');assert.match(blobs[0].parts[0],/^<!doctype html>/);
 assert.equal(anchors[0].download,name);assert.equal(anchors[0].clicked,true);assert.equal(anchors[0].removed,true);assert.deepEqual(revoked,[]);assert.ok(timers[0].delay>=1000);timers[0].fn();assert.deepEqual(revoked,['blob:report']);
});

test('failed downloads remove the temporary element and immediately release its URL',()=>{
 const revoked=[],anchor={click(){throw new Error('save failed');},remove(){this.removed=true;}};class TestURL extends URL{}TestURL.createObjectURL=()=> 'blob:failed';TestURL.revokeObjectURL=url=>revoked.push(url);
 const report=api({Blob:class{},URL:TestURL,document:{body:{append(){}},createElement:()=>anchor},setTimeout(){throw new Error('should not schedule');}});
 assert.throws(()=>report.download([project()],{exportedAt}),/save failed/);assert.equal(anchor.removed,true);assert.deepEqual(revoked,['blob:failed']);
});

test('filename has no path separators and invalid export inputs fail before download',()=>{
 assert.equal(api().filename(exportedAt),'FL-适配进展-20261029-040506Z.html');assert.throws(()=>api().buildReport([]),/暂无可导出的项目/);assert.throws(()=>api().buildReport([project()],{exportedAt:'invalid'}),/导出时间无效/);
});


test('chosen timezone preserves distinct export and observation times while filenames remain UTC',()=>{
 const report=api(),html=report.buildReport([project()],{exportedAt,timeZone:'Asia/Shanghai'});
 assert.match(html,/导出时间 2026-10-29 12:05:06 Asia\/Shanghai/);
 assert.match(html,/采集时间 2026-10-28 11:04:05 Asia\/Shanghai/);
 assert.equal(report.filename(exportedAt),'FL-适配进展-20261029-040506Z.html');
 const utc=report.buildReport([project()],{exportedAt});
 assert.match(utc,/导出时间 2026-10-29 04:05:06 UTC/);
 assert.doesNotMatch(utc,/Asia\/Shanghai/);
 assert.throws(()=>report.buildReport([project()],{exportedAt,timeZone:'invalid'}),/导出时区无效/);
});
