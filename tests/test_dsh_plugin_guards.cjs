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

test('setup counts as finished only when nothing is left to type',async()=>{
  // The panel hides the setup entry on `complete`, so this decides whether a
  // step can still be reached. The live machine can only ever show the
  // all-done state, which is the one branch that hides the entry.
  const vm=require('node:vm');
  const a=host.indexOf('  async function setupState(');
  const b=host.indexOf('\n  }',a)+4;
  assert(a>0&&b>a,'cannot locate setupState');
  const body=host.slice(a,b);
  const run=(preflight,credentials)=>{
    const context={
      deployPreflight:()=>preflight,
      credentialState:async()=>{if(credentials==='throw')throw new Error('dashboard down');return credentials;},
    };
    vm.createContext(context);
    vm.runInContext(body+'\nthis.go=setupState;',context);
    return context.go(null);
  };
  const ready={ready:true,checks:[{id:'repo',ok:true},{id:'configure-script',ok:true},{id:'configured',ok:true}],projects:[{id:'vllm'}]};
  const unready={ready:false,checks:[{id:'repo',ok:false},{id:'configure-script',ok:false},{id:'configured',ok:false}],projects:[{id:'vllm'}]};
  const done={claude:{available:true,configured:true},machineSource:'jumpserver',jumpserver:{enabled:true,authenticated:true}};

  // Objects built inside the VM have that realm's prototype, so `strict`
  // deep equality rejects them; compare the fields instead.
  const shape=async(...args)=>{const r=await run(...args);return {complete:r.complete,outstanding:[...r.outstanding]};};

  assert.deepEqual(await shape(ready,done),{complete:true,outstanding:[]});
  assert.deepEqual((await shape(unready,done)).outstanding,['repo','configure-script','configured']);

  // A running dashboard with no agent key cannot start a task, so it is not
  // finished — this is the case that made credentials part of the decision.
  assert.deepEqual((await shape(ready,{...done,claude:{available:true,configured:false}})).outstanding,['claude-key']);
  assert.deepEqual((await shape(ready,{...done,jumpserver:{enabled:true,authenticated:false}})).outstanding,['jumpserver-password']);

  // Typing a key does not install a missing interpreter package, so an
  // unavailable agent must not keep the entry alive forever.
  assert.equal((await run(ready,{...done,claude:{available:false,configured:false}})).complete,true);
  // SSH-sourced machines never need a bastion login.
  assert.equal((await run(ready,{...done,machineSource:'ssh',jumpserver:{enabled:true,authenticated:false}})).complete,true);
  // Credential readiness needs the dashboard; if it cannot be asked, the
  // deployment checks alone decide rather than reporting a false need.
  assert.equal((await run(ready,'throw')).complete,true);
});

test('a finished setup is still reachable',()=>{
  // Hiding the entry must not strand a deployment that breaks later.
  assert(/setup && !setup\.complete/.test(client),'the entry is conditional');
  assert(/setup && setup\.complete && selected !== 'deploy'/.test(client),'a way back exists');
  assert(client.includes('deployRailDone'),'the way back is labelled');
  // Staying on a view whose entry just disappeared would leave the user on an
  // unreachable screen, so selection moves off it.
  // Anchor on the updater itself. `} catch (failure)` occurs earlier in the
  // file, so slicing to it scanned a region that does not contain this logic
  // and the assertion passed on the wrong text.
  const open=client.indexOf('setSelected(current => {');
  assert(open>0,'cannot locate the selection updater');
  const sel=client.slice(open,client.indexOf('\n          })',open));
  // Checking that both strings merely appear somewhere in this region is too
  // weak: `if (current === 'deploy') return current` satisfies it while
  // stranding the user. The escape has to be inside the deploy branch.
  const branch=sel.slice(sel.indexOf("if (current === 'deploy')"));
  assert(branch,'the finished setup view is special-cased');
  assert.equal(/^if \(current === 'deploy'\) return current/.test(branch),false,
    'a finished setup view must not keep the selection');
  assert(/outstanding\.complete/.test(branch.slice(0,240)),
    'the deploy branch itself consults the setup state');
});
