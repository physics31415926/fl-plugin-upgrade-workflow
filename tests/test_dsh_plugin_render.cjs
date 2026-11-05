// Render the new components to a static tree and assert on what they show.
// Verifies branch logic without a browser: components receive their data as
// props and useEffect is a no-op, so no request is ever issued.
const assert = require('node:assert/strict')
const fs = require('node:fs')
const path = require('node:path')
const test = require('node:test')
const vm = require('node:vm')

const src = fs.readFileSync(path.join(__dirname, '..', 'integrations/dsh-plugin/client.js'), 'utf8')

/** A React stand-in: records the tree instead of rendering it. */
function createElement(type, props, ...children) {
  return {
    type,
    props: props || {},
    children: children.flat(Infinity).filter(c => c !== null && c !== undefined && c !== false),
  }
}
let hookValues = []
let hookIndex = 0
const React = {
  createElement,
  Fragment: 'Fragment',
  // Hooks return the seeded value and ignore updates: each render is one
  // deterministic pass over one state, which is what the assertions describe.
  useState(initial) {
    const i = hookIndex++
    if (!(i in hookValues)) hookValues[i] = typeof initial === 'function' ? initial() : initial
    return [hookValues[i], value => { hookValues[i] = typeof value === 'function' ? value(hookValues[i]) : value }]
  },
  useEffect() { /* an effect would fetch; the tests supply data directly */ },
  useCallback(fn) { return fn },
  useContext() { return { get: () => undefined, on: () => () => {}, effect: () => {} } },
  createContext() { return { Provider: 'Provider' } },
}

const sandbox = {
  console,
  window: {
    __ModuleLoader__: {
      load({ factory }) {
        factory(name => {
          if (name === 'react') return React
          throw new Error('unexpected require: ' + name)
        })
      },
    },
  },
  fetch: () => { throw new Error('a render test must not fetch') },
  document: { hidden: false },
  setTimeout, clearTimeout, setInterval, clearInterval,
  URLSearchParams, encodeURIComponent, JSON, Boolean, String, Number,
  Object, Array, Math, Date, Set, Map, RegExp, isNaN, parseInt, parseFloat, Promise, Error,
}
sandbox.globalThis = sandbox
vm.createContext(sandbox)
vm.runInContext(src, sandbox)
const probe = sandbox.__probe
if (!probe) throw new Error('no probe seam in client.js')

/** Flatten a tree to text, the way a reader would see it. */
function text(node) {
  if (node === null || node === undefined || node === false) return ''
  if (typeof node === 'string' || typeof node === 'number') return String(node)
  if (Array.isArray(node)) return node.map(text).join(' ')
  return [node.props && node.props.children, node.children].flat(Infinity).map(text).join(' ')
}
/** Every class name appearing anywhere in the tree. */
function classes(node, out = new Set()) {
  if (!node || typeof node !== 'object') return out
  if (Array.isArray(node)) { node.forEach(n => classes(n, out)); return out }
  const c = node.props && node.props.className
  if (typeof c === 'string') c.split(/\s+/).forEach(x => x && out.add(x))
  ;[node.props && node.props.children, node.children].flat(Infinity).forEach(n => classes(n, out))
  return out
}
/** Collect nodes by element type. */
function find(node, type, out = []) {
  if (!node || typeof node !== 'object') return out
  if (Array.isArray(node)) { node.forEach(n => find(n, type, out)); return out }
  if (node.type === type) out.push(node)
  ;[node.props && node.props.children, node.children].flat(Infinity).forEach(n => find(n, type, out))
  return out
}
function render(Component, props, state) {
  hookValues = state ? state.slice() : []
  hookIndex = 0
  return Component(props)
}

const t = probe.dictFor('en')
function check(label, condition, detail) {
  test(label, () => assert.ok(condition, detail))
}

const MACHINE = {
  id: 'jms-a', label: 'host-alpha', backend: 'cuda', role: 'compute',
  usable: true, hasWorkspace: true, backendVerified: true, configError: null,
}
const OPTIONS = {
  available: true, defaultBranch: '0.4.0-dev', backends: ['cuda'],
  machines: [MACHINE], policies: {}, takenPairs: [],
  providers: { codex: { available: true }, claude: { available: true }, flagscale: { available: false } },
  anyManualPossible: true,
}
const FORM = { profile: 'jms-a', branch: '0.4.0-dev', goal: '', provider: 'codex' }
// CreateTask hook order: open, options, form, busy, error, made.
const state = (over = {}) => [
  over.open !== undefined ? over.open : true,
  over.options !== undefined ? over.options : OPTIONS,
  over.form !== undefined ? over.form : FORM,
  over.busy || false,
  over.error !== undefined ? over.error : null,
  over.made !== undefined ? over.made : null,
]
const createTask = over => render(probe.CreateTask, { t, project: { id: 'vllm' }, onCreated() {} }, state(over))
const prepareButton = tree => find(tree, 'button').find(b => /Prepare task/.test(text(b)))

