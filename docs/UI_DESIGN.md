# UI design language for `ai-oncall`

This document is the design contract for the Next.js web UI. It synthesizes
two references the maintainer trusts:

- `ryanthedev/design-for-ai` — Claude Code plugin distilled from David
  Kadavy's *Design for Hackers*. Six-phase methodology: Foundation →
  Structure → Typography → Composition & Hierarchy → Color → Technical.
  Advocates intentional proportions, modular type scales, and warm/cool
  color balance over "safe defaults."
- `pbakaus/impeccable` (`npx skills add pbakaus/impeccable`) — Anthropic
  frontend-design extension. Bans the generic-AI tropes: Inter, purple
  gradients, nested cards, gray-on-color. Mandates OKLCH, tinted
  neutrals, modular type scales, motion with restraint.

Both share the same north star: **fight the default**. The AI-generated UI
fingerprint (Inter + Tailwind purple + cards-on-cards + bouncy springs) is
the floor, not the ceiling. Use this document to stay above it.

---

## 1. Foundation — what the UI is for

The UI exists for two moments, not one.

**Hot moment** — Sam, on-call, 11pm, Friday. Phone or laptop, half-attentive.
The UI's job is to communicate the ranked hypotheses, the evidence, and the
suggested action in **5 seconds of glance**. Nothing else competes.

**Cold moment** — the post-incident review on Monday. The same data,
explored deeply: trajectory trace, alternative hypotheses, runbook edits,
learnings feedback. Multiple tabs, calm reading.

Design the same screen for both. The hot view is the **default render**;
the cold view is exposed via expand/disclosure on the same primitives. No
separate "incident report" page.

---

## 2. Type system

### Fonts (use exactly these — pick two from the lists below)

**Display / serif (one of):**
- **Fraunces** (variable, free, Google Fonts) — modern serif with
  optical-size axis. Recommended.
- **Source Serif 4** — quieter alternative.
- **Lora** — humanist, less editorial.

