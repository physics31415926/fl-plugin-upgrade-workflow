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
  // A provider is fixed at creation and cannot be changed, so an unavailable
  // one would strand the task. flagscale reports itself unavailable.
  assert(/provider must be codex or claude/.test(block));
  assert.equal(/'flagscale'/.test(block), false, 'must not accept flagscale');
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

test('only the two credentials the dashboard can store are accepted', () => {
  const block = route('credential', 'task-message');
  assert(block.includes("kind must be claude or jumpserver"));
  // A GitHub token has no upstream store: the dashboard reads it from its own
  // environment, so a form would mean the plugin inventing persistence.
  assert.equal(/'github'/.test(block), false, 'must not accept a github token');
  // SSH key files are referenced by path and never read, so accepting key
  // material would break an invariant the dashboard holds.
  assert.equal(/'ssh'|private_key|passphrase/.test(block), false,
    'must not accept ssh key material');
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
  // The success reply carries readiness booleans, not the value.
  assert(/credentialState\(projectId\)/.test(block), 'confirms from the read route');
  assert.equal(/secret\s*[,}]/.test(block.slice(block.indexOf('sendJson(res, 200'))), false,
    'must not echo the secret');
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

test('cloning is never performed by the plugin', () => {
  // Cloning writes to disk and reaches the network, so it stays a command the
  // user runs. Nothing in the plugin may spawn it.
  assert.equal(/'clone'|"clone"|git clone['"]/.test(host), false,
    'the host must not run a clone');
  // The panel shows the command as copyable text only.
  assert(/git clone https:\/\/github\.com\//.test(client), 'offers the command');
  const spawns = [...client.matchAll(/host\.call\([^)]*\)/g)].map(m => m[0]);
  for (const call of spawns) {
    assert.equal(/clone/.test(call), false, 'must not ask the host to clone');
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
