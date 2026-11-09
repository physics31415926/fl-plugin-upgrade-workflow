/*
 * Self-update: version ordering, release selection and the install guards.
 *
 * The comparison and selection helpers are module-private, so they are read
 * out of the source and evaluated here. That keeps them tested without
 * exporting internals the plugin has no other reason to expose, and it is the
 * same approach the other plugin suites take for host-side logic.
 */
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const test = require('node:test');
const vm = require('node:vm');

const root = path.join(__dirname, '..', 'integrations/dsh-plugin');
// Normalized to LF: the sources are checked out with CRLF on Windows, and
// every boundary match below is written in terms of line-start braces.
const read = name => fs.readFileSync(path.join(root, name), 'utf8').replace(/\r\n/g, '\n');
const host = read('index.js');
const client = read('client.js');

/**
 * Evaluate the named top-level helpers in isolation.
 *
 * The constants they close over are taken from the source too, rather than
 * restated here: a test that declared its own tag prefix or host allowlist
 * would keep passing after the real ones changed.
 */
function load(names) {
  // A bare VM context has no `URL`, which the asset host check uses; without
  // it every candidate is silently rejected and the suite tests nothing.
  const sandbox = { URL };
  const constants = [
    'RELEASE_TAG_PREFIX',
    'RELEASE_ASSET_HOSTS',
  ].map(name => {
    const match = new RegExp(`^const ${name} = .*$`, 'm').exec(host);
    assert(match, `${name} must be a single-line const`);
    return match[0];
  }).join('\n');
  const source = names.map(name => {
    const start = host.indexOf(`function ${name}(`);
    assert.notEqual(start, -1, `${name} must exist`);
    // Each helper is followed by a blank line then a comment or declaration at
    // column 0, so the next line-start `}` closes it.
    const end = host.indexOf('\n}\n', start);
    assert.notEqual(end, -1, `${name} must terminate`);
    return host.slice(start, end + 3);
  }).join('\n');
  vm.runInNewContext(
    `${constants}\n${source}\nglobalThis.out = { ${names.join(', ')} }`,
    sandbox,
  );
  return sandbox.out;
}

const { parseVersion, compareVersions, selectRelease, isReleaseDownloadHost } = load([
  'parseVersion', 'compareVersions', 'selectRelease', 'isReleaseDownloadHost',
]);

test('version parsing accepts releases and refuses anything else', () => {
  // Compared field-wise: the helper runs in a VM realm, so its objects have a
  // different prototype and never satisfy a strict deep equality check.
  assert.deepEqual({ ...parseVersion('1.2.3') }, { major: 1, minor: 2, patch: 3, pre: null });
  assert.deepEqual([...parseVersion('0.1.0-rc.1').pre], ['rc', '1']);
  for (const bad of ['', 'v1.2.3', '1.2', '1.2.3.4', 'latest', null, undefined, '1.2.x']) {
    assert.equal(parseVersion(bad), null, `${bad} must not parse`);
  }
});

test('newer releases sort above older ones', () => {
  assert(compareVersions('0.1.0', '0.2.0') < 0);
  assert(compareVersions('0.1.0', '0.1.1') < 0);
  assert(compareVersions('1.0.0', '0.9.9') > 0);
  assert.equal(compareVersions('0.1.0', '0.1.0'), 0);
  // Double-digit segments must compare numerically, not as text.
  assert(compareVersions('0.9.0', '0.10.0') < 0);
});

test('a prerelease never looks newer than its own release', () => {
  // Without this an installed 0.2.0 would be offered an "update" to 0.2.0-rc.1.
  assert(compareVersions('0.2.0-rc.1', '0.2.0') < 0);
  assert(compareVersions('0.2.0', '0.2.0-rc.1') > 0);
  assert(compareVersions('0.2.0-rc.1', '0.2.0-rc.2') < 0);
  // Numeric identifiers compare numerically.
  assert(compareVersions('0.2.0-rc.2', '0.2.0-rc.10') < 0);
});

