# CSS contract

Three files, one link tag, zero build steps, zero network calls.

```html
<link rel="stylesheet" href="/css/app.css" />
```

That is the whole integration. `app.css` `@import`s `tokens.css` and the
self-hosted IBM Plex, so a page cannot get the order wrong or forget one.

| File | What it is | Edit by hand? |
|---|---|---|
| `tokens.css` | Carbon g10 + g100 semantic palettes and the app's design tokens | **No** — generated |
| `app.css` | Every pattern the UI uses, built on those tokens | Yes |
| `gallery.html` | Every pattern rendered once, in both themes | Yes |
| `../vendor/plex/` | 4 IBM Plex woff2 faces + their `@font-face` block | **No** — generated |
| `../vendor/carbon/carbon.css` | Full Carbon CSS, CDN font faces stripped. **Optional** | **No** — generated |

Regenerate the generated files with:

```bash
python3 webapp/tools/vendor_assets.py            # needs npm + network, once
python3 webapp/tools/vendor_assets.py --verify   # offline: proves nothing phones home
```

Open the gallery while the app is running:
<http://127.0.0.1:8765/css/gallery.html> (or whichever port ./run.sh reports)

---

## The four rules

**1. No raw colour outside `tokens.css`.**
Every colour is `var(--cds-*)` or `var(--a2c-*)`. A hex, `rgb()` or `hsl()`
literal in `app.css` or in a `style=` attribute is a bug — it will be wrong in
one of the two themes, and nobody will notice until a reviewer opens the app in
dark mode during a demo.

**2. No network.**
No `@import` of a CDN, no `url(https://…)`, no Google Fonts, no
`1.www.s81c.com`. The container has no egress. `vendor_assets.py --verify` fails
the build if any stylesheet under `static/` references an absolute URL.

**3. Focus is restyled, never removed.**
`outline: none` without a replacement ring is a defect. The global
`:focus-visible` rule already gives everything a Carbon-correct 2px ring.

**4. `--cds-support-*` colours shapes; `--a2c-text-*` colours words.**
Carbon's status tokens are icon and fill colours. As text on a light layer they
measure 3.35:1 (success), 2.46:1 (caution) and **1.68:1** (warning) — all below
the 4.5:1 AA floor. Use the text-safe aliases for text.

---

## Theming

Light (g10) is the default. Dark (g100) applies when the OS asks for it, unless
the user pins a theme. The pin is one attribute on `<html>`:

```js
document.documentElement.dataset.theme = 'dark';   // force dark
document.documentElement.dataset.theme = 'light';  // force light
delete document.documentElement.dataset.theme;     // follow the OS
```

Nothing else is needed — every rule reads through `var(--cds-*)`, and
`color-scheme` is switched alongside so native scrollbars and form controls
follow. There is no theme class on `<body>`.

The one trap worth naming: **`--cds-focus` is `#ffffff` in g100, not blue.**
Hand-written Carbon palettes get this wrong and the dark focus ring vanishes.
`tokens.css` is extracted from the real package, so it cannot drift.

---

## Tokens

### Carbon semantics — 328 of them, use these first

The full g10/g100 set is in `tokens.css`. The ones you will actually reach for:

| Purpose | Token |
|---|---|
| Page background | `--cds-background` |
| Card / panel surface | `--cds-layer-01` (hover `--cds-layer-hover-01`, selected `--cds-layer-selected-01`) |
| Sunken surface (code, table header) | `--cds-layer-02`, `--cds-layer-accent-01` |
| Body text | `--cds-text-primary` |
| Secondary text | `--cds-text-secondary` (AA) |
| Faintest legible text | `--cds-text-helper` (AA) |
| Placeholder / disabled | `--cds-text-placeholder`, `--cds-text-disabled` |
| Hairline | `--cds-border-subtle-01` |
| Visible border | `--cds-border-strong-01` |
| Accent / link | `--cds-interactive`, `--cds-link-primary` |
| Focus ring | `--cds-focus` |
| Status marks | `--cds-support-{success,warning,error,info,caution-major}` |
| Notification fills | `--cds-notification-background-{success,warning,error,info}` |
| Tag pairs | `--cds-tag-background-*` + `--cds-tag-color-*` |
| Skeleton | `--cds-skeleton-background`, `--cds-skeleton-element` |

### Application tokens — `--a2c-*`

