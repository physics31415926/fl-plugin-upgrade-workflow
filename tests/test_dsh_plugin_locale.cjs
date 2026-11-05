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

test('the panel palette is derived from theme tokens, not fixed hues', () => {
  // The panel adds its own richer surfaces, but each one has to be mixed from
  // a theme token. A literal hue would stay put when the user switches theme
  // while everything around it moved, so it would be wrong in one of the two.
  const custom = [...new Set([...css.matchAll(/--flup-[a-z-]+(?=:)/g)].map(m => m[0]))];
  assert(custom.length > 0, 'expected the panel to define its own variables');
  for (const name of custom) {
    const value = css.slice(css.indexOf(name + ':') + name.length + 1).split('\n')[0];
    if (name === '--flup-on-brand') continue; // Documented exception.
    if (name === '--flup-ease') continue;     // An easing curve, not a colour.
    assert(
      /var\(--dsw-/.test(value),
      name + ' must reference a theme token, got: ' + value.trim(),
    );
  }
});

test('every mixed colour has a plain fallback declared before it', () => {
  // color-mix() needs a recent engine. On an older one the mixed declaration
  // is dropped as invalid, so the rule must already have set a usable value;
  // otherwise the surface falls back to transparent and the text sits on
  // whatever is behind it.
  const rules = [...css.matchAll(/\{([^}]*)\}/g)].map(m => m[1]);
  for (const body of rules) {
    for (const prop of ['background', 'box-shadow']) {
      const decls = [...body.matchAll(new RegExp('(?:^|;)\\s*' + prop + ':\\s*([^;]+)', 'g'))].map(m => m[1].trim());
      const mixed = decls.findIndex(d => /--flup-(wash|edge)-/.test(d));
      if (mixed < 0) continue;
      assert(mixed > 0, prop + ' uses a mix with no fallback before it: ' + body.trim().slice(0, 80));
      assert(
        /var\(--dsw-/.test(decls[mixed - 1]),
        prop + ' fallback must be a theme token, got: ' + decls[mixed - 1],
      );
    }
  }
});

test('motion is disabled when the reader asks for reduced motion', () => {
  // A dashboard someone watches all day must honour the OS setting.
  assert(css.includes('@media (prefers-reduced-motion: reduce)'), 'no reduced-motion block');
  const block = css.slice(css.indexOf('prefers-reduced-motion'));
  assert(/animation-duration:\s*0\.001ms\s*!important/.test(block), 'animations not neutralized');
  assert(/transition-duration:\s*0\.001ms\s*!important/.test(block), 'transitions not neutralized');
});

test('every animation the stylesheet plays is defined', () => {
  // An undefined animation name fails silently, leaving an element that was
  // meant to move sitting still, or worse, stuck at its start frame.
  const defined = new Set([...css.matchAll(/@keyframes\s+([a-z0-9-]+)/g)].map(m => m[1]));
  const played = new Set([...css.matchAll(/animation:\s*([a-z0-9-]+)/g)].map(m => m[1]));
  assert.deepEqual([...played].filter(a => !defined.has(a)), [], 'played but undefined');
  assert.deepEqual([...defined].filter(a => !played.has(a)), [], 'defined but never played');
});

test('only a genuinely running status pulses', () => {
  // Motion means one thing in this panel: work is happening right now. If a
  // settled state pulsed, the signal would degrade into decoration.
  const live = src.match(/const LIVE_STATUSES = \[([^\]]*)\]/);
  assert(live, 'LIVE_STATUSES not found');
  const names = [...live[1].matchAll(/'([a-z_]+)'/g)].map(m => m[1]);
  assert.deepEqual(names.sort(), ['interrupting', 'running', 'starting']);
  // `prepared` is brand-coloured because it is healthy, but nothing is
  // executing yet, so it must not be in the pulsing set.
  assert(!names.includes('prepared'), 'prepared is not an in-flight state');
  for (const settled of ['completed', 'failed', 'interrupted']) {
    assert(!names.includes(settled), settled + ' must not pulse');
  }
});

test('a loading placeholder announces itself to a screen reader', () => {
  // The sweep animation says "loading" only to someone who can see it.
  const start = src.indexOf('function Skeleton(');
  assert(start > 0, 'Skeleton not found');
  const body = src.slice(start, src.indexOf('\n    }', start));
  assert(/role:\s*'status'/.test(body), 'Skeleton needs role=status');
  assert(/aria-label/.test(body), 'Skeleton needs an accessible label');
});
