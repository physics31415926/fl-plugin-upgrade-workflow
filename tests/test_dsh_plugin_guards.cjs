const assert=require('node:assert/strict');
const fs=require('node:fs');
const path=require('node:path');
const test=require('node:test');
const root=path.join(__dirname,'..','integrations/dsh-plugin');
const host=fs.readFileSync(path.join(root,'index.js'),'utf8');
const client=fs.readFileSync(path.join(root,'client.js'),'utf8');

test('only read-only dashboard endpoints are proxied',()=>{
  const block=host.slice(host.indexOf('const READ_PATHS'),host.indexOf('const TRANSCRIPT_PATH'));
  const allowed=[...block.matchAll(/'(\/api\/[a-z0-9/-]+)'/g)].map(m=>m[1]);
  assert(allowed.includes('/api/projects-status'));
  assert(allowed.includes('/api/adaptation-tasks'));
  // These carry SSH aliases, identity-file paths, machine profiles, worktrees
  // and credential locations, and the upstream has no redaction toggle.
  for(const denied of ['/api/codex-sessions','/api/status','/api/jumpserver','/api/access-shares','/api/agent-settings','/api/logs'])
    assert.equal(allowed.includes(denied),false,denied+' must not be proxied');
});

test('the agent tool cannot create tasks or reach credentials',()=>{
  const block=host.slice(host.indexOf("name: 'fl_upgrade_dashboard'"));
  const actions=[...block.slice(0,block.indexOf('handler')).matchAll(/'(status|start|stop|read|deploy|task-message|adopt-workspace)'/g)].map(m=>m[1]);
  assert.deepEqual([...new Set(actions)].sort(),['read','start','status','stop']);
});

test('a task is driven only when upstream says every gate is clear',()=>{
  const block=host.slice(host.indexOf("if (action === 'task-message')"),host.indexOf("if (action === 'refresh')"));
  // The route resolves the task upstream; a stale panel must not drive a task
  // that upstream would refuse, and each refusal has its own reason.
  assert(block.includes('await listTasks(projectId)'),'resolves the task upstream');
  assert(/\/\^\[0-9a-f\]\{32\}\$\//.test(block),'rejects a non-session id');
  assert(block.includes("task_policy?.mode === 'monitor'"),'honours monitor-only backends');
  assert(block.includes("execution_owner === 'codex_desktop'"),'honours a desktop handoff');
  assert(block.includes('task.controllable !== true'),'honours the controllable flag');
  // Upstream reads only `text` and caps it at 12000 characters.
  assert(block.includes('JSON.stringify({ text: message })'),'sends the field upstream reads');
  assert(block.includes('12_000'),'matches the upstream length cap');
  // `thread_id` is the agent CLI thread UUID (36 chars) and is valid in no
  // dashboard URL, so the upstream path must be built from the task id.
  assert(/codex-sessions\/\$\{taskId\}\/message/.test(block),'addresses the session by task id');
  assert.equal(/\$\{[^}]*thread_id[^}]*\}/.test(block),false,'must not address by thread id');
});

test('workspace adoption never takes a path from the request',()=>{
  const block=host.slice(host.indexOf("if (action === 'adopt-workspace')"),host.indexOf("if (action === 'start')"));
  assert(block.includes('await listTasks(projectId)'),'resolves the task upstream');
  assert(block.includes('task.worktree'),'uses the path upstream reports');
  // Accepting a path from the body would turn this into a way to register an
  // arbitrary directory through the browser.
  assert.equal(/parsed\??\.(worktree|path|dir)/.test(block),false,'must not read a path from the body');
  // Registration only: creating or pruning a worktree belongs to the dashboard.
  for(const verb of ['rmdir','unlink','rename','mkdir','worktree add','worktree remove'])
    assert.equal(block.includes(verb),false,'must not '+verb);
});

test('the panel never renders an upstream field that carries machine identity',()=>{
  for(const field of ['ssh_alias','ssh_connection','identity_file','remote_workspace','rollout_path','model_paths','jumpserver_asset_id','reported_provenance'])
    assert.equal(client.includes(field),false,'client must not render '+field);
  // `worktree` reaches the Host, which resolves it, but never the browser:
  // the word may appear in UI copy and comments, a field read may not.
  assert.equal(/\.worktree\b|\[['"]worktree['"]\]/.test(client),false,'client must not read a worktree path');
  assert(host.includes('task.worktree'),'the Host still resolves it');
  // The summarizer decides what leaves the dashboard process, so the panel
  // cannot receive a field the projection never emits.
  const summary=host.slice(host.indexOf('title: taskLabel(task)'),host.indexOf('pending_count:'));
  assert.equal(/worktree|ssh_|remote_workspace|rollout_path/.test(summary),false,'projection must not emit machine identity');
});