| Group | Tokens |
|---|---|
| Type stacks | `--a2c-font-sans`, `--a2c-font-mono` |
| Type sizes | `--a2c-type-caption` (12px), `--a2c-type-body-sm` (14px, the default), `--a2c-type-body`, `--a2c-type-heading-sm/-heading/-heading-lg`, `--a2c-type-display` |
| Line height | `--a2c-lh-tight`, `--a2c-lh-body`, `--a2c-lh-prose` |
| Weights | `--a2c-weight-light` (300), `-regular` (400), `-label` (500), `-strong` (600) |
| Spacing | `--a2c-space-01` … `--a2c-space-09` (Carbon scale, 2px … 48px) |
| Radius | `--a2c-radius` (2px), `--a2c-radius-lg` (4px) |
| Motion | `--a2c-ease-productive/-entrance/-exit`, `--a2c-dur-fast/-dur/-dur-slow` |
| Status as text | `--a2c-text-success`, `--a2c-text-info`, `--a2c-text-caution`, `--a2c-text-danger` |
| Confidence marks | `--a2c-confidence-{high,medium,low,unknown}` |
| Confidence text | `--a2c-confidence-{high,medium,low,unknown}-text` |
| Overlay | `--a2c-overlay-scrim`, `--a2c-overlay-halo`, `--a2c-overlay-stroke` |
| Elevation | `--a2c-shadow-raised`, `--a2c-shadow-overlay` |
| Layout | `--a2c-header-height`, `--a2c-sidebar-width`, `--a2c-content-max`, `--a2c-prose-max` |

Never define a new colour token inline. Add it to `EXTRA_THEMED` in
`webapp/tools/vendor_assets.py` with a comment saying why no `--cds-*` token
would do, and regenerate. There is exactly one entry today (`--a2c-amber`,
because Carbon has no AA-safe caution colour for text).

---

## The visual language

This is the IBM Carbon **presentation** idiom used for the BB / DIRIS executive
decks, brought down from slide density to application density. Five rules carry
the whole look; break one and the page stops looking like the deck.

| Rule | Concretely |
|---|---|
| Light weights at large sizes | `--a2c-weight-light` (300) on every display and section heading. 400 is body, 600 is the only emphasis. **Nothing is ever bold.** The decks specify 200 for display; only Light/Regular/SemiBold are vendored, so 300 is the lightest truthful weight here and asking for 200 renders as 300 anyway. |
| Fluid clamp scale | `--a2c-type-hero`, `--a2c-type-section`, `--a2c-type-lead` — the deck scale divided by ~1.6, same proportions between the steps. |
| Section labels | `.section-title` and `.eyebrow`: 12px / 500 / `0.16em` tracking / uppercase. The most identifiable mark in this language. |
| Hairlines, not borders | 1px `--cds-border-subtle-01` rules and a 3px `--a2c-rule` accent edge on the panels that matter. Shadows only on true overlays. |
| Accent with parsimony | Blue-60 for interaction, the accent rule and the eyebrow. Cyan and teal as data marks only. **No purple** — it is not in this project's operational palette. |

Motion is Carbon's: `--a2c-ease-productive` for state, `--a2c-ease-expressive`
for entrances.

`--a2c-type-hero/-section/-lead`, `--a2c-ease-expressive`, `--a2c-rule` and
`--a2c-gutter` are declared at the top of `app.css`, not in `tokens.css`. They
are **type and motion only** — no colour is ever defined outside `tokens.css`,
so nothing can drift between the two themes.

---

## Classes

### Shell and layout

| Class | Notes |
|---|---|
| `.skip-link` | First child of `<body>`, targets `#main`. Uses the inverse pair, not `--cds-interactive`: white on `#4589ff` is 3.35:1 in g100 |
| `.masthead` | Sticky, `--a2c-header-height`, hairline bottom. Parts: `.masthead__brand` > `.masthead__mark` + `.masthead__name`, `.masthead__context`, `.masthead__actions` > `.masthead-btn` |
| `.app-main`, `.app-footer` | |
| `.wrap` (+ `--narrow`) | Centred column, `--a2c-content-max`, `--a2c-gutter` inline padding |
| `.stack` (+ `--tight`, `--loose`), `.row` (+ `--between`), `.push` | Layout primitives |

**Every single-column grid in this file is clamped to `minmax(0, 1fr)`** — see
the block comment above `.stack`. Without it one unbreakable mono path inside a
tool call widens the implicit `auto` column past the viewport and the whole page
grows a horizontal scrollbar. Add any new single-column grid to that selector
list.

### Surfaces

`.surface` with `--accent` (3px top rule), `--edge` (3px left rule), `--sunken`,
`--flush`. Parts: `.surface__head`, `.surface__body` (+ `--flush`),
`.surface__foot`. Section heading inside a head: `.section-title`.

### Buttons

`.btn` plus one of `.btn-primary`, `.btn-secondary`, `.btn-tertiary`,
`.btn-ghost`, `.btn-quiet`, `.btn-danger`. Sizes: `.btn-sm`, `.btn--lg`,
`.btn--full`, `.btn--center`. `.btn__note` is the right-aligned mono detail
inside a wide button. Group with `.btn-set`. `.link-btn` reads as a link.

