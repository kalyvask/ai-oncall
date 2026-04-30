"""Static HTML export of an RcaReport.

Used for blameless post-mortems and external sharing. The aesthetic intentionally
mirrors UI_DESIGN.md (OKLCH neutrals + one teal-cyan accent, Geist + Fraunces).
The full Next.js UI in `web/` builds on the same tokens.
"""

from __future__ import annotations

import html

from ai_oncall.models import RcaReport

CSS = """
:root {
  --ink-0: oklch(98% 0.005 250); --ink-1: oklch(93% 0.008 250);
  --ink-3: oklch(60% 0.015 250); --ink-5: oklch(28% 0.022 250);
  --ink-7: oklch(15% 0.020 250); --ink-9: oklch(8% 0.015 250);
  --acc:   oklch(72% 0.13 200);  --neg:   oklch(70% 0.18 22);
  --warn:  oklch(82% 0.14 80);   --pos:   oklch(74% 0.16 155);
  font-family: Geist, system-ui, -apple-system, sans-serif;
}
@media (prefers-color-scheme: dark) {
  body { background: var(--ink-9); color: var(--ink-1); }
  .card { background: var(--ink-7); border-color: var(--ink-5); }
}
@media (prefers-color-scheme: light) {
  body { background: var(--ink-0); color: var(--ink-9); }
  .card { background: white; border-color: var(--ink-1); }
}
body { margin: 0; padding: 2rem; line-height: 1.55; }
main { max-width: 64ch; margin: 0 auto; }
h1 { font-family: Fraunces, Georgia, serif; font-weight: 600; line-height: 1.02; }
.card { border: 1px solid; border-radius: 8px; padding: 1.25rem; margin-bottom: 1rem; }
.confidence { font-feature-settings: "tnum"; color: var(--acc); font-weight: 600; }
.evidence { padding-left: 1.25rem; }
.evidence li { margin-bottom: 0.25rem; }
.action { background: oklch(72% 0.13 200 / 12%); padding: 0.75rem 1rem; border-radius: 4px; font-family: 'Geist Mono', ui-monospace, monospace; }
.escalate { color: var(--warn); font-weight: 600; }
.meta { color: var(--ink-3); font-size: 0.875rem; }
"""


def render(report: RcaReport) -> str:
    top = report.hypotheses[0]
    alt_html = "".join(_alt_card(h) for h in report.hypotheses[1:])
    escalate = ""
    if report.escalation and report.escalation.should_escalate:
        escalate = f'<p class="escalate">⚠️ Escalation suggested: {html.escape(report.escalation.reason or "")}</p>'
    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>RCA — {html.escape(report.alert.title)}</title>
<style>{CSS}</style>
</head>
<body><main>
<h1>{html.escape(report.alert.title)}</h1>
<p class="meta"><code>{html.escape(report.alert.service)}</code> · {html.escape(report.alert.severity)} · tenant <code>{html.escape(report.tenant_id)}</code></p>

<section class="card">
  <p><strong>Top hypothesis:</strong> <code>{html.escape(top.root_cause_service)}</code>
     <span class="confidence">{int(top.confidence * 100)}%</span></p>
  <p>{html.escape(top.reasoning)}</p>
  <ul class="evidence">{"".join(f"<li>{html.escape(e.claim)} <span class=meta>({html.escape(e.source)})</span></li>" for e in top.evidence)}</ul>
  <p class="action">{html.escape(top.recommended_action)}</p>
  {escalate}
</section>

<h2 style="font-family: Fraunces, serif;">Alternatives</h2>
{alt_html}

<p class="meta">Generated {report.generated_at.isoformat()} · model <code>{html.escape(report.model.id)}</code></p>
</main></body></html>"""


def _alt_card(h) -> str:  # type: ignore[no-untyped-def]
    return f"""<section class="card">
  <p><strong>{html.escape(h.root_cause_service)}</strong>
     <span class="confidence">{int(h.confidence * 100)}%</span></p>
  <p>{html.escape(h.reasoning)}</p>
  <ul class="evidence">{"".join(f"<li>{html.escape(e.claim)}</li>" for e in h.evidence)}</ul>
  <p class="action">{html.escape(h.recommended_action)}</p>
</section>"""
