/*
 * The one-time auto-clone of the dashboard repository.
 *
 * The plugin needs a checkout of its own dashboard to run. When `repoPath` is
 * unset it clones one, over HTTPS, into a default directory under the Harness
 * home. This is a deliberate exception to the rule that the plugin never
 * clones — it applies ONLY to the dashboard's own repo, never to the monitored
 * project repos — so the guards around it are load-bearing and pinned here.
 */
const assert = require('node:assert/strict')
const fs = require('node:fs')
const path = require('node:path')
const test = require('node:test')
const vm = require('node:vm')

const host = fs.readFileSync(
  path.join(__dirname, '..', 'integrations/dsh-plugin/index.js'),
  'utf8',
).replace(/\r\n/g, '\n')

/** Evaluate defaultRepoPath + normalizeConfig in isolation. */
function loadNormalize(env) {
  const body = host.slice(host.indexOf('function defaultRepoPath'), host.indexOf('function resolveLaunch'))
  const sandbox = {
    resolvePath: p => p, Number, Boolean, String,
    join: (...parts) => parts.join('/'),
    homedir: () => '/home/test',
    process: { env: env || {} },
  }
  vm.createContext(sandbox)
  vm.runInContext(`${body}\nglobalThis.__out = { normalizeConfig, defaultRepoPath }`, sandbox)
  return sandbox.__out
}

test('the default checkout sits under the Harness home', () => {
  const { defaultRepoPath } = loadNormalize({ DSH_HOME: '/home/test/.dsh' })
  assert.equal(defaultRepoPath(), '/home/test/.dsh/fl-plugin-upgrade-workflow')
  // Falls back to ~/.dsh when DSH_HOME is unset.
  const { defaultRepoPath: fallback } = loadNormalize({})
  assert.equal(fallback(), '/home/test/.dsh/fl-plugin-upgrade-workflow')
})

test('an unset repoPath resolves to the default, and is marked non-explicit', () => {
  const { normalizeConfig } = loadNormalize({ DSH_HOME: '/home/test/.dsh' })
  const def = normalizeConfig({})
  assert.equal(def.repoPath, '/home/test/.dsh/fl-plugin-upgrade-workflow')
  assert.equal(def.repoPathExplicit, false, 'the default is not an explicit choice')
  // An explicit path wins and is marked explicit, so the clone leaves it alone.
  const chosen = normalizeConfig({ repoPath: '/some/where' })
  assert.equal(chosen.repoPath, '/some/where')
  assert.equal(chosen.repoPathExplicit, true)
})

test('autoClone defaults on and only an explicit false disables it', () => {
  const { normalizeConfig } = loadNormalize({ DSH_HOME: '/h/.dsh' })
  assert.equal(normalizeConfig({}).autoClone, true, 'on by default')
  assert.equal(normalizeConfig({ autoClone: false }).autoClone, false, 'explicit opt-out kept')
  assert.equal(normalizeConfig({ autoClone: 0 }).autoClone, true, 'a stray value does not disable it')
})

test('the clone is pinned, guarded, and shallow', () => {
  const url = /const DASHBOARD_REPO_URL = 'https:\/\/github\.com\/[\w-]+\/fl-plugin-upgrade-workflow\.git'/.exec(host)
  assert(url, 'the repo URL is a pinned HTTPS literal')
  const clone = host.slice(host.indexOf('async function cloneDashboardIfMissing'), host.indexOf('function snapshot'))
  // Three skips, each a distinct reason not to clone.
  assert(/if \(!config\.autoClone\) return/.test(clone), 'off switch')
  assert(/if \(config\.repoPathExplicit\) return/.test(clone), 'never clones over a chosen path')
  assert(/scripts', 'flflow\.py'\)\)\) return/.test(clone), 'never re-clones an existing checkout')
  // Shallow and pointed at the pinned URL + default path, not caller input.
  assert(/'--depth', '1'/.test(clone), 'a shallow clone')
  assert(/DASHBOARD_REPO_URL, config\.repoPath/.test(clone), 'clones the pinned repo to the resolved path')
  // A non-checkout directory already at the target is the user's; not clobbered.
  assert(/not a dashboard checkout/.test(clone), 'refuses a foreign non-empty directory')
  // Bounded: a hung clone is abandoned.
  assert(/CLONE_TIMEOUT_MS/.test(clone), 'the clone is time-bounded')
})

test('a clone failure is recorded, never thrown at activation', () => {
  const clone = host.slice(host.indexOf('async function cloneDashboardIfMissing'), host.indexOf('function snapshot'))
  // Every failure path sets cloneState to an error rather than throwing.
  assert(/cloneState = \{ phase: 'error'/.test(clone), 'records the error')
  // git stderr can name a proxy or path, so it is redacted.
  assert(/redactText\(readOutput\(handle\), 300\)/.test(clone), 'redacts git output')
  // The activation chain swallows it: the panel still loads.
  const tail = host.slice(host.indexOf('cloneDashboardIfMissing()'))
  assert(/\.catch\(\(\) => \{/.test(tail), 'the activation chain swallows a clone failure')
})

test('the clone runs before autostart, not in parallel', () => {
  // start() looks for a checkout, so the clone must finish first. Match the
  // activation chain as one shape, anchored on the clone CALL site (the one
  // followed by `.catch`, not the function definition), so replacing the call
  // or unchaining the start both fail the match.
  const tail = host.slice(host.indexOf('export function apply'))
  const callAt = tail.indexOf('cloneDashboardIfMissing()\n    .catch')
  assert(callAt >= 0, 'the clone call is chained with .catch at activation')
  const chain = tail.slice(callAt)
  assert(/^cloneDashboardIfMissing\(\)\s*\n\s*\.catch\(\(\) => \{[\s\S]*?\}\)\s*\n\s*\.then\(\(\) => \{[\s\S]*?config\.autoStart[\s\S]*?start\(\)\.catch/.test(chain),
    'the clone call is chained ahead of a conditional autostart')
  // start appears only inside that chain, never as a bare top-level call that
  // would race the clone.
  assert.equal((tail.match(/\n  (?:return )?start\(\)\.catch/g) || []).length, 0,
    'no unsequenced top-level start call')
})