The disabled style deliberately departs from Carbon's own pair, which measures
1.94:1 — the gate submit spends most of its life disabled and an unreadable
instruction is not an instruction.

### Fields

`.field` > `<label>` + `.input` / `.select` / `.textarea` + `.field-hint`.
Layout: `.form-grid`, `.field-inline`, `.form-actions`. Invalid state is
`aria-invalid="true"` on the control.

`.disclose` (+ `--bare`) > `<summary>` + `.disclose__body` is the collapsed
disclosure used for **Advanced options** and every raw-payload escape hatch.

### Tags, status and notices

`.tag` (+ `--blue --cyan --teal --green --red --magenta --cool-gray --warm-gray
--outline --sm`) wrapping a `.tag__label`.

`.status-badge` is squared, not pill-shaped, because it carries machine state.
Combine with the run-model state name so CSS and JSON cannot drift:
`.status-created`, `.status-running`, `.status-succeeded`, `.status-failed`,
`.status-awaiting_input`, `.status-blocked`, `.status-cancelled`,
`.status-skipped`. `.status-dot` takes the same modifiers. `.spinner` is the
"something is executing" mark.

`.notice` (+ `.notice-error`, `.notice-warn`, `.notice-success`) >
`.notice__body` > `.notice-title`, `.notice-remedy`, `.notice-list`.
**Never put an interactive element inside a `.notice`.**

`.strip` (+ `--error`, `--warn`) is the full-bleed bar for run and environment
state, with `.strip__remedy` as its action text.

### Screen 1 — the invitation

`.invite` > `.invite__lede` (`.eyebrow--rule`, `.hero-title`, `.lead`) +
`.dropzone` (+ `.is-over`, `.is-busy`; parts `.dz-glyph`, `.dz-title`,
`.dz-hint`, `.dz-meta`, `.dz-progress`) + `.journey` > `.journey__step` >
`.journey__n` + `.journey__name` + `.journey__what` + `.runlist` >
`.runlist__item` > `.runlist__id`, `.runlist__src`, `.runlist__when`.

### Screen 2 — execution

`.runbar` > `.runbar__id` + `.switcher` + `.meters` > `.meter` >
`.meter__value` + `.meter__label` (+ `.meter--warn`).

`.workspace` is the two-column grid: `.workspace__left` (sticky) holds the
diagram, the second column holds the track. Collapses at 72rem.

`.track` > `.stage[data-state]` where the state is one of `pending`, `running`,
`succeeded`, `failed`, `awaiting` — **not** the raw run-model status, because a
stage that is `running` while the run is `awaiting_input` is parked at the gate
and must not show a spinner. Parts: `.stage__marker` (the rail dot),
`.stage__head` > `.stage__n` + `.stage__title` + `.stage__facts`, and
`.stage__body` > `.stage__section`.

Reasoning: `.reasoning` (+ `--full` to lift the height cap) > `.think` >
`.think__label` + `.think__text`, alternating with `.say`. `.cursor` is the
blinking caret on the segment that is still streaming.

Tool calls: `.calls` > `.call` (a `<details>`, + `.call--error`) with
`.call__caret`, `.call__name`, `.call__args`, `.call__facts` in the summary and
`.call__body` > `.payload` > `.payload__label` inside. `.dim`, `.problem` and
`.remedy` are the three note weights used in a call body.

`.wrote` > `<button>` (+ `.is-missing`) are the files a stage wrote.

Bounding boxes: `.image-frame` > `.vision-image` + `.overlay-layer`
(+ `.has-selection`) > `.ov-box` with `.ov-conf-high|-medium|-low|-unknown`,
`.ov-connection`, `.ov-alert`, `.is-hovered`, `.is-selected`, `.is-dimmed`, and
`.ov-tag` inside. Positions are **percentages**, so the boxes track the rendered
image through resize and zoom with no JS redraw. Legend: `.image-legend` >
`.lg` + `.lg-high|-medium|-low|-conn`. Facts: `.factlist` > `.fact` > `dt`/`dd`.

### Screen 3 — the gate

`.gate` (+ `--approved`, `--blocked`, `--absent`, `--decided`) >
`.gate__head` (`.gate__title`, `.gate__verdict`) + `.gate__grid` >
`.gate__evidence` + `.gate__decision`.

Evidence: `.gate__findings` > `.finding` > `.finding__sev`
(+ `--critical|--high|--medium|--low`) + `.finding__body` > `.finding__title` +
`.finding__text`. `.gate__excerpt` is the monospaced verdict.md pane.

Decision: `.gate__choices` > `.choice` > `<input>` + `.choice__label` +
`.choice__hint`. Selection state comes from `:has(input:checked)`, not a class.
`.gate__override` is the warning shown when the choice contradicts the verdict.