// --- CreateTask ---
{
  const tree = createTask({ open: false })
  check('collapsed by default shows one button', text(tree).includes('New task'))
  check('collapsed shows no form', !classes(tree).has('flup-select'))
}
{
  const tree = createTask()
  const body = text(tree)
  check('open form says it does not start the task', /does not start it/i.test(body))
  check('open form states the worktree and lease cost', /worktree/i.test(body) && /reserves this machine/i.test(body))
  const button = prepareButton(tree)
  check('prepare is enabled when nothing blocks', Boolean(button) && !button.props.disabled)
  const providers = find(tree, 'option').map(o => o.props.value)
  check('flagscale is never offered', !providers.includes('flagscale'), providers.join(','))
}
{
  // The live deployment: every profile lacks a remote workspace.
  const machine = { ...MACHINE, hasWorkspace: false }
  const tree = createTask({ options: { ...OPTIONS, machines: [machine], anyManualPossible: false } })
  const body = text(tree)
  check('no-workspace deployment is explained up front', /no machine profile/i.test(body))
  check('a machine without a workspace is explained', /no remote workspace recorded/i.test(body))
  check('prepare is disabled without a workspace', prepareButton(tree).props.disabled === true)
}
{
  // A monitor policy on the SAME branch must block.
  const policies = { cuda: { mode: 'monitor', branch: '0.4.0-dev', reason: 'PR exists', pr: 484, pr_url: 'https://example.invalid/pr/484' } }
  const tree = createTask({ options: { ...OPTIONS, policies } })
  check('monitor policy blocks and explains', /monitor-only/i.test(text(tree)))
  check('prepare is disabled under a monitor policy', prepareButton(tree).props.disabled === true)
  const link = find(tree, 'a')[0]
  check('the blocking PR is linked', Boolean(link) && link.props.href === 'https://example.invalid/pr/484')
  check('the PR link is safe', Boolean(link) && link.props.rel === 'noreferrer noopener' && link.props.target === '_blank')
}
{
  // The SAME policy on a DIFFERENT branch must not block.
  const policies = { cuda: { mode: 'monitor', branch: '0.5.0-dev', reason: 'PR exists', pr: 1, pr_url: null } }
  const tree = createTask({ options: { ...OPTIONS, policies } })
  check('a policy on another branch does not block', !prepareButton(tree).props.disabled)
}
{
  const takenPairs = [{ backend: 'cuda', branch: '0.4.0-dev' }]
  const tree = createTask({ options: { ...OPTIONS, takenPairs } })
  check('an existing pair blocks and explains', /already has a task on this branch/i.test(text(tree)))
  check('prepare is disabled for a taken pair', prepareButton(tree).props.disabled === true)
}
{
  const tree = createTask({ options: { ...OPTIONS, machines: [{ ...MACHINE, backendVerified: false }] } })
  check('an inferred backend is flagged', /inferred from its name/i.test(text(tree)))
}
{
  const tree = createTask({ form: { ...FORM, profile: '' } })
  check('prepare is disabled with no machine picked', prepareButton(tree).props.disabled === true)
}
{
  const tree = createTask({ made: 'abc' })
  check('a prepared task says it is not started', /not started/i.test(text(tree)))
}

// --- TaskTable ownership filter ---
const task = (id, over = {}) => ({
  id, title: 'T-' + id, branch: '0.4.0-dev', backend: 'cuda', status: 'completed',
  controllable: true, created_here: false, ...over,
})
// TaskTable takes the plain array; ProjectView unwraps the upstream envelope.
const table = (tasks, mineOnly) => render(probe.TaskTable, {
  tasks, t, onOpen() {}, branch: null, onBranch() {}, mineOnly, onMineOnly() {},
}, [])
{
  const tree = table([task('a'), task('b'), task('c', { created_here: true })], true)
  const body = text(tree)
  check('mine-only counts only created-here tasks', /created here \(1\)/.test(body), body.slice(0, 160))
  check('mine-only still offers the full list', /all in the dashboard \(3\)/.test(body))
  check('mine-only hides other tasks', !body.includes('T-a') && !body.includes('T-b'))
  check('mine-only shows its own task', body.includes('T-c'))
}
{
  const tree = table([task('a'), task('b')], true)
  check('empty mine-only explains the first-run state', /No tasks have been created from this panel/i.test(text(tree)))
}
{
  const body = text(table([task('a'), task('b')], false))
  check('all-scope shows dashboard tasks', body.includes('T-a') && body.includes('T-b'))
}

