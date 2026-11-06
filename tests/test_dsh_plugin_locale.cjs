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
  /*
   * Every colour the panel defines has to trace back to a theme token, or it
   * stays put when the user switches theme while everything around it moves.
   *
   * Derivation is now a chain rather than one hop: the accent comes from a
   * theme token, the surfaces come from the accent, and the plates mix into
   * the surfaces. So this resolves transitively and requires the chain to
   * terminate at a theme token, which is the actual invariant. Demanding a
   * direct reference would reject a correct two-step derivation and, worse,
   * would pass a token that merely mentioned a theme token somewhere.
   */
  const definitions = new Map();
  for (const m of css.matchAll(/(--flup-[a-z0-9-]+):\s*([^;]+);/g)) {
    // A later declaration of the same name is a deliberate override; keep the
    // last one, which is what the browser uses.
    definitions.set(m[1], m[2].trim());
  }
  assert(definitions.size > 0, 'expected the panel to define its own variables');

  const EXEMPT = new Set([
    '--flup-on-brand', // Documented: white on a fill that does not change.
    '--flup-ease',     // An easing curve, not a colour.
    '--flup-s1', '--flup-s2', '--flup-s3', '--flup-s4',
    '--flup-r1', '--flup-r2', '--flup-r3',
  ]);

  const rootedInTheme = (name, seen = new Set()) => {
    if (seen.has(name)) return false; // A cycle resolves to nothing at all.
    seen.add(name);
    const value = definitions.get(name);
    if (!value) return false;
    if (/var\(--dsw-/.test(value)) return true;
    const refs = [...value.matchAll(/var\((--flup-[a-z0-9-]+)\)/g)].map(m => m[1]);
    return refs.length > 0 && refs.every(ref => rootedInTheme(ref, new Set(seen)));
  };

  for (const [name, value] of definitions) {
    if (EXEMPT.has(name)) continue;
    // --flup-chip-hue is supplied per element from the markup, not defined here.
    if (name === '--flup-chip-hue') continue;
    assert(rootedInTheme(name), name + ' does not derive from a theme token: ' + value);
    // A literal colour anywhere in the chain defeats the point.
    assert(!/#[0-9a-fA-F]{3,8}/.test(value), name + ' contains a literal colour: ' + value);
  }

  // And the chain must be shallow enough to reason about.
  const depth = (name, d = 0) => {
    const value = definitions.get(name) || '';
    const refs = [...value.matchAll(/var\((--flup-[a-z0-9-]+)\)/g)].map(m => m[1]);
    return refs.length ? Math.max(...refs.map(r => depth(r, d + 1))) : d;
  };
  for (const name of definitions.keys()) {
    if (EXEMPT.has(name) || name === '--flup-chip-hue') continue;
    assert(depth(name) <= 2, name + ' is derived through too many steps to follow');
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
      // The fallback may be a theme token or one of the panel's own surface
      // tokens, which are themselves theme-derived and resolve on any engine
      // that can do color-mix at all. What it must not be is another mix of
      // the same kind, which would fail in exactly the same engines.
      const fallback = decls[mixed - 1];
      assert(
        /var\(--dsw-/.test(fallback) || /var\(--flup-surface(-\d)?\)/.test(fallback),
        prop + ' fallback must resolve without a wash mix, got: ' + fallback,
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

  // Each hue rotates a theme token, so it follows light/dark; a literal hex
  // would stay put while everything around it moved.
  // Hue and lightness come from the theme; saturation is pinned, because the
  // theme's 71% is poster paint once twelve plates sit in one row.
  const relative = [...cssText.matchAll(/--flup-hue-\d+: hsl\(from var\((--dsw-alias-[\w-]+)\) calc\(h \+ (\d+)\) (\d+)% l\);/g)];
  assert.equal(relative.length, 12, 'every hue derives from a theme token');
  assert.equal(new Set(relative.map(m => m[1])).size, 1, 'one base token for the whole wheel');
  const saturations = new Set(relative.map(m => Number(m[3])));
  assert.equal(saturations.size, 1, 'one saturation for the whole wheel');
  const saturation = [...saturations][0];
  // The khaki register. Above roughly 45% the row stops reading as muted.
  assert(saturation >= 25 && saturation <= 45, 'saturation left the muted range: ' + saturation);
  // Lightness must stay inherited, or the wheel stops following the theme.
  assert(/\) \d+% l\);/.test(cssText), 'lightness must come from the theme');

  /*
   * The base must actually have a hue to rotate.
   *
   * This is the bug the first attempt shipped: it rotated brand-primary,
   * which resolves to #0f1115 in light and #f9fafb in dark — a near-black and
   * a near-white. Rotating the hue of an almost achromatic colour returns
   * almost the same colour, so all twelve steps rendered identically and the
   * panel showed no colour at all. The CSS was well-formed and the feature
   * was supported; the base was simply wrong.
   *
   * Only three theme tokens are saturated enough to rotate.
   */
  const base = relative[0][1];
  const SATURATED = [
    '--dsw-alias-state-success-primary',
    '--dsw-alias-state-warn-primary',
    '--dsw-alias-state-error-primary',
  ];
  assert(SATURATED.includes(base), 'the hue base must be a saturated token, not ' + base);

  // Angles must separate. Even spacing beat hand-picked uneven angles when
  // measured, so this pins the spacing that was actually verified.
  const angles = relative.map(m => Number(m[2]));
  assert.deepEqual(angles, [0, 30, 60, 90, 120, 150, 180, 210, 240, 270, 300, 330], 'angles must stay evenly spread');

  // No color-mix fallback: mixing two tokens always parses, so a fallback
  // would always win in the renderers that cannot do the real thing, hiding
  // a broken wheel rather than revealing it.
  assert.equal([...cssText.matchAll(/--flup-hue-\d+: color-mix\(/g)].length, 0,
    'a fallback here masks a broken wheel');

  // Colour is never the only signal: the chip and the row both keep a label.
  assert(/\}, shortBackend\(host\.backend, t\)\)/.test(markup), 'the row keeps its vendor name');
  assert(/\$\{shortBackend\(name, t\)\} \(\$\{count\}\)/.test(markup), 'the chip keeps its label and count');
});