### Screen 4 — delivery

`.deliver` > `.downloads` > `.dl` > `.dl__name` + `.dl__what` + `.dl__facts`,
then `.files` > `.tree` + `.viewer`.

`.tree` > `<details>`/`<ul>` with `.tree__file` (+ `.is-selected`,
`.is-missing`) and `.tree__size`. `.viewer` > `.viewer__head` >
`.viewer__path` + `.viewer__meta`.

### Code

`.code-block` (+ `--wrap`, `--tall`, `.code-stderr`) and `.artifact-pre`.
Line numbers: `.gutter` + `.code-col`, applied by `js/components/artifact.js`.
Syntax classes: `.j-key .j-str .j-num .j-bool .j-null .j-punct`,
`.y-comment .y-dash`, `.md-heading .md-fence .md-code .md-quote .md-list
.md-verdict-ok .md-verdict-block`. `.snippet` > `.snippet__actions` floats a
Copy button over a block.

### Environment, empty and loading

`.health-panel` > `.probe-list` > `.probe` (+ `-ok|-warn|-error` on
`.probe-dot`) > `.probe-head`, `.probe-title`, `.probe-id`, `.probe-detail`,
`.probe-remedy` (+ `-missing`).

`.empty`, `.empty-state` + `.empty-hint`, `.loading`, `.skeleton`,
`.skeleton-text`, `.skeleton-block`.

### Accessibility, non-negotiable

The reasoning pane is `aria-live="off"`. A stream-json frame arrives several
times a second and announcing each one makes the app unusable with a screen
reader. Only stage transitions and terminal states go into the run view's
`aria-live="polite"` region.

---

## What NOT to do

- **No raw hex, `rgb()` or `hsl()`** outside `tokens.css`. Not in `app.css`, not
  in a `style=` attribute, not in an inline `<style>`.
- **No `!important`** except the one on `[hidden]`, which exists so that any
  author `display:` rule cannot resurrect an element the JS hid. That bug shipped
  once already: an empty red error box under the drop zone.
- **No `px` font sizes.** Use the `--a2c-type-*` scale.
- **No new `@font-face`.** Four faces are vendored; there is no italic and no
  bold, by design. Emphasis is `--a2c-weight-strong` (600), never 700.
- **No `outline: none`** without an equivalent visible replacement.
- **No editing `tokens.css`, `vendor/plex/plex.css` or `vendor/carbon/carbon.css`.**
  They are generated; your change disappears on the next regeneration.
- **No `.cds--*` class** unless you have linked `vendor/carbon/carbon.css`
  yourself and read the warning below.
- **No colour as the only carrier of meaning.** Every state that has a colour
  also has text or a shape — that is what keeps the app usable in forced-colors
  mode and for the 8% of male reviewers with a colour vision deficiency.

## The optional Carbon stylesheet

`vendor/carbon/carbon.css` (751 KB) is the full Carbon component CSS with the
105 CDN `@font-face` blocks removed. **Nothing links it.** `app.css` does not
need it and the app has never rendered with it.

If you link it to use real `cds--*` markup, know that:

- It carries its own reset and type rules that will restyle everything on the
  page. Link it **before** `app.css`.
- It defines the theme tokens under `.cds--g10` / `.cds--g100` **class**
  selectors, which outrank `tokens.css`'s `:root`. Putting one of those classes
  on `<body>` overrides the `data-theme` contract for that subtree.
- It ships no SVGs. Carbon icons are a separate 53 MB package; extract the few
  you need and inline them.
- `css/styles.min.css` is published on npm but not documented by IBM. That is
  why it is pinned to `@carbon/styles@1.111.0` and vendored, never resolved at
  runtime.

## Weights

| Asset | Bytes | Notes |
|---|---|---|
| `tokens.css` | 44 852 B | 7 922 B gzipped; 328 tokens × 3 theme blocks |
| `app.css` | 60 168 B | 10 989 B gzipped |
| `plex.css` | 1 125 B | the four `@font-face` blocks |
| Plex Sans Light + Regular + SemiBold | 196 436 B | woff2, `complete` (not split) |
| Plex Mono Regular | 45 640 B | woff2 |
| **Transferred on first load** | **353 590 B (345 KB)** | ~330 KB of it is fonts, cached forever |
| **Transferred on repeat load** | **≈ 20 KB** | the two stylesheets, gzipped |
| `vendor/carbon/carbon.css` | 769 593 B | **not loaded** — opt-in only |
| Licences (Carbon + Plex) | 15 795 B | redistribution requires shipping them |
| **`static/vendor/` on disk** | **1 028 589 B (1 004 KB)** | |

Serve with `Cache-Control: public, max-age=31536000, immutable` in production —
every path is version-pinned.