// --- CredentialView ---
// Hook order: state, draft, remember, busy, error, saved.
const credState = (over = {}) => [
  over.state !== undefined ? over.state : null,
  over.draft || { claude: '', jumpserver: '' },
  over.remember || false,
  over.busy || '',
  over.error !== undefined ? over.error : null,
  over.saved || '',
]
const cred = over => render(probe.CredentialView, { t, project: { id: 'vllm' } }, credState(over))
{
  const tree = cred({
    state: {
      codex: { available: true, configured: false, authenticated: true, error: null },
      claude: { available: true, configured: false, authenticated: false, error: null },
      flagscale: { available: false, configured: true, authenticated: false, error: 'missing package' },
      machineSource: 'jumpserver',
      jumpserver: { enabled: true, authenticated: false, passwordSaved: false, canRemember: true, assetCount: 0, error: null },
    },
  })
  const body = text(tree)
  check('github has no input, only guidance', /gh auth login/.test(body) && /no field here on purpose/i.test(body))
  check('ssh keys are explained as never entered', /never entered anywhere/i.test(body))
  check('the missing dashboard auth is disclosed', /no authentication of its own/i.test(body))
  const inputs = find(tree, 'input').filter(i => i.props.type === 'password')
  check('exactly two secret fields are offered', inputs.length === 2, 'got ' + inputs.length)
  check('secret fields disable autofill', inputs.every(i => i.props.autoComplete === 'off'))
  check('secret fields are never pre-filled', inputs.every(i => i.props.value === ''))
  check('claude storage is described', /DPAPI/.test(body))
}
{
  const tree = cred({
    state: {
      codex: { available: true, configured: false, authenticated: true, error: null },
      claude: { available: true, configured: true, authenticated: false, error: null },
      flagscale: null,
      machineSource: 'jumpserver',
      jumpserver: { enabled: true, authenticated: true, passwordSaved: true, canRemember: true, assetCount: 57, error: null },
    },
  })
  const inputs = find(tree, 'input').filter(i => i.props.type === 'password')
  check('a configured credential offers no field', inputs.length === 0, 'got ' + inputs.length)
  check('a configured claude key is confirmed', /Claude key configured/.test(text(tree)))
}
{
  // When machines do not come from JumpServer, its password is irrelevant.
  const tree = cred({
    state: {
      codex: null,
      claude: { available: true, configured: true, authenticated: false, error: null },
      flagscale: null,
      machineSource: 'static',
      jumpserver: { enabled: false, authenticated: false, passwordSaved: false, canRemember: false, assetCount: 0, error: null },
    },
  })
  check('jumpserver is hidden when it is not the machine source', !/JumpServer password/.test(text(tree)))
}

// --- RepoCard ---
// Hook order: repo, busy, error.
const repoState = (over = {}) => [
  over.repo !== undefined ? over.repo : null,
  over.busy || false,
  over.error !== undefined ? over.error : null,
]
const repoCard = over => render(probe.RepoCard, { t, project: { id: 'vllm', label: 'vLLM-Plugin-FL' } }, repoState(over))
const REPO = {
  supported: true, project: 'vllm', label: 'vLLM-Plugin-FL', known: true,
  present: true, adopted: false, workspace: null, stale: false,
  name: 'vllm-plugin-FL', branch: '0.4.0-dev', remote: 'flagos-ai/vllm-plugin-FL',
}
{
  check('renders nothing before it loads', repoCard({ repo: null }) === null)
  check('renders nothing without sidebar support', repoCard({ repo: { supported: false } }) === null)
}
{
  const tree = repoCard({ repo: { ...REPO, known: false } })
  check('an unconfigured project says so', /no repository recorded/i.test(text(tree)))
}
{
  const tree = repoCard({ repo: REPO })
  const body = text(tree)
  check('a detected repo shows its directory name', body.includes('vllm-plugin-FL'))
  check('a detected repo shows its branch', body.includes('0.4.0-dev'))
  check('a detected repo offers adoption', /Add to sidebar/.test(body))
  check('a detected repo offers no conversation yet', !/Start a conversation/.test(body))
}
{
  const tree = repoCard({ repo: { ...REPO, present: false } })
  const body = text(tree)
  check('a missing repo is flagged', /not on disk yet/i.test(body))
  check('a missing repo shows a clone command instead of running one', /git clone https:\/\/github\.com\/flagos-ai\/vllm-plugin-FL\.git/.test(body))
  check('a missing repo cannot be adopted', !/Add to sidebar/.test(body))
}
{
  const tree = repoCard({ repo: { ...REPO, adopted: true, workspace: 'ws-1' } })
  const body = text(tree)
  check('an adopted repo confirms it is in the sidebar', /In the sidebar/.test(body))
  check('an adopted repo offers a conversation', /Start a conversation here/.test(body))
}
{
  // A path must never reach the browser, so no card may print one.
  const tree = repoCard({ repo: { ...REPO, adopted: true, workspace: 'ws-1' } })
  check('no filesystem path is rendered', !/(^|[^A-Za-z])[A-Za-z]:[\\/]/.test(text(tree)))
}
{
  // The seeded prompt must carry the project and branch, with no placeholder left.
  const prompt = t.repoSessionPrompt.replace('{project}', 'vLLM-Plugin-FL').replace('{branch}', '0.4.0-dev')
  check('the seeded prompt substitutes both fields', !/\{project\}|\{branch\}/.test(prompt))
  check('the seeded prompt names the project and branch', prompt.includes('vLLM-Plugin-FL') && prompt.includes('0.4.0-dev'))
}

// --- invariants ---
check('no browser storage anywhere', !/localStorage|sessionStorage|document\.cookie/.test(src))
check('no secret in any client URL', !/\$\{BASE\}[^`]*(secret|api_key|password)/.test(src))
