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
// GPU hosts as the device route reports them. Distinct from MACHINE above:
// that is the create-task machine profile, which carries configuration rather
// than live counters.
//
// Verdicts use the upstream vocabulary (free, partial, busy, residual,
// unknown, down); `accel` carries the per-metric card counts and the model
// name. Two backends, so the filter has something to separate.
const HOST = {
  id: 'jms-a', label: 'host-alpha', backend: 'cuda', verdict: 'free',
  stale: false, accel: { card_name: 'NVIDIA A100-SXM4-40GB', total: 8, free: 4, busy: 4, unknown: 0, residual: 0 },
}
const FLEET = [
  HOST,
  {
    id: 'jms-b', label: 'host-beta', backend: 'cuda', verdict: 'busy',
    stale: false, accel: { card_name: 'NVIDIA A100-SXM4-40GB', total: 8, free: 0, busy: 8, unknown: 0, residual: 0 },
  },
  {
    id: 'jms-c', label: 'host-gamma', backend: 'ascend', verdict: 'partial',
    stale: true, accel: { card_name: 'Ascend 910B', total: 16, free: 2, busy: 12, unknown: 2, residual: 0 },
  },
]
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

// --- status dot, placeholders and pending labels ---
{
  // The pulsing class is what tells a reader which rows are live, so it has to
  // appear for executing work and stay off everything that has settled.
  for (const status of ['running', 'starting', 'interrupting']) {
    const on = classes(render(probe.Dot, { status }))
    check('a ' + status + ' task pulses', on.has('flup-dot-live'))
  }
  for (const status of ['completed', 'failed', 'interrupted', 'prepared', 'unknown', '']) {
    const off = classes(render(probe.Dot, { status }))
    check('a ' + (status || 'blank') + ' task does not pulse', !off.has('flup-dot-live'))
  }
}
{
  // The halo is drawn with currentColor, so `color` has to be set alongside
  // the dot's own background or the ring renders in the inherited text colour.
  const live = render(probe.Dot, { status: 'running' })
  check('a live dot sets color for its halo', typeof live.props.style.color === 'string')
  check('a live dot still sets its own fill', typeof live.props.style.background === 'string')
  const idle = render(probe.Dot, { status: 'completed' })
  check('a settled dot sets no halo colour', idle.props.style.color === undefined)
}
{
  // A dot is decoration beside a word that already states the status.
  const dot = render(probe.Dot, { status: 'running' })
  check('a dot is hidden from assistive technology', dot.props['aria-hidden'] === true)
}
{
  const skeleton = render(probe.Skeleton, { rows: 4, label: t.loading })
  const bars = []
  const walk = n => {
    if (!n || typeof n !== 'object') return
    if (Array.isArray(n)) return n.forEach(walk)
    if (n.props && n.props.className === 'flup-skel') bars.push(n)
    ;[n.props && n.props.children, n.children].flat(Infinity).forEach(walk)
  }
  walk(skeleton)
  check('a skeleton renders the requested number of bars', bars.length === 4)
  check('a skeleton announces itself as a status', skeleton.props.role === 'status')
  check('a skeleton carries a readable label', skeleton.props['aria-label'] === t.loading)
  check('skeleton bars vary in width', new Set(bars.map(b => b.props.style.width)).size > 1)
  // Guard the degenerate case: a caller asking for nothing still shows
  // something, otherwise the block collapses and the layout jumps.
  check('a skeleton never renders zero bars', (() => {
    const none = []
    const w = n => {
      if (!n || typeof n !== 'object') return
      if (Array.isArray(n)) return n.forEach(w)
      if (n.props && n.props.className === 'flup-skel') none.push(n)
      ;[n.props && n.props.children, n.children].flat(Infinity).forEach(w)
    }
    w(render(probe.Skeleton, { rows: 0 }))
    return none.length === 1
  })())
}
{
  // A spinner replaces nothing: the label stays, so the button keeps saying
  // what it is doing rather than becoming a bare animation.
  const pending = render(probe.Pending, { label: 'Deploying' })
  check('a pending button keeps its label', text(pending).includes('Deploying'))
  check('a pending button shows a spinner', classes(pending).has('flup-spin'))
}