const asset = (over = {}) => ({
  name: 'local-fl-upgrade-workflow-0.2.0.tgz',
  browser_download_url: 'https://github.com/physics31415926/fl-plugin-upgrade-workflow/releases/download/dsh-plugin-v0.2.0/local-fl-upgrade-workflow-0.2.0.tgz',
  size: 76605,
  digest: `sha256:${'a'.repeat(64)}`,
  ...over,
});
const release = (over = {}) => ({
  tag_name: 'dsh-plugin-v0.2.0',
  draft: false,
  prerelease: false,
  published_at: '2026-11-06T07:05:27Z',
  body: 'notes',
  assets: [asset()],
  ...over,
});

test('the newest published plugin release is selected', () => {
  const picked = selectRelease([
    release({ tag_name: 'dsh-plugin-v0.1.0' }),
    release({ tag_name: 'dsh-plugin-v0.3.0' }),
    release({ tag_name: 'dsh-plugin-v0.2.0' }),
  ]);
  assert.equal(picked.version, '0.3.0');
  assert.equal(picked.digest, 'a'.repeat(64));
});

test('drafts, prereleases and foreign tags are not offered', () => {
  assert.equal(selectRelease([release({ draft: true })]), null);
  assert.equal(selectRelease([release({ prerelease: true })]), null);
  // The repo also tags the dashboard itself; only plugin tags are ours.
  assert.equal(selectRelease([release({ tag_name: 'v0.2.0' })]), null);
  assert.equal(selectRelease([release({ tag_name: 'dashboard-v0.2.0' })]), null);
  assert.equal(selectRelease([release({ tag_name: 'dsh-plugin-vlatest' })]), null);
});

test('an asset served from anywhere but a GitHub release host is refused', () => {
  // The URL comes from a network response and becomes an install source, so a
  // repointed host must yield no release at all rather than be downloaded.
  for (const url of [
    'https://evil.example/local-fl-upgrade-workflow-0.2.0.tgz',
    'http://github.com/x/y/releases/download/t/local-fl-upgrade-workflow-0.2.0.tgz',
    'https://github.com.evil.example/x.tgz',
    'not-a-url',
  ]) {
    assert.equal(selectRelease([release({ assets: [asset({ browser_download_url: url })] })]), null, url);
  }
  // The advertised asset host is `github.com` and nothing else: the CDN name
  // only ever appears as the redirect target, checked separately below.
  const url = 'https://github.com/path/local-fl-upgrade-workflow-0.2.0.tgz';
  assert.equal(selectRelease([release({ assets: [asset({ browser_download_url: url })] })]).url, url);
  assert.equal(selectRelease([release({ assets: [asset({ browser_download_url: 'https://objects.githubusercontent.com/x.tgz' })] })]), null);
});

test('a release download may land on a GitHub CDN host but nowhere else', () => {
  // Observed live: `github.com` redirects release downloads to
  // `release-assets.githubusercontent.com`, and GitHub has changed that
  // subdomain before, so the landing check matches the domain by suffix.
  for (const good of [
    'github.com',
    'release-assets.githubusercontent.com',
    'objects.githubusercontent.com',
  ]) {
    assert.equal(isReleaseDownloadHost(good), true, good);
  }
  // A suffix match must not be a substring match.
  for (const bad of [
    'evil-githubusercontent.com',
    'githubusercontent.com.evil.test',
    'github.com.evil.test',
    'notgithub.com',
    'githubusercontent.com',
    'evil.test',
  ]) {
    assert.equal(isReleaseDownloadHost(bad), false, bad);
  }
});

test('a release with no tarball is not offered', () => {
  assert.equal(selectRelease([release({ assets: [] })]), null);
  assert.equal(selectRelease([release({ assets: [asset({ name: 'notes.txt' })] })]), null);
  assert.equal(selectRelease([release({ assets: [asset({ browser_download_url: '' })] })]), null);
});

test('a malformed digest is dropped rather than trusted', () => {
  // A digest that is not sha256 hex must not be compared against; reporting
  // `digest: null` makes the install say it was unverified instead.
  for (const digest of ['sha1:abc', 'sha256:xyz', 'a'.repeat(64), undefined]) {
    assert.equal(selectRelease([release({ assets: [asset({ digest })] })]).digest, null, String(digest));
  }
});

test('nothing but a release array yields a release', () => {
  for (const payload of [null, undefined, {}, 'x', [], [null], [{}]]) {
    assert.equal(selectRelease(payload), null);
  }
});