**UI / sans (one of):**
- **Geist** (Vercel, free) — recommended. Tabular numerics built in.
- **Inter Tight** (Inter's tighter sibling, *not* default Inter) — okay.
- **JetBrains Sans** — slightly technical character.

**Mono:**
- **Geist Mono** — recommended.
- **JetBrains Mono** — okay.
- **IBM Plex Mono** — okay.

> ⚠ Banned: default Inter, Roboto, Arial, Helvetica, system-ui-as-only-font.
> The Impeccable skill calls these out by name; treat them as smells.

### Modular scale (1.25 base, fluid)

```css
--t-2xs: clamp(0.69rem, 0.67rem + 0.10vw, 0.75rem);
--t-xs:  clamp(0.78rem, 0.74rem + 0.18vw, 0.875rem);
--t-sm:  clamp(0.875rem, 0.82rem + 0.28vw, 1.00rem);
--t-md:  clamp(1.00rem,  0.93rem + 0.34vw, 1.125rem);
--t-lg:  clamp(1.20rem,  1.08rem + 0.55vw, 1.40rem);
--t-xl:  clamp(1.50rem,  1.30rem + 1.00vw, 1.95rem);
--t-2xl: clamp(2.00rem,  1.60rem + 2.00vw, 2.95rem);
--t-3xl: clamp(2.60rem,  1.90rem + 3.50vw, 4.40rem);
```

Use `--t-3xl` only on the dashboard hero number ("MTTR 4m 12s"), nowhere
else. The display serif is reserved for `--t-xl` and up.

### Rules
- Body line-height: **1.55**. Display: **1.02**. Never the same.
- Numerics: **tabular** everywhere a number could change in place
  (latency, confidence %, counts). `font-feature-settings: "tnum"`.
- Letter-spacing on small caps + monospace eyebrows: **0.10em**.
- Maximum measure for prose blocks: **64ch**.

---

## 3. Color — OKLCH only

Hex and HSL are forbidden in the codebase. Every color is declared in OKLCH
so neutrals can be tinted, accessibility math is predictable, and dark/light
modes share the same accent.

### Palette

```css
:root {
  /* Tinted neutrals — cool blue, never pure */
  --ink-0: oklch(98% 0.005 250);
  --ink-1: oklch(93% 0.008 250);
  --ink-2: oklch(78% 0.012 250);
  --ink-3: oklch(60% 0.015 250);
  --ink-4: oklch(42% 0.020 250);
  --ink-5: oklch(28% 0.022 250);
  --ink-6: oklch(20% 0.022 250);
  --ink-7: oklch(15% 0.020 250);
  --ink-8: oklch(11% 0.018 250);
  --ink-9: oklch(8%  0.015 250);

  /* One chromatic accent — saturated teal-cyan */
  --acc:    oklch(72% 0.13 200);
  --acc-hi: oklch(82% 0.14 200);
  --acc-lo: oklch(58% 0.12 200);
  --acc-bg: oklch(72% 0.13 200 / 12%);

  /* Status — semantic only, never decorative */
  --pos:  oklch(74% 0.16 155);   /* OK / passing */
  --neg:  oklch(70% 0.18 22);    /* error / failing */
  --warn: oklch(82% 0.14 80);    /* low-confidence / escalate */
}
```

**Rules.**
- One chromatic accent. Resist the urge to add a second.
- Status colors *only* communicate status. Never paint a button
  `--pos` because it looks nice.
- Body text on colored surfaces: use `--ink-0` or `--ink-1`, never
  gray-on-color (Impeccable's #1 ban).
- Dark first; light auto-derives via `prefers-color-scheme`. Both
  must pass WCAG AA on text.

---

## 4. Spacing & layout

4-point grid. Eight tokens. Use them, don't improvise.

```css
--s-1: 0.25rem; --s-2: 0.5rem;  --s-3: 0.75rem;
--s-4: 1rem;    --s-5: 1.5rem;  --s-6: 2rem;
--s-7: 3rem;    --s-8: 4rem;    --s-9: 6rem;
--s-10: 8rem;
```

- Section vertical rhythm: `clamp(--s-8, 6vw + 2rem, --s-10)`.
- Container max-width: **1180px**. Breathing room at both edges.
- Grid: `repeat(auto-fit, minmax(min(280px, 100%), 1fr))` for cards.
- One frame per element. **Never wrap a card in a card.** Impeccable's
  most-cited offence; treat nested cards like nested ternaries.

### Radius
`--r-1: 4px; --r-2: 8px; --r-3: 12px; --r-4: 20px; --r-pill: 999px;`

Use `--r-2` everywhere. `--r-3` for hero blocks. `--r-pill` only on tags.

---

## 5. Composition & hierarchy

Each page has **one dominant element** — the thing the eye lands on first.
For the incident detail page, that is the top hypothesis card with its
confidence and recommended action. Nothing else competes for that role on
that screen.

Information density rules:

1. The top of every screen answers the user's question in <5 seconds.
   Detail discloses below.
2. Scrollable lists fit at least 7 items above the fold on a 13" laptop.
3. Charts get one job. A latency timeline is a latency timeline; don't
   stack three series on it because you can.
4. Tables: monospace numerics, right-aligned, fixed column widths.
   Never let a number column reflow.

---

## 6. Components — the v1 set

The UI is built from a small library. **Resist creating a new primitive
when an existing one composes.**

- `<Page>` — outer shell, top nav, container.
- `<Section>` — content band with consistent vertical rhythm.
- `<Card>` — single bordered surface, never nested. Used for hypothesis,
  evidence group, runbook preview, etc.
- `<Statline>` — label + tabular number + delta indicator. The atom for
  every metric.
- `<HypothesisCard>` — top variant has confidence ring, evidence list,
  action button; alt variants are collapsed by default.
- `<EvidenceItem>` — claim text + source pill that deep-links to the tool
  call result.
- `<Pill>` — status, severity, env. One word, one color token.
- `<Trace>` — vertical timeline of tool calls with timing bars. Used on
  the incident detail page.
- `<TopologyGraph>` — directed graph using `react-flow` or `d3-dag`. Pick
  one and stay.
- `<EmptyState>` — illustration optional, copy mandatory: tell the user
  why they're seeing nothing and what creates content.

### Buttons
- Primary: `--acc` background, `--ink-0` text. One per screen, lives
  next to the dominant element.
- Secondary: ghost (border only).
- Destructive: `--neg` outline, fill on hover. Used for "rollback" only.

---

## 7. Motion

The `impeccable` skill's exact line: bounce/elastic easing feels dated.
The defaults below are non-negotiable.

```css
--ease-out:    cubic-bezier(0.22, 1, 0.36, 1);
--ease-in-out: cubic-bezier(0.65, 0, 0.35, 1);
--d-1: 120ms;
--d-2: 220ms;
--d-3: 360ms;
```

- Hover: `--d-1`, opacity / lightness only. No scale, no translate.
- Page transition: `--d-2`, fade + 4px translate. Never spring.
- Stagger: 40ms between siblings on first load only.
- `prefers-reduced-motion: reduce` always honored — animation duration
  collapses to `0.01ms`.

---

## 8. Iconography

- Library: **Lucide** (open-source, matches Geist visually).
- Stroke width: **1.5** uniformly.
- Size: 16 / 20 / 24 px. Never inline-styled to other sizes.
- Never decorate copy with an icon that doesn't add information.

---

## 9. Page-by-page intent

### `/` Incidents inbox
- One row per incident; severity color is a 4px left border, not a
  colored row background.
- Columns: severity / service / triggered_at / top hypothesis (truncate
  to 1 line) / confidence / action.
- Empty state speaks to onboarding: "no alerts yet — point your OTel
  exporter at /v1/traces to start."

### `/incidents/[id]` Incident detail
- Hero: top hypothesis card. `<Statline>` cluster: time-to-detect,
  time-to-RCA, agent confidence, model used, tokens, cost.
- Below hero, three tabs (URL-driven, not local state):
  `Reasoning trace · Topology · Runbook · Alternatives`.
- The **Reasoning trace** tab is the differentiator — it shows the
  agent's tool calls with input, summarized result, and elapsed time.
  This is what makes the product feel honest.

### `/topology`
- Live graph view. Color nodes by current status (`--pos / --neg /
  --warn / --ink-3` for unknown). Edge thickness = traffic.
- Hover a node → side panel with last 5 events.

### `/runbooks`
- Markdown editor (CodeMirror 6) with live preview.
- One runbook per service. Save persists to disk under `runbooks/`.

### `/settings`
- Tenant switcher (top-right, pill).
- Model selector (`claude-haiku` default; `claude-opus` opt-in).
- Cost ceiling slider (default $0.50 / RCA, hard upper $2.00).
- Telemetry-store driver (sqlite / duckdb / snowflake).

---

## 10. Accessibility

Non-negotiable. Run a contrast and tab-flow audit on every page before
calling it done.

- WCAG **AA** minimum on text; **AAA** on the dominant element of each
  screen.
- Keyboard: every interactive element reachable in tab order; visible
  `:focus-visible` ring (2px `--acc`, 3px offset).
- Screen-reader: `<main>` per page, one `<h1>`, semantic landmarks,
  ARIA only when semantic HTML can't express the thing.
- No reliance on color alone — pair status color with an icon or label.

---

## 11. Stack

- **Next.js 15+, App Router, TypeScript.** No Pages router.
- **Tailwind CSS** for utilities; design tokens declared as CSS custom
  properties (above) so they work outside Tailwind too.
- **shadcn/ui** components as a starting point — but rip out the default
  Inter/zinc/slate palette and rewire to the OKLCH tokens above.
- **Lucide** icons.
- **react-flow** for topology.
- **CodeMirror 6** for runbook editing.
- **Recharts** for time-series. One series per chart.
- **No Framer Motion** in v1. CSS transitions are enough; revisit when
  there's a real animation requirement.

---

## 12. Anti-patterns to refuse on sight

Direct from the two reference docs, pinned here so they're impossible to
miss:

1. **Default Inter font.** Replace with Geist or Inter Tight.
2. **Purple gradient hero.** Replace with a single OKLCH accent or
   plain text on tinted neutral.
3. **Cards inside cards inside cards.** Flatten. One bordered surface
   per group.
4. **Gray text on colored backgrounds.** Use `--ink-0` / `--ink-1` on
   any chromatic surface.
5. **Pure black `#000` or pure gray `#666`.** All neutrals are tinted
   (250 hue in OKLCH).
6. **Bouncy spring motion.** Use the easing curves above.
7. **Two competing accent colors.** One accent. Status colors are not
   accents.
8. **Decorative icons next to plain copy.** Icon must carry meaning.
9. **Skeleton loaders that don't match final content shape.** If you
   show a skeleton, it must be the same dimensions as the loaded result.
10. **Modal-on-modal stacking.** A modal interrupts; two modals
    interrupt the interrupt. Reroute to a page instead.

---

## 13. Quick checklist before merging UI changes

Run all 8 before you mark the PR ready.

- [ ] No new font imports beyond Fraunces / Geist / Geist Mono.
- [ ] No hex or HSL colors anywhere in the diff.
- [ ] No new spacing values outside the 4pt grid tokens.
- [ ] No nested `<Card>` instances.
- [ ] All status communicated by color *and* one of: icon, label, shape.
- [ ] `prefers-reduced-motion` and `prefers-color-scheme` both work.
- [ ] Lighthouse perf ≥ 95, accessibility ≥ 100.
- [ ] One dominant element per screen, eye lands on it in <2s
      (self-test: blink, look at the screen, where does the eye go?).