// --- devices are independent of project and branch ---
{
  // The fleet is shared: the dashboard returns a byte-identical payload for
  // every project and its route takes no branch. So the device view must not
  // sit beside the per-branch views, and must not carry a branch picker.
  const tree = render(probe.DeviceView, { t, project: { id: 'vllm' }, running: true }, [
    { host_count: 2, updated_at: '2026-01-01T00:00:00Z', hosts: [HOST] },
    null, false, null,
  ])
  const body = text(tree)
  check('the device view states why it has no branch', /not tied to a project or a branch/.test(body))
  check('the device view has no branch selector', find(tree, 'select').length === 0)
  check('the device view offers its own refresh', body.includes(t.refreshDevices))
  // The stand-in records child components without rendering them, so the
  // table is asserted by its presence here and by its own render below.
  check('the device view mounts the host table', find(tree, probe.DeviceTable).length === 1)
}
// Device view state order: devices, error, busy, backend.
const FLEET_BODY = { host_count: 3, updated_at: '2026-01-01T00:00:00Z', hosts: FLEET }
{
  // Counters are shown as ratios: "4 free" means nothing without the total it
  // is drawn from, so a bare count is a regression.
  const tree = render(probe.DeviceView, { t, project: { id: 'vllm' }, running: true }, [FLEET_BODY, null, false, null])
  const body = text(tree)
  // One of three hosts has verdict free and is not stale.
  check('idle hosts are shown against the total', /1\/3/.test(body))
  // free 4+0+2 of total 8+8+16.
  check('free cards are shown against the total', /6\/32/.test(body))
  check('unreadable cards are called out', body.includes(t.statUnknown) && /\b2\b/.test(body))
  check('stale hosts are called out', body.includes(t.statStale))
}
{
  // The upstream summary describes the whole fleet, so it must not be used:
  // it would report 57 hosts above a table of three.
  const tree = render(probe.DeviceView, { t, project: { id: 'vllm' }, running: true }, [
    { ...FLEET_BODY, summary: { hosts: 57, online: 57, free_hosts: 22, total_cards: 576, free_cards: 293, unknown_cards: 99 } },
    null, false, null,
  ])
  const body = text(tree)
  check('the headline ignores the fleet-wide summary', !/57|293\/576/.test(body))
  check('the headline counts only the hosts on screen', /1\/3/.test(body) && /6\/32/.test(body))
}
{
  // Zero unreadable cards is the normal case; a permanent "unknown 0" would
  // train the eye to skip the line, so it is omitted entirely.
  const tree = render(probe.DeviceView, { t, project: { id: 'vllm' }, running: true }, [
    { host_count: 1, hosts: [HOST] }, null, false, null,
  ])
  const body = text(tree)
  check('no unreadable-card notice when there are none', !body.includes(t.statUnknown))
  check('no stale notice when nothing is stale', !body.includes(t.statStale))
}
{
  // An empty or not-yet-populated payload must not render "undefined"/"NaN".
  for (const [label, payload] of [['empty', { host_count: 0, hosts: [] }], ['bare', {}]]) {
    const tree = render(probe.DeviceView, { t, project: { id: 'vllm' }, running: true }, [payload, null, false, null])
    check('a ' + label + ' payload renders no undefined text', !/undefined|NaN/.test(text(tree)))
  }
}

