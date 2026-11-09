// Collection autostart, the collection health indicator, and the view cache.
//
// Three changes with one motive: the panel should show current data without
// being told to. Autostart brings the collectors up with the plugin, the
// header reports whether they are actually delivering, and the cache stops a
// view switch from throwing away what they delivered.
const assert = require('node:assert/strict')
const fs = require('node:fs')
const path = require('node:path')
const test = require('node:test')
const vm = require('node:vm')

const root = path.join(__dirname, '..', 'integrations/dsh-plugin')
const host = fs.readFileSync(path.join(root, 'index.js'), 'utf8')
const client = fs.readFileSync(path.join(root, 'client.js'), 'utf8')
const patch = fs.readFileSync(path.join(root, 'cordis.patch.yml'), 'utf8')

// --- collection starts with the plugin -------------------------------------

test('an absent autoStart setting still starts collection', () => {
  /*
   * The data is the reason the panel exists, so the process comes up with it.
   * Read the normalizer directly rather than asserting on source text: the
   * question is what an old profile (no such key) resolves to.
   */
  // Include defaultRepoPath too: normalizeConfig calls it for the repoPath
  // fallback, and it sits just above in the source.
  const body = host.slice(host.indexOf('function defaultRepoPath'), host.indexOf('function resolveLaunch'))
  // normalizeConfig now falls back to defaultRepoPath() when repoPath is unset;
  // that helper needs join + a DSH_HOME, so both are supplied to the sandbox.
  const sandbox = {
    resolvePath: p => p, Number, Boolean, String,
    join: (...parts) => parts.join('/'),
    homedir: () => '/home/test',
    process: { env: { DSH_HOME: '/home/test/.dsh' } },
  }
  vm.createContext(sandbox)
  vm.runInContext(`${body}\nglobalThis.__out = normalizeConfig`, sandbox)
  const normalize = sandbox.__out

  assert.equal(normalize({}).autoStart, true, 'a profile predating this default must gain it')
  assert.equal(normalize(undefined).autoStart, true, 'no config at all still collects')
  assert.equal(normalize({ autoStart: true }).autoStart, true)
  // An explicit false is a decision and has to survive.
  assert.equal(normalize({ autoStart: false }).autoStart, false, 'an explicit opt-out must be kept')
  // Only a real `false` disables it; a stray value must not silently stop
  // collection, which would be the failure this change exists to remove.
  assert.equal(normalize({ autoStart: 0 }).autoStart, true)
  assert.equal(normalize({ autoStart: null }).autoStart, true)
})

test('the shipped config declares the autostart default', () => {
  // The schema default is what a fresh install reads, so it has to agree with
  // the normalizer rather than leaving the two to drift.
  assert(/autoStart: true/.test(patch), 'cordis.patch.yml must default autoStart on')
})

// --- collection health is observed and reported ----------------------------

test('only collector-backed reads report collection health', () => {
  const block = host.slice(host.indexOf('const COLLECTOR_PATHS'), host.indexOf('const MAX_UPSTREAM_BYTES'))
  const mapped = [...block.matchAll(/'(\/api\/[a-z0-9/-]+)'/g)].map(m => m[1])
  assert(mapped.includes('/api/devices'), 'the fleet is the main thing that fails')
  assert(mapped.includes('/api/branch-status'))
  assert(mapped.includes('/api/adaptation-tasks'))
  /*
   * Configuration routes answer from disk and would report a collector that
   * never ran as healthy, which is exactly the false reassurance the
   * indicator is meant to replace.
   */
  for (const denied of ['/api/projects', '/api/capabilities', '/api/agent-providers']) {
    assert.equal(mapped.includes(denied), false, denied + ' does not come from a collection round')
  }
})

test('the host reports collection separately from the process', () => {
  const block = host.slice(host.indexOf('function snapshot'), host.indexOf('function syncAdoption'))
  assert(/collection: collectionSnapshot\(\)/.test(block), 'state must carry collection health')
  // `running` already answers the process question; the point of the new
  // field is that a live process can still be collecting nothing.
  assert(/running:/.test(block), 'the process flag stays')
})

