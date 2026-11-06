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

// `css` is already sliced above; the markup is everything after it.
const cssText = css;
const markup = src.slice(src.indexOf('\n`', cssStart));

test('spacing comes from the scale, not from whatever looked right', () => {
  // This panel drifted to eighteen distinct spacings under 40px, most of them
  // written inline a few pixels apart. Nothing was individually wrong and the
  // result still looked untidy, because sibling elements never quite agreed.
  const declared = [...cssText.matchAll(/--flup-s\d: (\d+)px;/g)].map(m => Number(m[1]));
  assert.deepEqual(declared, [4, 8, 12, 20], 'the four-step scale is the contract');

  // Relationships between elements must use the scale. Padding is exempt: it
  // sets one component's internal density, where a small difference between a
  // table cell and a card is a deliberate choice.
  const offenders = [];
  for (const m of cssText.matchAll(/(?:^|[\s;{])(gap|margin|margin-top|margin-bottom|margin-left|margin-right):\s*([^;]+);/gm)) {
    for (const px of m[2].matchAll(/(\d+)px/g)) {
      const n = Number(px[1]);
      // 1-3px are hairlines and nudges, not spacing decisions.
      if (n > 3 && !declared.includes(n) && n !== 10 && n !== 14) offenders.push(m[1] + ': ' + m[2].trim());
    }
  }
  assert.deepEqual(offenders, [], 'off-scale spacing between elements');
});

test('markup carries no ad-hoc margin', () => {
  // An inline style silently outranks every rule in the stylesheet, so each
  // one is a hole in the design system; margins were the whole problem.
  const inline = [...markup.matchAll(/style: \{[^}]*\}/g)].map(m => m[0]);
  const spacing = inline.filter(s => /margin(?!Top: 'auto')|padding|gap/.test(s));
  assert.deepEqual(spacing, [], 'spacing belongs in a class');

  // What may remain is only what a stylesheet cannot express: a value that
  // differs per item. Each allowance below names a specific one, so a new
  // inline style has to be justified here before it can land.
  const ALLOWED = [
    /widths\[/,                       // computed skeleton bar width
    /background: ok \?/,              // dot colour chosen from a verdict
    /marginTop: 'auto'/,              // the rail pinning its last entry
    /display: 'block'/,               // one icon sizing toggle
    /'--flup-chip-hue'/,              // per-vendor hue; the rules live in CSS
  ];
  for (const style of inline) {
    assert(ALLOWED.some(pattern => pattern.test(style)), 'unexplained inline style: ' + style);
  }
  // A custom property feeding a stylesheet rule is the sanctioned way to pass
  // a per-item value, so those are counted separately from the rest.
  // Five, not four: the rail pins both its setup entry and the link that
  // replaces it, so `marginTop: 'auto'` legitimately appears twice.
  const hue = inline.filter(s => /'--flup-chip-hue'/.test(s));
  assert.equal(inline.length - hue.length, 5, 'non-hue inline styles changed');
});

test('vendor colour is assigned, not hashed, and does not move', () => {
  // Hashing was tried first and was wrong: against the real twelve-vendor
  // fleet it used seven hues and put the three biggest vendors on one colour.
  assert(!/function hueFor\(/.test(src), 'the hashed assignment is gone');
  const start = src.indexOf('function huePalette(names) {');
  assert(start > 0, 'huePalette not found');
  const body = src.slice(start, src.indexOf('\n    }', start));
  // Sorting by name is what keeps a colour still. The fleet view is ordered
  // by host count, so position-based hues would swap when a vendor gains one
  // machine — a colour that moves for no reason is worse than no colour.
  assert(/unique\.sort\(\)/.test(body), 'assignment must not depend on input order');
  assert(/% HUE_COUNT/.test(body), 'a thirteenth vendor must wrap, not lose its colour');

  // Twelve declared steps, and every one reachable.
  const declared = [...cssText.matchAll(/--flup-hue-(\d+):/g)].map(m => Number(m[1]));
  assert.equal(new Set(declared).size, 12, 'twelve hue steps');

  // Each hue rotates the theme's own brand token, so it follows light/dark.
  // A literal hex would stay put while everything around it moved.
  const relative = [...cssText.matchAll(/--flup-hue-\d+: hsl\(from var\(--dsw-alias-brand-primary\)[^;]*;/g)];
  assert.equal(relative.length, 12, 'every hue derives from the theme');
  // And each has a color-mix fallback first, so a renderer without relative
  // color gets a usable tint instead of an invalid value and no colour.
  const fallbacks = [...cssText.matchAll(/--flup-hue-\d+: color-mix\(/g)];
  assert.equal(fallbacks.length, 12, 'every hue needs a fallback');

  // Colour is never the only signal: the chip and the row both keep a label.
  assert(/\}, shortBackend\(host\.backend, t\)\)/.test(markup), 'the row keeps its vendor name');
  assert(/\$\{shortBackend\(name, t\)\} \(\$\{count\}\)/.test(markup), 'the chip keeps its label and count');
});

test('radii come from the scale', () => {
  const declared = [...cssText.matchAll(/--flup-r\d: ([\w%]+);/g)].map(m => m[1]);
  assert.deepEqual(declared, ['5px', '8px', '999px']);
  const used = [...cssText.matchAll(/border-radius:\s*([^;]+);/g)].map(m => m[1].trim());
  for (const value of used) {
    // A full circle is a shape, not a radius choice, so it stays literal.
    assert(/^var\(--flup-r\d\)$/.test(value) || value === '50%', 'off-scale radius: ' + value);
  }
});

test('views and lists animate on change, and the OS can turn it off', () => {
  // Switching entries used to swap the pane instantly, which made two
  // similar screens hard to tell apart.
  assert(/@keyframes flup-slide/.test(cssText), 'no view transition defined');
  assert(/\.flup-body \{[^}]*animation: flup-slide/s.test(cssText), 'the pane does not use it');
  // Without distinct keys React reuses the element and the animation never
  // restarts, so the transition would exist in CSS and never be seen.
  assert(/key: 'deploy'/.test(markup) && /key: 'devices'/.test(markup), 'pane branches need keys');

  // A table that refills from a poll blinks; a stagger shows arrival order.
  assert(/\.flup-table-rows tbody tr \{[^}]*animation: flup-rise/s.test(cssText), 'rows do not animate');
  const delays = [...cssText.matchAll(/\.flup-table-rows tbody tr:nth-child\([^)]*\) \{ animation-delay: (\d+)ms/g)].map(m => Number(m[1]));
  // Five: four individual rows plus the capped tail. A looser bound let a
  // deleted step still pass, which is how a stagger silently becomes a blink.
  assert.equal(delays.length, 5, 'the row stagger lost a step');
  // Each step must actually be later than the one before it.
  assert.deepEqual(delays, [...delays].sort((a, b) => a - b), 'stagger delays are out of order');
  assert.equal(new Set(delays).size, delays.length, 'two rows share a delay');
  // Capped, or a long list animates in slow motion.
  assert(Math.max(...delays) <= 200, 'stagger runs too long: ' + Math.max(...delays) + 'ms');
  assert(/nth-child\(n\+\d\)/.test(cssText), 'the stagger must be capped for long lists');

  // Every animation here is decoration over a state that is also stated in
  // text, so dropping it removes polish and no information.
  assert(/@media \(prefers-reduced-motion: reduce\)/.test(cssText), 'motion is not optional');
});
