// The panel ships its own locale dictionaries and stylesheet as plain
// JavaScript, so nothing checks them at build time. These tests do: a key
// present in one language but not the other, or a class used but never
// defined, is a defect that would otherwise only appear on screen.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const test = require('node:test');
const root = path.join(__dirname, '..', 'integrations/dsh-plugin');
const src = fs.readFileSync(path.join(root, 'client.js'), 'utf8');

/** One locale dictionary's source text. */
function block(name) {
  const start = src.indexOf(`      ${name}: {`);
  assert.notEqual(start, -1, 'no ' + name + ' dictionary');
  return src.slice(start, src.indexOf('\n      },', start));
}
const keys = text => [...text.matchAll(/^ {8}([A-Za-z][A-Za-z0-9]*):/gm)].map(m => m[1]);
const en = keys(block('en'));
const zh = keys(block('zh'));
const duplicates = list => list.filter((k, i) => list.indexOf(k) !== i);

test('both locales define exactly the same keys', () => {
  const setEn = new Set(en);
  const setZh = new Set(zh);
  // A key missing from one language renders as "undefined" for those users.
  assert.deepEqual(en.filter(k => !setZh.has(k)), [], 'missing from zh');
  assert.deepEqual(zh.filter(k => !setEn.has(k)), [], 'missing from en');
  assert.equal(en.length, zh.length);
});

test('no locale key is declared twice', () => {
  // A duplicate silently overrides the earlier value.
  assert.deepEqual(duplicates(en), [], 'duplicate en keys');
  assert.deepEqual(duplicates(zh), [], 'duplicate zh keys');
});

test('every key the panel reads is defined', () => {
  // `t.foo` with no entry renders as "undefined" rather than failing, so it
  // would otherwise reach a user.
  const REACT = new Set(['createElement', 'createContext', 'useState', 'useEffect',
    'useCallback', 'useContext', 'Fragment', 'Provider']);
  const setEn = new Set(en);
  const used = [...new Set([...src.matchAll(/\bt\.([A-Za-z][A-Za-z0-9]*)/g)].map(m => m[1]))];
  assert.deepEqual(used.filter(k => !setEn.has(k) && !REACT.has(k)), []);
});

const cssStart = src.indexOf('const CSS = `');
const css = src.slice(cssStart, src.indexOf('\n`', cssStart));

test('every class the panel applies is defined, and every defined class is used', () => {
  const defined = new Set([...css.matchAll(/\.(flup-[a-z0-9-]+)/g)].map(m => m[1]));
  const applied = new Set();
  for (const m of src.matchAll(/'(flup-[a-z0-9- ]+)'/g)) {
    for (const c of m[1].split(/\s+/)) if (c.startsWith('flup-')) applied.add(c);
  }
  assert.deepEqual([...applied].filter(c => !defined.has(c)), [], 'used but undefined');
  assert.deepEqual([...defined].filter(c => !applied.has(c)), [], 'defined but unused');
});

test('no CSS selector is declared twice', () => {
  // Two rules for one selector silently override each other.
  const rules = [...css.matchAll(/^\.([a-z0-9-]+)\s*\{/gm)].map(m => m[1]);
  assert.deepEqual(duplicates(rules), []);
});

test('colours come from theme tokens so the panel follows the active theme', () => {
  const literals = [...css.matchAll(/:\s*(#[0-9a-fA-F]{3,8}|rgba?\([^)]*\))/g)].map(m => m[1]);
  // One exception: a fixed foreground on the brand fill, which is brand-owned
  // in both themes.
  assert(literals.length <= 1, 'hard-coded colours: ' + literals.join(','));
  const LEGAL = new Set(['--dsw-alias-bg-base', '--dsw-alias-bg-layer-1', '--dsw-alias-bg-layer-2',
    '--dsw-alias-bg-overlay', '--dsw-alias-border-l1', '--dsw-alias-border-l2',
    '--dsw-alias-brand-primary', '--dsw-alias-label-primary', '--dsw-alias-label-secondary',
    '--dsw-alias-state-error-primary', '--dsw-alias-state-idle-primary',
    '--dsw-alias-state-success-primary', '--dsw-alias-state-warn-primary',
    '--dsw-specific-sidebar-fill']);
  const tokens = [...new Set([...src.matchAll(/--dsw-[a-z0-9-]+/g)].map(m => m[0]))];
  // A token that does not exist resolves to nothing, leaving an invisible
  // element rather than a visible error.
  assert.deepEqual(tokens.filter(x => !LEGAL.has(x)), [], 'unknown theme tokens');
});