// --- the backend filter ---
{
  const tree = render(probe.DeviceView, { t, project: { id: 'vllm' }, running: true }, [FLEET_BODY, null, false, null])
  const chips = find(tree, 'button').filter(b => b.props.className && b.props.className.includes('flup-chip'))
  const labels = chips.map(c => text(c).trim())
  check('the filter offers one chip per backend plus all', chips.length === 3)
  check('the all-hardware chip carries the fleet size', labels.includes(`${t.allBackends} (3)`))
  // Ordered by host count, so the vendors with most machines come first.
  check('backend chips carry their host counts', labels[1] === 'cuda (2)' && labels[2] === 'ascend (1)')
  check('all hardware is selected by default', chips[0].props['aria-pressed'] === true)
  check('no backend chip is pressed by default', chips.slice(1).every(c => c.props['aria-pressed'] === false))
}
{
  // Selecting a backend narrows the table and the headline together.
  const tree = render(probe.DeviceView, { t, project: { id: 'vllm' }, running: true }, [FLEET_BODY, null, false, 'ascend'])
  const body = text(tree)
  const table = find(tree, probe.DeviceTable)[0]
  check('a selected backend narrows the host list', table.props.hosts.length === 1)
  check('a selected backend keeps only its own hosts', table.props.hosts[0].backend === 'ascend')
  // The one ascend host is partial and stale, so no host counts as idle.
  check('the headline follows the selection', /0\/1/.test(body) && /2\/16/.test(body))
  const chips = find(tree, 'button').filter(b => b.props.className && b.props.className.includes('flup-chip'))
  check('the selected chip is pressed', chips.find(c => text(c).includes('ascend')).props['aria-pressed'] === true)
  check('the all chip is not pressed while filtering', chips[0].props['aria-pressed'] === false)
}
{
  // A filter pinned to a backend that has left the fleet would show an empty
  // table with no way to tell why, so it falls back to showing everything.
  const tree = render(probe.DeviceView, { t, project: { id: 'vllm' }, running: true }, [FLEET_BODY, null, false, 'gone'])
  const table = find(tree, probe.DeviceTable)[0]
  check('an unknown backend falls back to the whole fleet', table.props.hosts.length === 3)
  const chips = find(tree, 'button').filter(b => b.props.className && b.props.className.includes('flup-chip'))
  check('the all chip is pressed after a fallback', chips[0].props['aria-pressed'] === true)
}
{
  // A single-vendor fleet has nothing to filter, so the row is not drawn.
  const tree = render(probe.DeviceView, { t, project: { id: 'vllm' }, running: true }, [
    { host_count: 1, hosts: [HOST] }, null, false, null,
  ])
  const chips = find(tree, 'button').filter(b => b.props.className && b.props.className.includes('flup-chip'))
  check('one backend draws no filter row', chips.length === 0)
}
{
  const table = render(probe.DeviceTable, { hosts: FLEET, t })
  const body = text(table)
  check('the host table names each host', /host-alpha/.test(body) && /host-gamma/.test(body))
  check('the host table shows free cards against the total', /4\/8/.test(body))
  check('the host table states each verdict', /free/.test(body) && /partial/.test(body))
  check('the host table names the card model', /A100-SXM4-40GB/.test(body))
  // Stale data looks identical to fresh data otherwise, and acting on a stale
  // free count is how two tasks land on one machine.
  check('the host table marks a stale host', body.includes(t.stale))
  check('an empty host list says so', text(render(probe.DeviceTable, { hosts: [], t })).includes(t.noDevices))
}
{
  // A down dashboard must say so rather than render an empty fleet, which
  // would read as "no machines" instead of "not connected".
  const tree = render(probe.DeviceView, { t, project: { id: 'vllm' }, running: false })
  check('a stopped dashboard explains the empty fleet', text(tree).includes(t.dashboardDown))
}
{
  // Devices used to be a third tab next to backends and tasks. Guard the
  // separation so it is not quietly reintroduced.
  const project = render(probe.ProjectView, { project: { id: 'vllm', label: 'vLLM' }, running: true, t }, [
    '0.4.0-dev', ['0.4.0-dev'], { backends: [] }, [], null, false, 'backends', null, null, true,
  ])
  const labels = find(project, 'button').map(b => text(b).trim())
  check('the project view no longer tabs to devices', !labels.includes(t.tabDevices))
  check('the project view keeps its backend tab', labels.includes(t.tabBackends))
  check('the project view keeps its task tab', labels.includes(t.tabTasks))
  check('the project view no longer refreshes devices', !labels.some(l => l.includes(t.refreshDevices)))
  check('the project view keeps its branch refresh', labels.some(l => l.includes(t.refreshBranch)))
}
{
  // The per-branch load must not fetch the fleet: that request is the
  // expensive one and it would repeat on every branch change. Slice from
  // ProjectView specifically, since the device view has a `load` of its own.
  const from = src.indexOf('function ProjectView(')
  const load = src.slice(src.indexOf('const load = React.useCallback', from), src.indexOf('// Poll while mounted', from))
  check('the branch load exists to be checked', load.length > 0 && load.length < 2000)
  check('the branch load does not fetch devices', !load.includes('/api/devices'))
  check('the branch load still fetches tasks', load.includes('/api/adaptation-tasks'))
}

// --- invariants ---
check('no browser storage anywhere', !/localStorage|sessionStorage|document\.cookie/.test(src))
check('no secret in any client URL', !/\$\{BASE\}[^`]*(secret|api_key|password)/.test(src))
