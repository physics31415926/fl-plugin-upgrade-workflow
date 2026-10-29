/* Standalone, offline reports. Only explicit display fields cross this boundary. */
(() => {
  'use strict';
  const STATUS = {merged:'已合入',open:'待合入',draft:'草稿',closed:'已关闭',pending:'待验收',unknown:'待确认',passed:'通过',recorded_pass:'通过',failed:'未通过',running:'运行中',missing:'暂无结果',stale:'证据过期',partial:'部分通过',cancelled:'已取消',skipped:'跳过',reference:'PR 记录'};
  const SOURCE = {pr_description:'PR 作者报告',local_record:'本地验收记录',current_gate:'逐项验收结果',pr_reference:'PR 记录'};
  const TEST_CONTEXT = /\b(?:tests?|testing|validation|acceptance|verification|gate|e2e|smoke|benchmark|pytest|offline|concurrent|inference|mtp)\b|验收|验证|测试|并发|离线|推理|运行结果/i;
  const CI_ONLY = /^\W*(?:ci|github\s+actions?|workflow|流水线)\b/i;
  const COMMAND = /^(?:python(?:\d(?:\.\d+)?)?|pytest|bash|sh|pwsh|powershell|ssh|tmux|curl|wget|pip|git|rm|export)\s/i;
  const array = value => Array.isArray(value) ? value : [];
  const object = value => value && typeof value === 'object' && !Array.isArray(value) ? value : {};
  const escape = value => String(value ?? '').replace(/[&<>"']/g, char => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[char]));
  function plain(value, limit=1200) {
    if(typeof value !== 'string')return '';
    let text=value.slice(0,8000);
    // Decode character references before redaction so encoding cannot hide a key.
    for(let count=0;count<2;count++)text=text.replace(/&(#x[\da-f]+|#\d+|amp|lt|gt|quot|apos);/gi, (all,code) => {
      if(code[0] !== '#')return ({amp:'&',lt:'<',gt:'>',quot:'"',apos:"'"})[code.toLowerCase()];
      const point=parseInt(code.slice(/^#x/i.test(code)?2:1),/^#x/i.test(code)?16:10);
      return point>=0&&point<=0x10ffff?String.fromCodePoint(point):all;
    });
    text=text.replace(/!?\[([^\]]*)\]\([^)]*\)/g,'$1')
      .replace(/`([^`]+)`/g,(_all,code)=>COMMAND.test(code.trim())?'测试命令（见原 PR）':code)
      .replace(/(?:https?|ssh):\/\/[^\s<>]+/gi,'[地址已隐藏]')
      .replace(/\b(?:api[_-]?key|access[_-]?token|password|passwd|secret|authorization|bearer)\b\s*(?:[:=]\s*|\s+)["']?[^\s;,"']+/gi,'[凭据已隐藏]')
      .replace(/\b(?:sk-[A-Za-z0-9_-]{8,}|gh[pousr]_[A-Za-z0-9]{8,}|github_pat_[A-Za-z0-9_]{8,}|hf_[A-Za-z0-9]{8,}|AKIA[A-Z0-9]{12,}|xox[baprs]-[A-Za-z0-9-]{8,})\b/g,'[凭据已隐藏]')
      .replace(/\b(?:[A-Z0-9]+_)*(?:PRIVATE|SECRET)(?:_[A-Z0-9]+)+\b/g,'[私有信息已隐藏]')
      .replace(/\b(?:\d{1,3}\.){3}\d{1,3}\b|\[[0-9A-Fa-f:]*:[0-9A-Fa-f:]+\]|(?<![\w:])(?:[0-9A-Fa-f]{0,4}:){2,}[0-9A-Fa-f:.]{0,39}(?!\w)/g,'[地址已隐藏]')
      .replace(/[A-Za-z]:[\\/][^\s;,<>\]\)"']*|\\\\[^\s;,<>\]\)"']+|(?<![\w.])(?:~?\/[A-Za-z0-9_.-]+){2,}[^\s;,<>\]\)"']*/g,'[路径已隐藏]')
      .replace(/(?<!\w)~\/[^\s;,<>\]\)"']+/g,'[路径已隐藏]')
      .replace(/[\x00-\x1f\x7f]/g,' ').replace(/\s+/g,' ').trim();
    if(COMMAND.test(text))return '测试命令（见原 PR）';
    return text.length>limit?text.slice(0,limit-1)+'…':text;
  }
  const text = (value,limit) => escape(plain(value,limit));
  function status(value) {
    const normalized={success:'passed',failure:'failed',error:'failed',queued:'running',in_progress:'running'}[value]||value;
    return Object.hasOwn(STATUS,normalized)?normalized:'unknown';
  }
  function pill(value) {const key=status(value);return `<span class="pill ${key}">${STATUS[key]}</span>`;}
  function githubUrl(value, kind) {
    if(typeof value !== 'string'||value.length>1500||/[\x00-\x20\\]/.test(value))return '';
    try {
      const url=new URL(value);
      if(url.protocol!=='https:'||url.hostname!=='github.com'||url.port||url.username||url.password||url.search||url.hash)return '';
      const suffix=kind==='pr'?'pull/[1-9][0-9]*':kind==='commit'?'commit/[a-fA-F0-9]{7,64}':'(?:actions/runs/[1-9][0-9]*(?:/attempts/[1-9][0-9]*)?(?:/job/[1-9][0-9]*)?|runs/[1-9][0-9]*)';
      return new RegExp('^/[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+/'+suffix+'/?$').test(url.pathname)?url.href:'';
    }catch(_){return '';}
  }
  function link(value,label,kind) {const url=githubUrl(value,kind);return url?`<a href="${escape(url)}" target="_blank" rel="noopener noreferrer">${text(label)}</a>`:text(label);}
  function instant(value, fallback) {
    const date=value === undefined?new Date(fallback===undefined?Date.now():fallback):new Date(value);
    return Number.isFinite(date.getTime())?date:null;
  }
  function timeFormatter(timeZone) {
    let formatter;
    try {
      if(typeof timeZone!=='string'||!timeZone)throw new Error('Invalid timezone');
      formatter=new Intl.DateTimeFormat('en-CA',{timeZone,year:'numeric',month:'2-digit',day:'2-digit',hour:'2-digit',minute:'2-digit',second:'2-digit',hourCycle:'h23'});
    }catch(_){throw new Error('导出时区无效');}
    const zone=formatter.resolvedOptions().timeZone;
    return value => {
      if(!value)return '尚未采集';
      const date=instant(value);if(!date)return '时间未知';
      const parts=Object.fromEntries(formatter.formatToParts(date).map(part=>[part.type,part.value]));
      return `${parts.year}-${parts.month}-${parts.day} ${parts.hour}:${parts.minute}:${parts.second} ${zone}`;
    };
  }
  function exportedDate(value) {const date=instant(value);if(!date)throw new Error('导出时间无效');return date;}
  function filename(exportedAt) {return 'FL-适配进展-'+exportedDate(exportedAt).toISOString().replace(/[-:]/g,'').replace('T','-').slice(0,15)+'Z.html';}
  function supplementalTables(result) {
    let blocks=array(result.acceptance_tables),active=false;
    if(!blocks.length) {
      blocks=[];
      for(const block of array(result.description_blocks).slice(0,4000)) {
        if(!block||typeof block!=='object')continue;
        if(block.type==='heading')active=TEST_CONTEXT.test(String(block.text||''))&&!CI_ONLY.test(String(block.text||''));
        else if(active&&block.type==='table')blocks.push(block);
      }
    }
    const tables=[];let budget=65536;
    for(const block of blocks) {
      if(block?.type!=='table'||budget<=0)continue;
      const rows=[];
      for(const cells of array(block.rows).slice(0,200)) {
        if(!Array.isArray(cells)||budget<=0)continue;
        const row=[];
        for(const value of cells.slice(0,12)) {
          if(budget<=0)break;
          const clean=plain(value,Math.min(1200,budget));budget-=clean.length;row.push(clean);
        }
        rows.push(row);
      }
      if(rows.length>1)tables.push(rows);
    }
    return tables;
  }
  function fallbackRows(result) {
    const rows=[];
    for(const table of supplementalTables(result)) {
      const headers=table[0],resultIndex=headers.findIndex(cell=>/^(?:results?|status|outcome|结果|状态)(?:\b|\s|$)/i.test(cell));
      for(const values of table.slice(1)) {
        if(resultIndex>=0)rows.push({label:values.filter((_v,i)=>i!==resultIndex).map((v,i)=>v).join(' · ')||'验收项',result:values[resultIndex]||'待确认'});
        else rows.push({label:values[0]||'验收项',result:values.slice(1).map((value,index)=>(headers[index+1]?headers[index+1]+'：':'')+value).join('；')||'待确认'});
      }
    }
    return rows;
  }
  function acceptanceRows(result) {
    return array(result.rows).filter(row=>row&&typeof row==='object'&&!Array.isArray(row)).slice(0,100).map(row=>({label:plain(row.label,160)||'验收项',configuration:plain(row.configuration,300),result:plain(row.result,1200)||STATUS[status(row.status)]}));
  }
  function acceptanceDetails(backend,id) {
    const result=object(backend.acceptance),rows=acceptanceRows(result),truncated=result.rows_truncated===true||(Number.isInteger(result.rows_total)&&result.rows_total>rows.length);
    const extra=(!rows.length||truncated)?fallbackRows(result):[];
    const content=rows.concat(extra);
    const pr=object(backend.pr),prUrl=githubUrl(result.pr_url,'pr')||githubUrl(pr.url,'pr');
    const number=Number.isInteger(result.pr_number)?result.pr_number:pr.number;
    const source=SOURCE[result.source]||'验收记录';
    const summary=plain(result.summary);
    const table=content.length?`<div class="scroll"><table class="acceptance"><thead><tr><th scope="col">验收项</th><th scope="col">结果</th></tr></thead><tbody>${content.map(row=>`<tr><td>${text(row.label)}${row.configuration?`<span class="configuration">${text(row.configuration)}</span>`:''}</td><td>${text(row.result)}</td></tr>`).join('')}</tbody></table>`:'<p class="muted">暂无可识别的逐项验收记录。</p>';
    const note=truncated?`<p class="notice">逐项记录已截断：摘要列出 ${rows.length} 条${Number.isInteger(result.rows_total)&&result.rows_total>rows.length?' / 共 '+result.rows_total+' 条':''}。${extra.length?'已附可分享的补充测试表格；':''}完整范围请查看原 PR。</p>`:'';
    return `<details open id="${id}"><summary>${text(backend.label||backend.id)} <span>${STATUS[status(result.status)]}</span></summary><div class="details-body">${summary?`<p>${escape(summary)}</p>`:''}<p class="muted">来源：${source}。测试版本与覆盖范围以原始记录为准。</p>${table}${note}${prUrl?`<p>${link(prUrl,Number.isInteger(number)?'查看原 PR #'+number:'查看原 PR','pr')}</p>`:''}</div></details>`;
  }
  function projectSection(value,index,formatTime) {
    const project=object(value),label=text(project.label||project.id||'监控项目'),branch=text(project.branch||'未选择分支');
    const rows=array(project.backends).filter(row=>row&&typeof row==='object'&&!Array.isArray(row)).slice(0,50).sort((a,b)=>(a.id!=='cuda')-(b.id!=='cuda')||String(a.label||a.id).localeCompare(String(b.label||b.id),'en',{sensitivity:'base'}));
    const name={vllm:'vLLM',sglang:'SGLang'}[project.engine]||plain(project.engine,80)||'Monitor';
    const heading=`<div class="project-title"><h2>${label}</h2><span class="engine">${text(name)}${project.version?' '+text(project.version,100):''}</span></div><p class="branch">目标分支 <strong>${branch}</strong></p>`;
    if(project.unavailable===true)return {card:`<article class="project">${heading}<p class="notice">此分支尚无采集结果。本报告未包含该项目的 PR 或验收数据。</p></article>`,details:''};
    const notice=project.hasError?'最近刷新未完成，以下为保留的历史采集结果。':project.refreshing?'导出时后台仍在采集，以下为当前已有结果。':project.stale?'以下为历史缓存，尚未完成最新状态核验。':'';
    const count=key=>rows.filter(row=>key==='merged'?row.integration==='merged':key==='ci'?status(row.ci)==='passed':['passed','recorded_pass'].includes(status(object(row.acceptance).status))).length;
    const metrics=`<div class="metrics">${[['merged','代码合入'],['ci','适配 CI 通过'],['acceptance','验收通过']].map(([key,label])=>`<div><span>${label}</span><strong>${count(key)}<small> / ${rows.length}</small></strong></div>`).join('')}</div>`;
    const body=rows.map((backend,rowIndex)=>{
      const pr=object(backend.pr),id=`acceptance-${index}-${rowIndex}`,result=object(backend.acceptance);
      return `<tr><td><strong>${text(backend.label||backend.id)}</strong>${Number.isInteger(pr.number)?`<span class="pr">${link(pr.url,'PR #'+pr.number,'pr')}</span>`:'<span class="muted pr">尚无关联 PR</span>'}</td><td>${pill(backend.integration)}</td><td>${pill(backend.ci)}${githubUrl(backend.ci_url,'ci')?`<span class="pr">${link(backend.ci_url,'查看 CI','ci')}</span>`:''}</td><td>${pill(result.status)}<a class="detail-link" href="#${id}">验收明细 ↓</a></td></tr>`;
    }).join('');
    return {card:`<article class="project">${heading}<div class="meta"><span>采集时间 ${escape(formatTime(project.observed_at))}</span><span>HEAD ${link(project.head_url,String(project.head||'未知').slice(0,12),'commit')}</span></div>${notice?`<p class="notice">${notice}</p>`:''}${metrics}<div class="scroll"><table class="overview"><thead><tr><th scope="col">Backend / PR</th><th scope="col">代码合入</th><th scope="col">适配 CI</th><th scope="col">验收结果</th></tr></thead><tbody>${body||'<tr><td colspan="4" class="muted">此分支暂无 Backend 数据。</td></tr>'}</tbody></table></div></article>`,details:rows.length?`<section class="details-group"><h2>${label} · 验收明细</h2><p class="muted">分支 ${branch} · 采集时间 ${escape(formatTime(project.observed_at))}</p>${rows.map((backend,rowIndex)=>acceptanceDetails(backend,`acceptance-${index}-${rowIndex}`)).join('')}</section>`:''};
  }
  function buildReport(projects,{exportedAt,timeZone='UTC'}={}) {
    if(!Array.isArray(projects)||!projects.length)throw new Error('暂无可导出的项目');
    const exported=exportedDate(exportedAt),formatTime=timeFormatter(timeZone),sections=projects.slice(0,20).map((project,index)=>projectSection(project,index,formatTime));
    return `<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1"><meta name="referrer" content="no-referrer"><meta http-equiv="Content-Security-Policy" content="default-src 'none'; style-src 'unsafe-inline'; base-uri 'none'; form-action 'none'"><title>FL 多项目适配进展 · 离线报告</title><style>
:root{color-scheme:light;font:14px/1.6 system-ui,-apple-system,"Segoe UI",sans-serif;color:#1d2a40;background:#f3f6fb}*{box-sizing:border-box}body{margin:0}main{max-width:1440px;margin:auto;padding:32px 28px 52px}header{padding:0 0 24px;border-bottom:1px solid #dce4ef;margin-bottom:24px}.eyebrow{font-size:12px;letter-spacing:.09em;color:#48648d;font-weight:700}h1{font-size:30px;letter-spacing:-.03em;line-height:1.3;margin:8px 0 12px}h2{font-size:21px;line-height:1.4;margin:0}p{margin:10px 0}.subtitle,.muted,.meta{color:#607088}.export-time{font-size:12px;color:#607088}.projects{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:22px;align-items:start}.project{min-width:0;background:white;border:1px solid #dce4ef;border-top:4px solid #315bdb;border-radius:12px;padding:22px}.project:nth-child(even){border-top-color:#087c88}.project-title{display:flex;align-items:center;justify-content:space-between;gap:12px}.engine{font-size:12px;color:#315bdb;background:#eef3ff;border-radius:5px;padding:4px 8px;white-space:nowrap}.branch{color:#607088;margin:13px 0 8px}.branch strong{color:#263b59;overflow-wrap:anywhere}.meta{display:flex;flex-wrap:wrap;gap:4px 14px;font-size:12px;overflow-wrap:anywhere}.notice{color:#79551c;background:#fff7e7;border:1px solid #eed8b0;padding:10px 12px;border-radius:7px;font-size:12px}.metrics{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:9px;margin:19px 0}.metrics div{background:#f6f8fc;border:1px solid #e3e9f2;border-radius:8px;padding:10px}.metrics span{display:block;color:#607088;font-size:12px}.metrics strong{display:block;font-size:23px}.metrics small{font-size:13px;font-weight:400;color:#607088}.scroll{overflow-x:auto}table{width:100%;border-collapse:collapse;text-align:left}th{color:#607088;font-size:12px;font-weight:650}td,th{padding:12px 6px;border-bottom:1px solid #e5ebf3;vertical-align:top;overflow-wrap:anywhere}.overview td:first-child{min-width:110px}.overview td{font-size:13px}.pr,.detail-link{display:block;font-size:12px;margin-top:5px}a{color:#315bdb;text-decoration:none}a:hover{text-decoration:underline}a:focus-visible,summary:focus-visible{outline:3px solid #95b5ff;outline-offset:3px}.pill{display:inline-block;border-radius:5px;padding:3px 7px;font-size:12px;font-weight:650;background:#eef2f7;color:#63718a;white-space:nowrap}.pill.passed,.pill.recorded_pass,.pill.merged{background:#e8f6ef;color:#117a55}.pill.failed{background:#fdecef;color:#b8324a}.pill.running,.pill.open,.pill.partial{background:#edf2ff;color:#315bdb}.pill.pending,.pill.stale,.pill.missing{background:#fff5e3;color:#946210}.details-group{margin-top:34px}.details-group>h2{font-size:23px}details{margin:14px 0;background:white;border:1px solid #dce4ef;border-radius:9px;overflow:hidden;scroll-margin-top:16px}summary{cursor:pointer;padding:14px 18px;font-weight:650;background:#f8fafd;overflow-wrap:anywhere}summary span{font-size:12px;font-weight:500;color:#607088;margin-left:10px}.details-body{padding:4px 18px 15px}.acceptance{table-layout:fixed}.acceptance th:first-child{width:48%}.acceptance td{white-space:pre-wrap;font-size:13px}.configuration{display:block;margin-top:4px;color:#607088;font-size:12px}footer{margin-top:28px;padding-top:16px;border-top:1px solid #dce4ef;color:#607088;font-size:12px}@media(max-width:900px){.projects{grid-template-columns:1fr}main{padding:24px 16px}.project{padding:18px}h1{font-size:26px}}@media(max-width:480px){main{padding:20px 10px}.project{padding:13px}.project-title{align-items:flex-start;flex-direction:column;gap:5px}.metrics{gap:5px}.metrics div{padding:8px}.metrics span{font-size:11px}.overview{min-width:440px}.details-body{padding:4px 12px 12px}summary{padding:12px}.acceptance td,.acceptance th{padding:9px 5px}}@media print{@page{size:A4 landscape;margin:12mm}:root{font-size:10px;background:white;color:#111}main{max-width:none;padding:0}.projects{gap:12px}.project{padding:12px;border-radius:0;box-shadow:none}h1{font-size:24px}h2{font-size:17px}.engine,.pill{print-color-adjust:exact;-webkit-print-color-adjust:exact}.metrics{margin:10px 0}.metrics strong{font-size:19px}.scroll{overflow:visible}.overview td{font-size:10px}.overview td:first-child{min-width:0}td,th{padding:7px 4px}.detail-link{display:none}.details-group{break-before:page}details{break-inside:auto;overflow:visible}summary{break-after:avoid}tr{break-inside:avoid}thead{display:table-header-group}.acceptance td{font-size:10px}.meta,.export-time,footer{font-size:10px}a{color:#111;text-decoration:none}}
</style></head><body><main><header><div class="eyebrow">FL · 适配进展快照</div><h1>多项目适配进展</h1><p class="subtitle">所选分支的 PR、适配 CI 与验收结果</p><p class="export-time">导出时间 ${escape(formatTime(exported.toISOString()))} · 离线报告</p></header><div class="projects">${sections.map(section=>section.card).join('')}</div>${sections.map(section=>section.details).join('')}<footer>本报告为导出时的状态快照，可离线查看，不会自动更新。采集时间与导出时间分别列出；PR 验收记录属于作者报告，测试版本与覆盖范围以原始记录为准。可通过浏览器“打印”另存为 PDF；导出默认展开验收表格，折叠后请展开再打印。</footer></main></body></html>`;
  }
  function download(projects,{exportedAt,timeZone='UTC'}={}) {
    const date=exportedDate(exportedAt),name=filename(date),html=buildReport(projects,{exportedAt:date,timeZone});
    const blob=new Blob([html],{type:'text/html;charset=utf-8'}),url=URL.createObjectURL(blob),anchor=document.createElement('a');
    anchor.href=url;anchor.download=name;anchor.hidden=true;
    try {document.body.append(anchor);anchor.click();}
    catch(error){URL.revokeObjectURL(url);throw error;}
    finally{anchor.remove();}
    setTimeout(()=>URL.revokeObjectURL(url),60000);
    return name;
  }
  globalThis.FLOverviewExport=Object.freeze({buildReport,filename,download});
})();