test('a 200 carrying an error body counts as a failed round', () => {
  // The dashboard answers 200 with `{error}` when a round fails while the
  // server itself is fine, which is the common case for an SSH fan-out.
  const block = host.slice(host.indexOf("if (req.method === 'GET' && action === 'read')"))
  const read = block.slice(0, block.indexOf('res.writeHead'))
  assert(/if \(parsed && parsed\.error\) failed =/.test(read), 'an error body must mark the collector failing')
  assert(/upstream\.status >= 400/.test(read), 'an HTTP error marks it too')
  assert(/noteCollector\(path, \{ ok: false/.test(read), 'a transport failure marks it too')
})

test('a collection error is redacted like every other forwarded string', () => {
  // The error text comes from a collector and routinely embeds SSH targets
  // and absolute paths, so it cannot reach the browser raw.
  const calls = [...host.matchAll(/failed = redactText\(String\(parsed\.error\), \d+\)/g)]
  assert(calls.length >= 2, 'both the panel and agent read paths must redact it')
  const raw = /failed = String\(parsed\.error\)/.test(host)
  assert.equal(raw, false, 'an unredacted collector error must never be stored')
})

test('health survives one bad round without losing the last success', () => {
  const block = host.slice(host.indexOf('function noteCollector'), host.indexOf('function collectionSnapshot'))
  // Keeping `okAt` across a failure is what lets the tooltip say "failing,
  // last data 14:02" instead of pretending nothing ever worked.
  assert(/okAt: previous\?\.okAt \?\? null/.test(block), 'a failure must preserve the last success time')
})

test('the state report sends timestamps, not formatted dates', () => {
  const start = host.indexOf('function collectionSnapshot')
  // Bound the slice to the function body: a date formatted anywhere else in
  // the host is not this function's business.
  const block = host.slice(start, host.indexOf('\n  }', host.indexOf('return rows', start)))
  assert(block.includes('return rows'), 'the slice must cover the body')
  // Epoch milliseconds let the panel format against the reader's clock;
  // a preformatted string would bake this process's locale into the payload.
  assert.equal(/toLocaleString|toISOString/.test(block), false, 'formatting belongs to the reader')
  assert(/okAt: value\.okAt/.test(block), 'the raw timestamp is what travels')
})

// --- the indicator, rendered ------------------------------------------------

function loadClient() {
  let hookValues = []
  let hookIndex = 0
  const createElement = (type, props, ...children) => ({
    type,
    props: props || {},
    children: children.flat(Infinity).filter(c => c !== null && c !== undefined && c !== false),
  })
  const React = {
    createElement,
    Fragment: 'Fragment',
    useState(initial) {
      const i = hookIndex++
      if (!(i in hookValues)) hookValues[i] = typeof initial === 'function' ? initial() : initial
      return [hookValues[i], value => { hookValues[i] = typeof value === 'function' ? value(hookValues[i]) : value }]
    },
    useEffect() {},
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
  vm.runInContext(client, sandbox)
  return {
    probe: sandbox.__probe,
    render(Component, props, state) {
      hookValues = state ? state.slice() : []
      hookIndex = 0
      return Component(props)
    },
  }
}

/** Flatten a tree to the text a reader would see. */
function text(node) {
  if (node === null || node === undefined || node === false) return ''
  if (typeof node === 'string' || typeof node === 'number') return String(node)
  if (Array.isArray(node)) return node.map(text).join(' ')
  return [node.props && node.props.children, node.children].flat(Infinity).map(text).join(' ')
}
/** Collect every `title` attribute in the tree. */
function titles(node, out = []) {
  if (!node || typeof node !== 'object') return out
  if (Array.isArray(node)) { node.forEach(n => titles(n, out)); return out }
  if (node.props && typeof node.props.title === 'string') out.push(node.props.title)
  ;[node.props && node.props.children, node.children].flat(Infinity).forEach(n => titles(n, out))
  return out
}

const { probe, render } = loadClient()
const t = probe.dictFor('en')
const NOW = Date.now()

test('the verdict is pessimistic: one failing collector fails the badge', () => {
  /*
   * Branches collecting while every GPU host is unreachable is not a healthy
   * panel: half the data on screen is wrong and nothing says so. The badge
   * answers "is what I am looking at current", so any failure shows.
   */
  const verdict = probe.collectionVerdict({
    branches: { okAt: NOW, triedAt: NOW, error: null },
    devices: { okAt: NOW - 600_000, triedAt: NOW, error: 'ssh: all hosts unreachable' },
  })
  assert.equal(verdict.state, 'stale')
  assert.deepEqual(verdict.failing, ['devices'], 'the failing collector is named')
  assert.equal(verdict.error, 'ssh: all hosts unreachable')
  // The latest success across collectors, so the tooltip can date the data.
  assert.equal(verdict.okAt, NOW)
})

test('all collectors delivering reads as healthy', () => {
  const verdict = probe.collectionVerdict({
    branches: { okAt: NOW - 1000, triedAt: NOW, error: null },
    devices: { okAt: NOW, triedAt: NOW, error: null },
  })
  assert.equal(verdict.state, 'ok')
  assert.equal(verdict.okAt, NOW)
})

test('a collector nobody has read yet is not evidence of health', () => {
  // Reporting "collecting" before any round completed would be the same
  // false reassurance as reporting the process.
  assert.equal(probe.collectionVerdict({}).state, 'waiting')
  assert.equal(probe.collectionVerdict(null).state, 'waiting')
  assert.equal(
    probe.collectionVerdict({ devices: { okAt: null, triedAt: NOW, error: null } }).state,
    'waiting',
    'tried but never succeeded is not healthy',
  )
})

test('the badge states the verdict and dates the data in its tooltip', () => {
  const tree = render(probe.CollectionBadge, {
    t,
    running: true,
    collection: { devices: { okAt: NOW, triedAt: NOW, error: null } },
  })
  assert(text(tree).includes(t.collecting), 'says it is collecting')
  const tip = titles(tree).join(' ')
  assert(tip.includes(t.collectOkHint), 'the tooltip explains the badge')
  assert(tip.length > t.collectOkHint.length, 'and carries a timestamp')
})

test('a failing badge names the collector and surfaces the error', () => {
  const tree = render(probe.CollectionBadge, {
    t,
    running: true,
    collection: {
      devices: { okAt: NOW - 600_000, triedAt: NOW, error: 'ssh: all hosts unreachable' },
    },
  })
  assert(text(tree).includes(t.collectStale), 'the badge says collection is failing')
  const tip = titles(tree).join(' ')
  assert(tip.includes('ssh: all hosts unreachable'), 'the error is readable without leaving the panel')
  assert(tip.includes(t.collectDevices), 'and attributed to the device collector')
})

test('a failing badge still dates the last good data', () => {
  const tree = render(probe.CollectionBadge, {
    t,
    running: true,
    collection: { devices: { okAt: NOW - 600_000, triedAt: NOW, error: 'timeout' } },
  })
  // "Failing" alone leaves the reader unable to judge what is on screen.
  assert(titles(tree).join(' ').includes(t.collected), 'names when data last arrived')
})

test('a failing badge that never succeeded says so instead of implying a time', () => {
  const tree = render(probe.CollectionBadge, {
    t,
    running: true,
    collection: { devices: { okAt: null, triedAt: NOW, error: 'timeout' } },
  })
  assert(titles(tree).join(' ').includes(t.collectNeverOk))
})

test('the badge is absent while the dashboard is down', () => {
  // The process indicator beside it already explains an empty panel; a second
  // badge would only repeat it.
  const tree = render(probe.CollectionBadge, { t, running: false, collection: {} })
  assert.equal(tree, null)
})

test('the badge dot uses a colour the palette actually defines', () => {
  // A status word outside statusColor's lists falls through to idle grey,
  // which would paint a failing collector the same as an idle one.
  const dots = []
  const walk = node => {
    if (!node || typeof node !== 'object') return
    if (Array.isArray(node)) { node.forEach(walk); return }
    if (node.type === probe.Dot) dots.push(node.props.status)
    ;[node.props && node.props.children, node.children].flat(Infinity).forEach(walk)
  }
  for (const collection of [
    { devices: { okAt: NOW, triedAt: NOW, error: null } },
    { devices: { okAt: NOW, triedAt: NOW, error: 'boom' } },
    {},
  ]) {
    walk(render(probe.CollectionBadge, { t, running: true, collection }))
  }
  assert.deepEqual(dots, ['online', 'failed', 'unknown'])
  const palette = client.slice(client.indexOf('function statusColor'), client.indexOf('const LIVE_STATUSES'))
  for (const status of dots) {
    assert(palette.includes(`'${status}'`), `statusColor does not paint ${status}`)
  }
})

test('both locales carry every collection string', () => {
  const zh = probe.dictFor({ getSnapshot: () => ({ active: 'zh-CN' }) })
  const keys = Object.keys(t).filter(key => key.startsWith('collect'))
  assert(keys.length >= 8, 'the indicator has strings to translate')
  for (const key of keys) {
    assert(typeof zh[key] === 'string' && zh[key], `zh is missing ${key}`)
    assert.notEqual(zh[key], t[key], `${key} is untranslated`)
  }
})

// --- the view cache ---------------------------------------------------------

test('a cached view renders its previous data on the first pass', () => {
  /*
   * This is the flicker, reproduced: each view carries a React key so its
   * slide-in replays on every switch, which remounts it and discards its
   * state. Before the cache that meant a skeleton and a refetch even though
   * the previous data was still good.
   */
  probe.dropCache()
  // Drive the hook the way a view does: first mount seeds, a write records,
  // and the next mount (a remount) observes the written value.
  const first = render(() => probe.useCached('devices', 'devices', null), {}, [])
  assert.equal(first[0], null, 'nothing cached yet, so the view starts empty')
  first[1]({ hosts: ['host-alpha'] })
  const second = render(() => probe.useCached('devices', 'devices', null), {}, [])
  assert.deepEqual(second[0], { hosts: ['host-alpha'] }, 'the remount sees the previous data')
})

test('the cache is keyed per view and per project', () => {
  probe.dropCache()
  render(() => probe.useCached('project:vllm', 'status', null), {}, [])[1]('vllm-status')
  render(() => probe.useCached('project:sglang', 'status', null), {}, [])[1]('sglang-status')
  // Two projects sharing a slot would show one project's data under the
  // other's name, which is worse than the flicker.
  assert.equal(render(() => probe.useCached('project:vllm', 'status', null), {}, [])[0], 'vllm-status')
  assert.equal(render(() => probe.useCached('project:sglang', 'status', null), {}, [])[0], 'sglang-status')
  // Fields within a view stay separate too.
  render(() => probe.useCached('project:vllm', 'tasks', null), {}, [])[1](['t1'])
  assert.equal(render(() => probe.useCached('project:vllm', 'status', null), {}, [])[0], 'vllm-status')
})

test('reload empties the cache so the button means what it says', () => {
  probe.dropCache()
  render(() => probe.useCached('devices', 'devices', null), {}, [])[1]({ hosts: [] })
  assert.equal(probe.VIEW_CACHE.size, 1)
  probe.dropCache()
  assert.equal(probe.VIEW_CACHE.size, 0)
  assert.equal(render(() => probe.useCached('devices', 'devices', null), {}, [])[0], null)
  // And the handler is actually wired to the button, not just available.
  const block = client.slice(client.indexOf('}, running ? t.stop : t.start)'))
  assert(/dropCache\(\); load\(\)/.test(block.slice(0, block.indexOf('t.reload'))), 'reload must clear the cache first')
})

test('one view can be dropped without clearing the rest', () => {
  probe.dropCache()
  render(() => probe.useCached('devices', 'devices', null), {}, [])[1]('fleet')
  render(() => probe.useCached('panel', 'state', null), {}, [])[1]('panel-state')
  probe.dropCache('devices\u0000devices')
  assert.equal(render(() => probe.useCached('devices', 'devices', null), {}, [])[0], null)
  assert.equal(render(() => probe.useCached('panel', 'state', null), {}, [])[0], 'panel-state')
})

test('a functional update is cached, not just the resolved value', () => {
  probe.dropCache()
  const hook = render(() => probe.useCached('panel', 'projects', []), {}, [])
  hook[1](previous => [...previous, 'vllm'])
  assert.deepEqual(render(() => probe.useCached('panel', 'projects', []), {}, [])[0], ['vllm'])
})

test('the plaintext credential draft is never cached', () => {
  /*
   * Everything else in the panel caches; this must not. The draft holds a
   * token or password, and a module-level map would keep it for the life of
   * the page, long after the form was abandoned.
   */
  const view = client.slice(client.indexOf('function CredentialView'), client.indexOf('function Panel'))
  assert(/const \[draft, setDraft\] = React\.useState/.test(view), 'the secret draft stays component-local')
  assert.equal(/useCached/.test(view), false, 'no part of the credential view may cache')
})

test('the credential draft is not reachable through the cache at runtime', () => {
  // The source assertion above can be satisfied while something else writes
  // the secret into the map, so check the map's contents too.
  probe.dropCache()
  render(probe.CredentialView, { t, project: { id: 'vllm' } }, [
    { claude: { configured: false }, jumpserver: { configured: false } },
    { claude: 'sk-live-must-not-persist', jumpserver: 'pw-must-not-persist' },
    false, '', null, '',
  ])
  const stored = JSON.stringify([...probe.VIEW_CACHE.entries()])
  assert.equal(stored.includes('must-not-persist'), false, 'a secret reached the cache')
})

test('an action result is not replayed on a later visit', () => {
  /*
   * Data is cached; claims about what just happened are not. A restored
   * "deployed" banner or a restored "task created" notice would describe an
   * action the user did not just take.
   */
  const deploy = client.slice(client.indexOf('function DeployView'), client.indexOf('function Panel'))
  const header = deploy.slice(0, deploy.indexOf('React.useEffect'))
  assert(/const \[output, setOutput\] = React\.useState/.test(header), 'the run transcript is per-visit')
  assert(/const \[done, setDone\] = React\.useState/.test(header), 'the completion flag is per-visit')
  const create = client.slice(client.indexOf('function CreateTask'), client.indexOf('function DeployView'))
  assert(/const \[made, setMade\] = React\.useState/.test(create.slice(0, create.indexOf('React.useEffect'))),
    'the created-task notice is per-visit')
})

test('no cached view state is written to browser storage', () => {
  // The cache lives for the life of the page on purpose. Persisting it would
  // put panel data on disk, which this plugin does not do anywhere else.
  assert.equal(/localStorage|sessionStorage|indexedDB|document\.cookie/.test(client), false)
})