test('the hue wheel renders twelve colours a person can tell apart', () => {
  /*
   * Compute what the declared angles actually produce.
   *
   * Checking the CSS shape is not enough: the first version of this wheel was
   * perfectly formed and rendered twelve copies of the same near-black,
   * because its base token was achromatic. Only arithmetic catches that, so
   * this resolves the base token's real value and rotates it.
   */
  const RESOLVED = {
    // From the application's own token definitions.
    '--dsw-alias-state-success-primary': '#22c55e',
    '--dsw-alias-state-warn-primary': '#f59e0b',
    '--dsw-alias-state-error-primary': '#ec1313',
  };
  const steps = [...cssText.matchAll(/--flup-hue-\d+: hsl\(from var\((--dsw-alias-[\w-]+)\) calc\(h \+ (\d+)\) (\d+)% l\);/g)];
  assert.equal(steps.length, 12);
  const hex = RESOLVED[steps[0][1]];
  assert(hex, 'unknown hue base: ' + steps[0][1]);
  const pinnedSaturation = Number(steps[0][3]) / 100;

  const toHsl = value => {
    const n = parseInt(value.slice(1), 16);
    const r = ((n >> 16) & 255) / 255; const g = ((n >> 8) & 255) / 255; const b = (n & 255) / 255;
    const mx = Math.max(r, g, b); const mn = Math.min(r, g, b);
    const l = (mx + mn) / 2;
    if (mx === mn) return { h: 0, s: 0, l };
    const d = mx - mn;
    const s = l > 0.5 ? d / (2 - mx - mn) : d / (mx + mn);
    const h = (mx === r ? (g - b) / d + (g < b ? 6 : 0) : mx === g ? (b - r) / d + 2 : (r - g) / d + 4) * 60;
    return { h, s, l };
  };
  const toRgb = (h, s, l) => {
    h = ((h % 360) + 360) % 360;
    const c = (1 - Math.abs(2 * l - 1)) * s;
    const x = c * (1 - Math.abs(((h / 60) % 2) - 1));
    const m = l - c / 2;
    const [r, g, b] = h < 60 ? [c, x, 0] : h < 120 ? [x, c, 0] : h < 180 ? [0, c, x]
      : h < 240 ? [0, x, c] : h < 300 ? [x, 0, c] : [c, 0, x];
    return [r, g, b].map(v => Math.round((v + m) * 255));
  };

  const hsl = toHsl(hex);
  // A base with no hue to rotate is the failure this test exists for.
  assert(hsl.s > 0.5, 'the hue base is too desaturated to rotate: s=' + hsl.s.toFixed(2));

  /*
   * Measure the PLATE, not the raw hue.
   *
   * What the user sees is the hue mixed into a theme surface, so the raw wheel
   * being separable proves nothing on its own: a mix percentage low enough
   * would flatten it back into the background. Both themes are checked,
   * because one declaration has to work against #fff and #232324 alike.
   */
  const plate = (angle, surface, percent) => {
    const rgb = toRgb(hsl.h + angle, pinnedSaturation, hsl.l);
    return rgb.map((v, i) => Math.round(v * percent + surface[i] * (1 - percent)));
  };
  const distance = (a, b) => Math.abs(a[0] - b[0]) + Math.abs(a[1] - b[1]) + Math.abs(a[2] - b[2]);
  const relativeLuminance = rgb => {
    const [r, g, b] = rgb.map(v => {
      const c = v / 255;
      return c <= 0.03928 ? c / 12.92 : ((c + 0.055) / 1.055) ** 2.4;
    });
    return 0.2126 * r + 0.7152 * g + 0.0722 * b;
  };
  const contrast = (a, b) => {
    const pair = [relativeLuminance(a), relativeLuminance(b)].sort((x, y) => y - x);
    return (pair[0] + 0.05) / (pair[1] + 0.05);
  };

  // Resolved from the application's own token definitions.
  const THEMES = [
    { name: 'light', surface: [255, 255, 255], text: [15, 17, 21] },
    { name: 'dark', surface: [35, 35, 36], text: [249, 250, 251] },
  ];
  // The mix percentages the stylesheet actually uses for the two plates.
  const chipMix = /\.flup-chip-hue \{[^}]*color-mix\(in srgb, var\(--flup-chip-hue\) (\d+)%/s.exec(cssText);
  const rowMix = /\.flup-vendor \{[^}]*color-mix\(in srgb, var\(--flup-chip-hue\) (\d+)%/s.exec(cssText);
  assert(chipMix, 'the chip has no backing plate');
  assert(rowMix, 'the table cell has no backing plate');

  /*
   * An unmixed fill sidesteps the percentages this test reasons about, and at
   * 35% saturation it is also the one value that cannot carry the label: a
   * neat demonstration that matching a pattern is not the same as covering a
   * rule. Every plate must be a mix against a theme surface, so that the
   * contrast arithmetic below actually describes what renders.
   */
  for (const selector of ['.flup-chip-hue', '.flup-vendor']) {
    const block = new RegExp(selector.replace('.', '\\.') + '[^{]*\\{([^}]*)\\}', 's').exec(cssText);
    assert(block, selector + ' not found');
    const fill = /background:\s*([^;]+);/.exec(block[1]);
    assert(fill, selector + ' has no background');
    // The surface may be a raw theme background or the panel's tinted
    // derivative of one; what matters is that the hue is mixed INTO a
    // surface rather than used as a flat fill.
    assert(/^color-mix\(in srgb, var\(--flup-chip-hue\) \d+%, var\((?:--dsw-alias-bg-|--flup-surface)/.test(fill[1].trim()),
      selector + ' must mix its hue into a surface, not fill with it: ' + fill[1].trim());
  }
  // A dot is not a plate: the previous attempt was too small to register.
  assert(Number(chipMix[1]) >= 50, 'the chip plate is too faint: ' + chipMix[1] + '%');
  // The row must stay quieter than the chip, or it outshouts the status column.
  assert(Number(rowMix[1]) < Number(chipMix[1]), 'the table plate must be quieter than the chip');

  const angles = steps.map(m => Number(m[2]));
  for (const { name, surface, text } of THEMES) {
    for (const [label, percent] of [['chip', Number(chipMix[1]) / 100], ['row', Number(rowMix[1]) / 100]]) {
      const plates = angles.map(a => plate(a, surface, percent));
      let closest = Infinity;
      for (let i = 0; i < plates.length; i += 1) {
        for (let j = i + 1; j < plates.length; j += 1) {
          closest = Math.min(closest, distance(plates[i], plates[j]));
        }
      }
      // The broken wheel scored 2 of a possible 765. Plates are mixed down, so
      // the floor is lower than the raw wheel's, but far above indistinguishable.
      assert(closest >= 12, `${name} ${label} plates too close: ${closest}`);
      assert.equal(new Set(plates.map(p => p.join(','))).size, 12, `${name} ${label}: two plates identical`);
      // Colour must never cost legibility. 4.5:1 is the AA floor for small text.
      const worst = Math.min(...plates.map(p => contrast(p, text)));
      assert(worst >= 4.5, `${name} ${label} label contrast ${worst.toFixed(2)} is below AA`);
    }
  }
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

test('nothing inside the stylesheet can terminate its template literal', () => {
  /*
   * The CSS is a JS template literal, so a backtick in comment prose ends it
   * early and the file stops parsing. This has broken the build three times
   * while writing these styles, always in an explanatory comment quoting a
   * token name. A syntax check catches it, but only after a confusing failure
   * some distance from the cause, so name the cause here.
   *
   * ${ would be worse: it interpolates silently instead of failing.
   */
  const BACKTICK = String.fromCharCode(96);
  /*
   * Scan the raw source from the opening delimiter to the stylesheet's final
   * rule, not the `cssText` slice used elsewhere. That slice ends at the
   * first line consisting of a lone backtick, so a stray delimiter moves the
   * end and takes the offending line out of range — the scan would look only
   * at text that is fine and report nothing. Anchoring on a known last rule
   * instead keeps the whole region in view however it is broken.
   */
  /*
   * Scan the CSS by its own structure rather than by hunting for an end
   * marker, because every marker I tried was wrong in a way that made the
   * test pass while the bug was present. A rule inside the sheet (the
   * reduced-motion block) sits only a third of the way in, so strays after it
   * escaped; the statement that consumes the sheet is nowhere near it, since
   * the CSS is injected as a style element a thousand lines later, so
   * indexOf returned -1 and the region swallowed the entire module's
   * JavaScript, where backticks are ordinary and expected.
   *
   * The reliable definition is the literal itself: it runs from its opening
   * delimiter to the first line that is nothing but a closing delimiter, and
   * anything before that line which also contains one is the bug.
   */
  const open = src.indexOf('const CSS = ' + BACKTICK);
  assert(open > 0, 'stylesheet not found');
  const lines = src.slice(open).split('\n');
  const closing = lines.findIndex((line, index) => index > 0 && line.trim() === BACKTICK);
  assert(closing > 0, 'the stylesheet literal is never closed');

  const offenders = lines.slice(1, closing)
    .map((line, index) => [index + 1, line])
    .filter(([, line]) => line.includes(BACKTICK) || line.includes('${'));
  assert.deepEqual(
    offenders.map(([index, line]) => `line +${index}: ${line.trim().slice(0, 70)}`),
    [],
    'a backtick or ${ inside the CSS literal ends it early; name tokens in plain words instead',
  );
  // Sanity-check the scan itself: the region has to be the whole stylesheet,
  // not a few lines of it. An anchor that silently matched early is exactly
  // how the earlier versions of this test reported success on broken input.
  assert(closing > 600, 'the scanned region is too short to be the stylesheet: ' + closing + ' lines');
  assert(lines.slice(1, closing).join('\n').includes('prefers-reduced-motion'),
    'the scan missed the end of the stylesheet');
});

test('the whole palette stays legible in both themes', () => {
  /*
   * Resolve the panel's palette arithmetically and check every pairing that
   * carries text or structure, in light and dark.
   *
   * This exists because the panel's colours are now a derivation chain: the
   * accent comes from a theme token, surfaces from the accent, plates mix into
   * surfaces. Any link can be adjusted innocently and ruin a pairing three
   * steps away — tinting the page a little more also tints every card, every
   * plate and the sticky table header. Reading percentages out of the
   * stylesheet keeps this measuring what actually ships.
   */
  const percentOf = name => {
    const m = new RegExp('--flup-' + name + ':\\s*color-mix\\(in srgb, var\\(--flup-accent\\) (\\d+)%').exec(cssText);
    assert(m, '--flup-' + name + ' is not a mix of the accent');
    return Number(m[1]) / 100;
  };
  const hslToRgb = (h, s, l) => {
    h = ((h % 360) + 360) % 360; s /= 100; l /= 100;
    const c = (1 - Math.abs(2 * l - 1)) * s;
    const x = c * (1 - Math.abs(((h / 60) % 2) - 1));
    const m = l - c / 2;
    const [r, g, b] = h < 60 ? [c, x, 0] : h < 120 ? [x, c, 0] : h < 180 ? [0, c, x]
      : h < 240 ? [0, x, c] : h < 300 ? [x, 0, c] : [c, 0, x];
    return [r, g, b].map(v => Math.round((v + m) * 255));
  };
  const blend = (a, b, p) => a.map((v, i) => Math.round(v * p + b[i] * (1 - p)));
  const luminance = rgb => {
    const [r, g, b] = rgb.map(v => {
      const c = v / 255;
      return c <= 0.03928 ? c / 12.92 : ((c + 0.055) / 1.055) ** 2.4;
    });
    return 0.2126 * r + 0.7152 * g + 0.0722 * b;
  };
  const ratio = (a, b) => {
    const pair = [luminance(a), luminance(b)].sort((x, y) => y - x);
    return (pair[0] + 0.05) / (pair[1] + 0.05);
  };
  const apart = (a, b) => Math.abs(a[0] - b[0]) + Math.abs(a[1] - b[1]) + Math.abs(a[2] - b[2]);

  // The accent's own saturation and the fill's, read from the stylesheet.
  const accentSat = Number(/--flup-accent:\s*hsl\(from var\(--dsw-alias-state-success-primary\) h (\d+)%/.exec(cssText)[1]);
  const fillMatch = /--flup-fill:\s*hsl\(from var\(--dsw-alias-state-success-primary\) h (\d+)% (\d+)%\)/.exec(cssText);
  assert(fillMatch, '--flup-fill must pin its own saturation and lightness');
  const inkMatch = /--flup-ink:\s*color-mix\(in srgb, var\(--flup-accent\) (\d+)%, var\(--dsw-alias-label-primary\)\)/.exec(cssText);
  assert(inkMatch, '--flup-ink must mix the accent into the theme label colour');

  // state-success-primary is #22c55e in both themes; the surfaces differ.
  const BASE_H = 145.4;
  const BASE_L = 45.3;
  const accent = hslToRgb(BASE_H, accentSat, BASE_L);
  const fill = hslToRgb(BASE_H, Number(fillMatch[1]), Number(fillMatch[2]));
  const inkPercent = Number(inkMatch[1]) / 100;
  const mixes = {
    surface: percentOf('surface'),
    layer1: percentOf('surface-1'),
    wash: percentOf('wash-brand'),
  };

  // Resolved from the application's own token definitions.
  const THEMES = [
    { name: 'light', base: [255, 255, 255], layer1: [255, 255, 255], label: [15, 17, 21], secondary: [107, 107, 112] },
    { name: 'dark', base: [21, 21, 23], layer1: [35, 35, 36], label: [249, 250, 251], secondary: [150, 150, 155] },
  ];
  const chipPercent = Number(/\.flup-chip-hue \{[^}]*color-mix\(in srgb, var\(--flup-chip-hue\) (\d+)%/s.exec(cssText)[1]) / 100;

  for (const theme of THEMES) {
    const page = blend(accent, theme.base, mixes.surface);
    const card = blend(accent, theme.layer1, mixes.layer1);
    const wash = blend(accent, theme.layer1, mixes.wash);
    const ink = blend(accent, theme.label, inkPercent);
    const where = detail => `${theme.name}: ${detail}`;

    // Text, at the AA floor for small text.
    assert(ratio(theme.label, page) >= 4.5, where('primary text on the page'));
    assert(ratio(theme.label, card) >= 4.5, where('primary text on a card'));
    assert(ratio(theme.secondary, card) >= 4.5, where('secondary text on a card'));
    assert(ratio(theme.label, wash) >= 4.5, where('primary text on a wash'));
    assert(ratio(ink, page) >= 4.5, where('accent text on the page'));
    assert(ratio(ink, card) >= 4.5, where('accent text on a card'));
    // A filled button's label is white in both themes.
    assert(ratio([255, 255, 255], fill) >= 4.5, where('white on the filled button'));

    // Structure. In light theme all three background tokens resolve to #fff,
    // so without the tint a card had no fill difference from the page at all
    // and depended entirely on its border.
    assert(apart(page, card) >= 6, where('a card must lift off the page'));

    // The tint must not swallow the vendor plates that sit on top of it.
    const plates = Array.from({ length: 12 }, (unused, i) => blend(hslToRgb(BASE_H + i * 30, accentSat, BASE_L), card, chipPercent));
    let closest = Infinity;
    for (let i = 0; i < plates.length; i += 1) {
      for (let j = i + 1; j < plates.length; j += 1) closest = Math.min(closest, apart(plates[i], plates[j]));
    }
    assert(closest >= 12, where(`chip plates collapsed to ${closest}`));
    assert(Math.min(...plates.map(p => ratio(p, theme.label))) >= 4.5, where('a plate label fell below AA'));

    // The page tint must stay quieter than the quietest thing that carries
    // information, or the decoration outranks the data.
    const pageShift = apart(theme.base, page);
    const plateShift = apart(card, blend(accent, card, 0.4));
    assert(pageShift < plateShift, where('the page tint is louder than a vendor plate'));
  }
});