test('the release source is pinned in code, not taken from config or request', () => {
  const block = host.slice(host.indexOf('const RELEASE_OWNER'), host.indexOf('function normalizeConfig'));
  assert(/const RELEASE_API = `https:\/\/api\.github\.com/.test(block), 'the API origin is a literal');
  // A configurable or caller-supplied source would let a profile setting or a
  // request body decide which code the Harness installs and runs.
  const route = host.slice(host.indexOf("action === 'self-update'"), host.indexOf("action === 'self-update'") + 1200);
  assert.equal(/readBody|parsed\.|searchParams\.get\('(url|version|spec|path)'\)/.test(route), false,
    'the install route must take no parameters');
  for (const key of ['releaseUrl', 'updateUrl', 'releaseRepo']) {
    assert.equal(host.includes(key), false, `${key} must not be configurable`);
  }
});

test('the download is size-capped, digest-checked and redirect-checked', () => {
  const block = host.slice(host.indexOf('async function runUpdate'), host.indexOf('function snapshot'));
  assert(block.includes('MAX_ASSET_BYTES'), 'caps the body size');
  assert(block.includes("createHash('sha256')"), 'hashes the download');
  assert(block.includes('actual !== release.digest'), 'compares against the published digest');
  // Redirects are followed, so the landing URL needs the same host check as
  // the advertised one.
  assert(block.includes('response.url'), 'inspects the final URL');
  assert(block.includes('isReleaseDownloadHost(landed.hostname)'), 're-checks the landing host');
  assert(block.includes('mode: 0o600'), 'writes the tarball private to this user');
  assert(block.includes('rm(target'), 'removes the tarball afterwards');
});

test('a linked checkout is never overwritten by a release', () => {
  const block = host.slice(host.indexOf('async function runUpdate'), host.indexOf('function snapshot'));
  assert(block.includes('if (linkedInstall)'), 'refuses before installing');
  // The decision is a local filesystem fact, so it cannot be misreported by a
  // registry and needs no network.
  const detect = host.slice(host.indexOf('const linkedInstall'), host.indexOf('async function latestRelease'));
  assert(detect.includes('realpathSync'), 'resolves the real module path');
  assert(detect.includes('node_modules'), 'tests for the profile store');
  assert(detect.includes('return true'), 'defaults to refusing when undecidable');
});

test('concurrent presses share one install', () => {
  const route = host.slice(host.indexOf("action === 'self-update'"), host.indexOf("action === 'self-update'") + 800);
  assert(route.includes('if (!updating)'), 'reuses a running install');
  assert(route.includes('updating = runUpdate()'), 'starts at most one');
});

/** The UpdateButton component source. */
const button = () => client.slice(
  client.indexOf('function UpdateButton'),
  client.indexOf('async function callJson'),
);

test('checking for updates is user-initiated, never automatic', () => {
  const block = button();
  /*
   * The earlier version checked on mount. That reached GitHub every time the
   * panel mounted, for data that changes perhaps weekly, and it is the
   * behaviour the user asked to remove: a check should be a consequence of a
   * click.
   */
  assert.equal(/useEffect\(\(\)\s*=>\s*\{?\s*check/.test(block), false,
    'no mount-time check');
  assert.equal(block.includes('React.useEffect'), false,
    'the component runs no effect at all');
  // Every read of update-state is inside a click handler.
  const reads = [...block.matchAll(/update-state/g)];
  assert.equal(reads.length, 1, 'exactly one place reads update-state');
  assert(/onClick: check/.test(block), 'the check is bound to a click');
});

test('a click is never answered from a stale cache', () => {
  // A cached answer would make the button a no-op that looks like it worked.
  assert(/update-state\?refresh=1/.test(button()), 'a click forces a refresh');
});

test('repeated clicking cannot burn the GitHub rate limit', () => {
  const block = host.slice(host.indexOf('let releaseCache'), host.indexOf('async function updateState'));
  assert(block.includes('RELEASE_MIN_INTERVAL_MS'), 'a floor between network checks exists');
  // `refresh` must not be an unconditional bypass: with one, a held-down
  // button spends the unauthenticated hourly budget in a minute.
  assert(/refresh \? age < RELEASE_MIN_INTERVAL_MS/.test(block),
    'a forced refresh still respects the floor');
  const floor = /const RELEASE_MIN_INTERVAL_MS = ([0-9_]+)/.exec(host);
  assert(floor, 'the floor is a named constant');
  assert(Number(floor[1].replace(/_/g, '')) >= 10_000, 'the floor is a meaningful interval');
});

test('the button reports being up to date instead of vanishing', () => {
  const block = button();
  /*
   * The whole point of the user's complaint: with nothing to install the
   * component used to render null, so "am I current?" had no answer anywhere
   * in the UI — and the answer "yes" was exactly the case that showed nothing.
   */
  assert(block.includes('t.updateCurrent'), 'says it is current');
  assert(block.includes('if (!state.newer)'), 'handles the up-to-date case explicitly');
  assert(block.includes('t.updateInstalledIs'), 'names the installed version');
  /*
   * Still clickable: a release can appear at any time, and this is the only
   * way to ask. Bounded by the next branch rather than a character count, so
   * editing a comment inside the branch cannot move the assertion off it.
   */
  const upToDate = block.slice(
    block.indexOf('if (!state.newer)'),
    block.indexOf('const version ='),
  );
  assert(upToDate.includes('onClick: check'), 'stays clickable when current');
  assert(upToDate.includes('t.updateCurrent'), 'the up-to-date branch carries the label');
});

test('a failed check is distinguished from being up to date', () => {
  const block = button();
  // The question went unanswered; claiming currency would be a lie.
  assert(block.includes('state.checkError'), 'inspects the reported check error');
  assert(block.includes('t.updateRateLimited'), 'names rate limiting specifically');
  assert(block.includes('t.updateCheckFailed'), 'reports other failures');
  // Bounded by the next branch, not a character count.
  const failure = block.slice(
    block.indexOf('if (state.checkError)'),
    block.indexOf('if (!state.newer)'),
  );
  assert(failure.includes('onClick: check'), 'stays retryable after a failure');
});

test('an unpressed button offers the check, and installing is a second press', () => {
  const block = button();
  assert(block.includes('t.updateCheck'), 'offers to check before any state exists');
  assert(block.includes("method: 'POST'"), 'installs over POST');
  assert(block.includes('onClick: install'), 'the install is its own click');
  // Two distinct actions: the check must not install anything.
  const checkFn = block.slice(block.indexOf('const check ='), block.indexOf('const install ='));
  assert.equal(checkFn.includes('self-update'), false, 'checking never installs');
});

test('a version it cannot install is still reported', () => {
  const block = button();
  assert(block.includes('if (!state.installable)'), 'reports it as text');
  assert(block.includes('t.updateLinkedHint'), 'explains a linked checkout');
  assert(block.includes('t.updateUnsupportedHint'), 'explains a build without the manager');
  // The new module is on disk but this page still runs the old one.
  assert(block.includes('t.updateRestart'), 'asks for a reload after installing');
});

test('both locales describe every update string', () => {
  const keys = [...client.matchAll(/\bt\.(update[A-Za-z]+)/g)].map(m => m[1]);
  assert(keys.length > 0, 'the panel uses update copy');
  const dict = name => {
    const start = client.indexOf(`      ${name}: {`);
    assert.notEqual(start, -1, `${name} dictionary must exist`);
    return client.slice(start, client.indexOf('\n      },', start));
  };
  for (const locale of ['en', 'zh']) {
    const block = dict(locale);
    for (const key of new Set(keys)) {
      assert(new RegExp(`\\b${key}:`).test(block), `${locale} must define ${key}`);
    }
  }
});

test('no update string is defined but unused', () => {
  /*
   * This is the check that was missing. `updateCheck` ("check for updates")
   * was translated into both locales and never referenced, so the dictionary
   * promised a control the panel did not have — and the one-directional test
   * above, which only proves used keys are defined, could not see it.
   */
  const used = new Set([...client.matchAll(/\bt\.(update[A-Za-z]+)/g)].map(m => m[1]));
  const start = client.indexOf('      en: {');
  const defined = [...client.slice(start, client.indexOf('\n      },', start))
    .matchAll(/^\s{8}(update[A-Za-z]+):/gm)].map(m => m[1]);
  assert(defined.length > 0, 'the dictionary defines update copy');
  const orphans = defined.filter(key => !used.has(key));
  assert.deepEqual(orphans, [], `defined but never rendered: ${orphans.join(', ')}`);
});
