// Guards for the panel's write actions: creating a task, handing over a
// credential, and registering a project repository. Each test reads the
// shipped plugin source, so a change that loosens a guard fails here.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const test = require('node:test');
const root = path.join(__dirname, '..', 'integrations/dsh-plugin');
const host = fs.readFileSync(path.join(root, 'index.js'), 'utf8');
const client = fs.readFileSync(path.join(root, 'client.js'), 'utf8');

/** The body of one route handler, by its action name. */
function route(action, next) {
  const start = host.indexOf(`action === '${action}'`);
  assert.notEqual(start, -1, `no ${action} route`);
  const end = next ? host.indexOf(`action === '${next}'`, start) : host.length;
  return host.slice(start, end === -1 ? host.length : end);
}

/**
 * The same text with comments removed.
 *
 * These guards describe what the code DOES, and this plugin's comments
 * deliberately name the hazards they avoid ("never retry", "not its path"), so
 * a prose-matching assertion would read a warning as a violation.
 */
function code(text) {
  return text
    .replace(/\/\*[\s\S]*?\*\//g, '')
    .split('\n')
    .filter(line => !line.trim().startsWith('//'))
    .map(line => line.replace(/\s\/\/.*$/, ''))
    .join('\n');
}

test('creating a task never starts it', () => {
  const block = route('create-task', 'task-message');
  const body = block.match(/JSON\.stringify\(\{[^}]*\}/)[0];
  // `start: true` makes one request both create the task AND launch an agent
  // turn, which spends model tokens and runs commands on a remote GPU host.
  assert.equal(/\bstart\b/.test(body), false, 'must not forward start');
  // Upstream validates a supplied connection against the saved profile and
  // forcing it triggers a live SSH preflight, so the profile is the only input.
  assert.equal(/ssh_connection|identity_file|remote_workspace/.test(body), false,
    'must not forward machine identity');
  assert.equal(/automatic/.test(body), false, 'must not request the SSH-probing path');
  // Exactly the four fields the panel collects.
  assert(/profile_id/.test(body) && /branch/.test(body) && /provider/.test(body) && /goal/.test(body));
});

test('creating a task validates every input before it reaches upstream', () => {
  const block = route('create-task', 'task-message');
  assert(block.includes("sendJson(res, 400, { error: 'project and profile are required' })"));
  assert(/branch is not a valid git ref/.test(block), 'rejects a malformed ref');
  assert(/6000/.test(block), 'caps the goal at the upstream limit');
  // All three implemented providers are accepted at the syntax gate; a
  // provider cannot be changed after creation, so only these three are valid.
  assert(/provider must be codex, claude or flagscale/.test(block));
  // flagscale is explicitly accepted, not merely tolerated by a loose check:
  // the syntax gate must name all three it rejects outside of.
  assert(/provider !== 'codex' && provider !== 'claude' && provider !== 'flagscale'/.test(block),
    'the gate rejects only values outside the three real providers');
  /*
   * Availability is then checked against upstream, so an unavailable provider
   * is refused with a 409 rather than forwarded and stranded. This replaces
   * the old blanket exclusion of flagscale, which is a real provider.
   */
  assert(/agent-providers/.test(block), 'probes provider availability');
  assert(/row\.available !== true/.test(block), 'refuses an unavailable provider');
  assert(/is not available on this deployment/.test(block), 'says why it refused');
});

test('setting a remote workspace writes only that field, and never a bad path', () => {
  const block = route('set-workspace', 'create-task');
  // The path is the user's; the plugin never invents a remote directory.
  assert(/remote_workspace: workspace/.test(block), 'forwards the path the user gave');
  assert(/\/api\/codex-machines/.test(block), 'writes through the machine-profile route');
  // The body is exactly the id and the one field — nothing else is disturbed
  // on the merged profile.
  const bodyCall = block.match(/JSON\.stringify\(\{ id: profileId, remote_workspace: workspace \}\)/);
  assert(bodyCall, 'sends only the id and remote_workspace, no extra fields');
  // An absolute POSIX path with no shell metacharacters: it ends up on an SSH
  // command line, and a relative path is ambiguous on the host.
  assert(/workspace\.startsWith\('\/'\)/.test(block), 'requires an absolute path');
  assert(/\["'`\$\\\\\]/.test(block) || /metacharacters/.test(block), 'rejects shell metacharacters');
  // Confirmed from the saved profile, and the path is never echoed back.
  const reply = block.slice(block.indexOf('sendJson(res, 200'));
  assert.equal(/workspace\s*[,}]/.test(reply), false, 'must not echo the path');
});

test('a timed-out create is never retried on its own', () => {
  const block = route('create-task', 'task-message');
  // Upstream offers no idempotency key, so a retry could create two tasks,
  // each taking a machine lease.
  assert.equal(/retry|attempt\s*\+\+|for\s*\(let\s+attempt|while\s*\(/.test(code(block)), false,
    'must not retry a create');
  assert(/could not be reached/.test(block), 'reports the failure instead');
});

test('created task ids are recorded so the panel can show only its own', () => {
  // Upstream records no creator: there is no created_by, and unknown request
  // fields are dropped, so no tag can be smuggled in. The returned id is the
  // only durable handle.
  assert(host.includes('rememberCreated'), 'records the id');
  assert(host.includes('readCreated'), 'reads the record back');
  assert(/created_here/.test(host), 'tags the projection');
  assert(/created_here/.test(client), 'the panel filters on the tag');
  // The record holds ids only.
  const store = code(host.slice(host.indexOf('const readCreated'), host.indexOf('const credentialState')));
  assert.equal(/password|api_key|secret|ssh|identity/.test(store), false,
    'the record must hold ids only');
});

test('only credentials with a real store are accepted', () => {
  const block = route('credential', 'task-message');
  assert(block.includes('kind must be claude, jumpserver or github'));
  /*
   * SSH private keys remain refused, and this is not an oversight to be
   * relaxed later. The dashboard references key files by path and never reads
   * their contents, so accepting key material would both break that invariant
   * and hand this plugin a secret it has no way to protect. A GitHub token is
   * different in kind: the Harness credential service already provides
   * encrypted storage for exactly this, so the token has a real home.
   */
  assert.equal(/'ssh'|private_key|passphrase/.test(block), false,
    'must not accept ssh key material');
});

test('a github token is stored by the Harness, never sent to the dashboard', () => {
  const block = route('credential', 'task-message');
  // Bound to the github branch: from its `if` to the start of the non-github
  // path (the project-required check that the claude/jumpserver code shares),
  // so a 409 in another branch is not read as this one's.
  const ghStart = block.indexOf("if (kind === 'github')");
  const github = block.slice(ghStart, block.indexOf("const projectId = scalar(parsed?.project)", ghStart));
  /*
   * The dashboard reads the token from its process environment and its control
   * server logs every request line to a plaintext file, so forwarding the
   * token over HTTP would write it to disk.
   */
  assert(github.includes('credentials.set('), 'stores through the credential service');
  const toUpstream = github.slice(0, github.indexOf('sendJson(res, 200'));
  assert.equal(/callUpstream/.test(toUpstream), false, 'never forwards the token upstream');
  /*
   * A token already present in the environment must NOT be refused. The
   * credential service's `set` always writes its own managed source and a
   * stored value overrides ambient fallbacks, so an environment token is no
   * reason to reject the write — refusing it (an earlier 409) was the bug
   * behind "cannot configure the token".
   */
  assert.equal(/status.*409|statusCode.*409|sendJson\(res, 409/.test(code(github)), false,
    'must not refuse a save because a token exists in the environment');
  assert.equal(/info\.writable/.test(code(github)), false,
    'must not gate the write on a writable flag');
  // A running dashboard keeps its original environment.
  assert(github.includes('restartRequired'), 'reports that a restart is needed');
  // The reply must carry readiness for the project the caller named.
  assert.equal(/credentialState\('vllm'\)|\|\| 'vllm'/.test(github), false,
    'must not invent a project id');
});

test('the token reaches the dashboard only through its environment', () => {
  // There are two spawns now (the dashboard, and the one-time repo clone); the
  // token belongs to the dashboard spawn, which is the one carrying githubEnv.
  const dashSpawn = host.indexOf('const githubEnv = await resolveGithubToken()')
  assert.notEqual(dashSpawn, -1, 'the dashboard spawn resolves a token')
  // End at the spawn's own failure handler (the `spawn failed` catch), not the
  // earlier executable-resolve catch between githubEnv and the spawn.
  const spawn = host.slice(dashSpawn, host.indexOf('spawn failed', dashSpawn))
  assert(spawn.includes('githubEnv'), 'passes the resolved token at spawn')
  assert(spawn.includes('env:'), 'uses the environment channel')
  // Omitted when absent: an empty variable would mask a `gh` login, which
  // upstream prefers over the token.
  assert(/githubEnv \?/.test(spawn), 'omits the variable when no token is held')
  // The clone spawn must never carry the token.
  const clone = host.slice(host.indexOf('async function cloneDashboardIfMissing'), host.indexOf('function snapshot'))
  assert.equal(/githubEnv|GITHUB_TOKEN|env:/.test(clone), false, 'the clone spawn carries no token')
  // The resolver must not retain or log the value.
  const resolver = host.slice(host.indexOf('async function resolveGithubToken'), host.indexOf('async function githubTokenState'))
  assert.equal(/console\.|logger\.|writeFile/.test(resolver), false, 'never logs or writes the token')
})

test('token presence is reported without the value', () => {
  const describe = host.slice(host.indexOf('async function githubTokenState'), host.indexOf('const credentialState'));
  assert(describe.includes('credentials.describe('), 'asks for a description, not the value');
  assert.equal(/\.resolve\(/.test(describe), false, 'the readiness probe never resolves the secret');
  for (const leak of ['value', 'secret', 'token:']) {
    assert.equal(new RegExp(`${leak}\\s*:\\s*(value|resolved|secret)`).test(describe), false,
      `must not return the ${leak}`);
  }
});

test('a credential travels in a request body, never in a URL', () => {
  const block = route('credential', 'task-message');
  // The upstream control server overrides log_message for its share server but
  // not for the control server, so request LINES reach a plaintext log file.
  // A secret in a query string or path segment would be written to disk.
  assert(/JSON\.stringify\(target\.payload\)/.test(block), 'sends the secret as a body');
  const urls = [...block.matchAll(/`[^`\n]*\?[^`\n]*`/g)].map(m => m[0]);
  for (const url of urls) {
    assert.equal(/secret|password|api_key|token/.test(url), false,
      'a secret must not appear in ' + url);
  }
  // The readiness route is a GET and so must carry no secret at all.
  const read = route('credentials', 'deploy-preflight');
  assert.equal(/secret|password|api_key/.test(read.split('\n').filter(l => !l.trim().startsWith('*') && !l.trim().startsWith('//')).join('\n')), false,
    'the readiness GET must not handle a secret');
});

test('a credential is never persisted or echoed by the plugin', () => {
  const block = route('credential', 'task-message');
  // The dashboard's own sink seals it with DPAPI in a git-ignored directory
  // and reads it back as a boolean; a second copy here would only add risk.
  assert.equal(/writeFile|appendFile|writeFileSync/.test(block), false,
    'must not write a secret to disk');
  // The success reply carries readiness booleans, not the value. Both write
  // paths confirm from the read route rather than trusting their own write.
  assert(/credentialState\(projectId\)/.test(block), 'confirms from the read route');
  assert(/credentialState\(githubProject\)/.test(block), 'the token path confirms too');
  /*
   * No success reply carries the value. Checked per reply rather than from the
   * first one to the end of the route: there are two write paths now, and a
   * slice starting at the first `sendJson(res, 200` runs on into the other
   * path's upstream payload, where `secret` legitimately appears.
   */
  for (const reply of block.matchAll(/sendJson\(res, 200, \{[\s\S]{0,400}?\}\)/g)) {
    assert.equal(/\bsecret\b/.test(reply[0]), false, `must not echo the secret: ${reply[0].slice(0, 80)}`);
  }
  assert(block.includes('sendJson(res, 200'), 'the route has a success reply to check');
  // The browser must not keep one either.
  assert.equal(/localStorage|sessionStorage|document\.cookie|indexedDB/.test(client), false,
    'the panel must not persist a secret');
  assert(/type: 'password'/.test(client), 'secret fields are masked');
  assert(/autoComplete: 'off'/.test(client), 'secret fields opt out of autofill');
});

test('credential readiness reports booleans, not values', () => {
  const block = host.slice(host.indexOf('const credentialState'), host.indexOf('const disposeRoute'));
  // The source routes carry a base URL, a username, an identity-file path and
  // the whole asset inventory. None of it is needed to say what is missing.
  for (const field of ['base_url', 'username', 'identity_file', 'assets', 'api_key', 'password']) {
    assert.equal(new RegExp(`\\b${field}\\b`).test(block), false,
      'readiness must not forward ' + field);
  }
  assert(/=== true/.test(block), 'coerces to booleans');
  assert(/redactText/.test(block), 'redacts the free-form error text');
});

test('the machine list reaches the browser without machine identity', () => {
  const block = route('task-options', 'deploy-preflight');
  // `?view=cards` is a size optimisation, not redaction: the source payload
  // still carries ssh_connection, identity_file, worktree and rollout_path.
  const emitted = block.slice(block.indexOf('.map(row => ({'));
  for (const field of ['ssh_connection', 'identity_file', 'ssh_alias', 'rollout_path', 'model_paths', 'worktree']) {
    assert.equal(emitted.includes(field), false, 'must not emit ' + field);
  }
  // A remote workspace decides whether creation can work, so its presence is
  // reported as a boolean rather than its value.
  assert(/hasWorkspace: typeof row\.remote_workspace/.test(emitted));
  assert(/redactText\(row\.label/.test(emitted), 'an operator label can name a host');
});

test('every creation blocker is resolved by the host, not the browser', () => {
  const block = route('task-options', 'deploy-preflight');
  // A stale panel must not offer a combination upstream would refuse.
  assert(/backend_task_policies/.test(block), 'reports monitor policies');
  assert(/takenPairs/.test(block), 'reports the one-per-(backend,branch) guard');
  assert(/anyManualPossible/.test(block), 'reports whether creation is possible at all');
  // A policy only bites on its own branch, so the branch must travel with it.
  assert(/branch: scalar\(policy\.branch\)/.test(block));
  // And the panel must actually gate on all of them.
  assert(/policyBlocks/.test(client) && /pairTaken/.test(client) && /needsWorkspace/.test(client));
  assert(/disabled: busy \|\| blocked/.test(client), 'the button honours the gates');
});

test('a project repository is detected, never typed in', () => {
  const resolver = host.slice(host.indexOf('function resolveProjectRepo'), host.indexOf('* Read an upstream dashboard response'));
  // The dashboard's own config names each checkout, so the path is derived.
  assert(/monitor_config/.test(resolver), 'reads the configured monitor file');
  assert(/monitor\?\.repo/.test(resolver), 'reads the configured repo path');
  // A relative repo path is relative to the config that named it.
  assert(/isAbsolute/.test(resolver), 'resolves a relative path correctly');

  const adopt = route('adopt-repo', 'workspaces');
  // Accepting a path from the browser would make this a way to register an
  // arbitrary directory.
  assert.equal(/parsed\??\.(path|dir|repo|worktree)/.test(adopt), false,
    'must not read a path from the body');
  assert(/resolveProjectRepo\(config, projectId\)/.test(adopt), 'derives the path');
  // Registration only: the directory must already exist.
  assert(/not on disk yet/.test(adopt), 'refuses a missing directory');
  for (const verb of ['mkdir', 'clone', 'rmdir', 'unlink', 'rename']) {
    assert.equal(adopt.includes(verb), false, 'must not ' + verb);
  }
});

test('a project workspace is named without its branch', () => {
  // A workspace is a directory, and the branch checked out in it changes with
  // any `git checkout`. A title carrying the branch survives that change and
  // starts lying, so the branch belongs on the conversations inside instead.
  const resolver = host.slice(host.indexOf('function resolveProjectRepo'), host.indexOf('* Read an upstream dashboard response'));
  assert(/title: entry\?\.label \|\| projectId/.test(resolver), 'the title is the project label alone');
  assert.equal(/title:.*\$\{branch\}/.test(resolver), false, 'the title must not interpolate the branch');

  // A workspace adopted before this change keeps its old title, because
  // `create` reuses a registration without retitling it.
  assert(/async function retitleBranchNamed/.test(host), 'old titles are corrected');
  const retitle = host.slice(host.indexOf('async function retitleBranchNamed'), host.indexOf('function taskLabel'));
  // Only the exact shape this plugin used to write is rewritten; a title the
  // user chose themselves must survive untouched.
  assert(/workspace\.title !== `\$\{resolved\.title\} · \$\{branch\}`/.test(retitle), 'matches only its own old shape');
  assert(/return$/m.test(retitle), 'leaves anything else alone');
  assert(/catch/.test(retitle), 'a failed rename does not fail the adoption');
});

test('a task worktree keeps the branch in its title', () => {
  // The two cases differ for a reason: a task worktree is created for one
  // branch on one backend and pruned when the task ends, so the branch is part
  // of what that directory permanently is.
  const label = host.slice(host.indexOf('function taskLabel'), host.indexOf('/** Reduce one backend row'));
  assert(/\$\{backend\} · \$\{branch\}/.test(label), 'names backend and branch');
  const adopt = route('adopt-workspace', 'forget-workspace');
  assert(/taskLabel\(task\)/.test(adopt), 'the worktree title comes from the task');
  // The distinction must be written down, or it reads as an inconsistency.
  // Block-comment prose wraps and each continuation line starts with a `*`,
  // so drop those markers before collapsing whitespace.
  const prose = adopt.replace(/^\s*\*/gm, ' ').replace(/\s+/g, ' ');
  assert(prose.includes('branch is part of what the directory permanently is'),
    'the difference from a project checkout is explained');
});

test('a conversation is named after the branch', () => {
  // The version belongs on the conversation: it is about one branch at one
  // moment, where the workspace outlives any single branch.
  const start = client.slice(client.indexOf('const startSession = async'), client.indexOf('const forget ='));
  assert(/connectWorkspace/.test(client), 'resolves the session id first');
  assert(/renameSession\(ctx, sessionId, branch\)/.test(client), 'renames it to the branch');

  const rename = client.slice(client.indexOf('async function renameSession'), client.indexOf('Shorten a bilingual vendor name'));
  // A held reference must be released even when the rename throws.
  assert(/sessions\.using\(/.test(rename), 'releases the reference');
  assert(/source: 'gateway'/.test(rename), 'uses a declared reference source');
  // Naming is cosmetic; a failure must not stop the conversation opening.
  assert(/catch \{/.test(rename), 'a failed rename is not fatal');
  assert(/slice\(0, 200\)/.test(rename), 'bounds the title length');
});

test('the only clone is the dashboard repo into the default directory', () => {
  /*
   * The plugin clones exactly one thing: its own dashboard repository, and
   * only into the default directory it owns. The monitored *project* repos
   * (vllm-plugin-FL, sglang-plugin-FL) are still never cloned — those stay a
   * command the user runs, shown as copyable text — and an explicit repoPath
   * is never cloned into.
   */
  const clone = host.slice(host.indexOf('async function cloneDashboardIfMissing'), host.indexOf('function snapshot'));
  assert(/'clone'/.test(clone), 'the dashboard clone exists');
  // It targets the pinned dashboard URL and the default path, not caller input.
  assert(/DASHBOARD_REPO_URL, config\.repoPath/.test(clone), 'clones the pinned repo to the default path');
  // Guarded: off switch, explicit path, and an existing checkout each skip it.
  assert(/if \(!config\.autoClone\) return/.test(clone), 'honours the off switch');
  assert(/if \(config\.repoPathExplicit\) return/.test(clone), 'never clones over an explicit path');
  assert(/scripts', 'flflow\.py'\)\)\) return/.test(clone), 'never re-clones an existing checkout');
  // The monitored project repos are never cloned by the plugin.
  assert.equal(/vllm-plugin-FL\.git|sglang-plugin-FL\.git/.test(host), false,
    'must not clone a monitored project repo');
  // The panel still shows the project-repo clone command as copyable text only.
  assert(/git clone https:\/\/github\.com\//.test(client), 'offers the project clone command');
  const spawns = [...client.matchAll(/host\.call\([^)]*\)/g)].map(m => m[0]);
  for (const call of spawns) {
    assert.equal(/clone/.test(call), false, 'the browser never asks the host to clone');
  }
});

test('a repository path never reaches the browser', () => {
  const repos = route('repos', 'adopt-repo');
  // The directory name is enough to recognise a checkout; its location is not
  // the browser's business.
  assert(/basename\(resolved\.path\)/.test(repos), 'reports the name only');
  const emitted = code(repos.slice(repos.indexOf('repos.push({')));
  assert.equal(/path: |\bpath,/.test(emitted), false, 'must not emit a path');
  // The workspace id is an opaque uuid, which the Client needs because its
  // workspace service cannot enumerate workspaces.
  assert(/workspace: described\.workspace/.test(emitted), 'reports the opaque id');
});

test('a stale sidebar entry is distinguished from one that was never made', () => {
  const describe = host.slice(host.indexOf('const describeWorkspace'), host.indexOf('const credentialState') > 0 ? host.indexOf('if (req.method === \'GET\' && action === \'workspaces\'') : host.length);
  // resolveByPath canonicalises through realpath BEFORE lookup, so a pruned
  // directory rejects instead of returning undefined, and a dead entry read as
  // "never adopted" while it sat in the sidebar opening nothing.
  assert.equal(/resolveByPath/.test(describe.split('*/').pop()), false,
    'must not probe by path');
  assert(/registry\.list\(\)/.test(describe), 'asks the registry for its own list');
  assert(/missing-dir/.test(describe), 'reads each entity\'s own status');
  assert(/stale:/.test(describe), 'labels a stale registration');

  const forget = route('forget-workspace', 'repos');
  // Clearing a live registration would remove a working sidebar entry.
  assert(/still exists; remove it from the sidebar instead/.test(forget));
  assert(/registry\.delete/.test(forget), 'drops only the registration');
  for (const verb of ['rmdir', 'unlink', 'rm(', 'rmSync']) {
    assert.equal(forget.includes(verb), false, 'must not ' + verb);
  }
});

test('a seeded conversation carries context, not machine detail', () => {
  const dict = client.slice(client.indexOf('repoSessionPrompt'));
  const prompt = dict.slice(0, dict.indexOf('\n'));
  // The prompt is a locale string filled with the project and branch only.
  assert(/\{project\}/.test(prompt) && /\{branch\}/.test(prompt));
  assert.equal(/[A-Za-z]:\\|\/home\/|ssh|password/.test(prompt), false,
    'the prompt must not carry machine detail');
  assert(/startSession\(repo\.workspace/.test(client), 'opens the detected workspace');
});

test('colour is never the only signal', () => {
  // Every state carries its text, so the panel stays readable without colour.
  for (const key of ['workspaceStaleTag', 'taskDesktopTag', 'createMonitorOnly', 'repoAdopted']) {
    assert(client.includes(key), 'missing state label ' + key);
  }
  // Colours come from theme tokens so they follow the active theme.
  const css = client.slice(client.indexOf('const CSS = `'), client.indexOf('\n`', client.indexOf('const CSS = `')));
  const literals = [...css.matchAll(/:\s*(#[0-9a-fA-F]{3,8}|rgba?\()/g)].map(m => m[1]);
  assert(literals.length <= 1, 'unexpected hard-coded colours: ' + literals.join(','));
});
