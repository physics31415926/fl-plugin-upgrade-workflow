/*
 * The per-backend CI column must show the adaptation CI verdict, not the
 * branch-tip run.
 *
 * Upstream reports two different CI facts per backend. `ci` is the backend's
 * adaptation CI — the run on its PR, or the accepted / merged-PR run — and is
 * the answer to "did this backend's adaptation pass". `current_ci` is narrower:
 * jobs observed at the current branch HEAD, which for an already-merged backend
 * is almost always "unknown" because nobody reran per-backend jobs on the tip.
 *
 * The summariser forwarded `current_ci` first, so every merged backend read as
 * CI "unknown" while its adaptation had in fact passed. These tests pin the
 * corrected resolution by running the real function on real-shaped rows.
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

/** Evaluate the two summariser functions in isolation. */
function load() {
  const grab = name => {
    const start = host.indexOf(`function ${name}(`)
    assert.notEqual(start, -1, `${name} must exist`)
    const end = host.indexOf('\n}\n', start)
    assert.notEqual(end, -1, `${name} must terminate`)
    return host.slice(start, end + 3)
  }
  const sandbox = {}
  vm.runInNewContext(
    `${grab('summarizeBackend')}${grab('summarizeBranch')}\nglobalThis.out = { summarizeBranch }`,
    sandbox,
  )
  return sandbox.out.summarizeBranch
}
const summarizeBranch = load()

/** One backend row in the shape upstream emits. */
const backend = (id, ci, currentCi, over = {}) => ({
  id,
  label: id.toUpperCase(),
  integration: { status: 'merged', detail: 'in branch', url: null },
  ci,
  current_ci: currentCi,
  pr: { number: 1, state: 'closed', merged: true, url: 'https://example/pr/1' },
  gate: { status: 'missing', passed: 0, failed: 0, missing: 20, total: 20 },
  acceptance: { status: 'passed', summary: 'ok', rows_total: 3 },
  ...over,
})
const branch = (head, backends) => summarizeBranch({ branch: 'b', head, backends })

test('a merged backend shows its adaptation CI, not the branch-tip unknown', () => {
  const [b] = branch('HEADSHA', [backend('cuda',
    { status: 'passed', scope: 'merged-pr', detail: '7 jobs', url: 'https://example/run/1', commit: 'MERGESHA' },
    { status: 'unknown', scope: 'branch', detail: '0 backend test jobs at branch HEAD', url: null, commit: 'HEADSHA' },
  )]).backends
  assert.equal(b.ci.status, 'passed', 'the verdict is the adaptation CI result')
  assert.equal(b.ci.scope, 'merged-pr')
  // The branch-tip run is kept, but only as a secondary signal.
  assert.equal(b.currentCi.status, 'unknown')
})

test('a running or cancelled adaptation CI is reported faithfully', () => {
  // These are real upstream states on merged-PR commits; the plugin must not
  // launder them into "unknown" or "passed".
  for (const status of ['running', 'cancelled', 'failed']) {
    const [b] = branch('HEADSHA', [backend('x',
      { status, scope: 'merged-pr', detail: 'd', url: 'u', commit: 'MERGESHA' },
      { status: 'unknown', scope: 'branch', detail: 'd', url: null, commit: 'HEADSHA' },
    )]).backends
    assert.equal(b.ci.status, status, `${status} must survive summarisation`)
  }
})

test('a branch-scoped pass goes stale once the branch moves past it', () => {
  // This mirrors the dashboard's own effectiveCi: a pass recorded against a
  // branch commit is no longer current once HEAD has advanced.
  const [moved] = branch('NEWHEAD', [backend('x',
    { status: 'passed', scope: 'branch', detail: 'd', url: 'u', commit: 'OLDHEAD' },
    null,
  )]).backends
  assert.equal(moved.ci.status, 'stale', 'a branch pass at a past commit is stale')

  const [current] = branch('SAMEHEAD', [backend('x',
    { status: 'passed', scope: 'branch', detail: 'd', url: 'u', commit: 'SAMEHEAD' },
    null,
  )]).backends
  assert.equal(current.ci.status, 'passed', 'a branch pass still at HEAD is current')
})

test('the stale downgrade is only for branch scope', () => {
  // A merged-PR or accepted pass is pinned to the PR, not the branch tip, so a
  // moved HEAD does not make it stale.
  for (const scope of ['merged-pr', 'accepted-tree', 'pr']) {
    const [b] = branch('NEWHEAD', [backend('x',
      { status: 'passed', scope, detail: 'd', url: 'u', commit: 'OLDCOMMIT' },
      null,
    )]).backends
    assert.equal(b.ci.status, 'passed', `${scope} pass must not be downgraded`)
  }
})

test('the branch-tip run is dropped when it equals the verdict', () => {
  // For an unmerged backend upstream sets current_ci === ci (same object). The
  // panel should not show a redundant "latest run" in that case.
  const ci = { status: 'passed', scope: 'pr', detail: 'd', url: 'u', commit: 'c' }
  const [b] = branch('HEAD', [backend('x', ci, ci)]).backends
  assert.equal(b.currentCi, null, 'no redundant branch-tip signal')
})

test('a backend with no CI at all yields null, not a crash', () => {
  const [b] = branch('HEAD', [backend('x', null, null)]).backends
  assert.equal(b.ci, null)
  assert.equal(b.currentCi, null)
})

test('the client reads the resolved verdict, not the inverted fallback', () => {
  const client = fs.readFileSync(
    path.join(__dirname, '..', 'integrations/dsh-plugin/client.js'),
    'utf8',
  )
  // Bound to the CI cell itself: between the integration cell above it and the
  // acceptance cell below. Header `th` anchors would wrongly include the
  // integration cell's `value: backend.integration`.
  const ciCell = client.slice(
    client.indexOf('h(Status, { value: backend.integration })'),
    client.indexOf('backend.acceptance && {'),
  )
  assert(ciCell, 'the CI cell region is found')
  assert.equal(/backend\.ci\s*\|\|\s*backend\.(current_ci|currentCi)/.test(client), false,
    'the inverted fallback is gone')
  assert(/value: backend\.ci\b/.test(ciCell), 'the CI cell renders the resolved verdict')
  // The branch-tip run appears only as secondary context (an `extra` tooltip),
  // never as the cell's primary value.
  assert(/extra: backend\.currentCi/.test(ciCell), 'the branch-tip run rides along as context')
  assert.equal(/value: backend\.currentCi/.test(ciCell), false,
    'the branch-tip run is never the primary value')
})
