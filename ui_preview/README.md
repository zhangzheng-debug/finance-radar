# Finance Radar latest public-reader UI preview

This preview is a standalone UI concept based on the post-PR #38 public reader
direction on `main` (`8ec7600`).

Open `finance-radar-ui-concept.html` directly in a browser to review the visual
direction. The page uses local sample data only, so it does not require the
backend API to be running.

## Backlog mapping

- Sprint: `Sprint4 · 结论质量证据`
- Primary item: `PBI_34 · 最小质量看板`
- Related item: `PBI_35 · 每个质量数字可点回原始依据`

This submission is a reviewable UI preview for those product directions. It
does not claim that the PBI acceptance conditions or production integration
are complete.

## What changed

- Focuses the first screen on the public reader's four jobs: discover events,
  understand the source claim, check evidence, and inspect optional research
  signals.
- Keeps the public navigation short: event radar, cases, and method.
- Adds UI space for the new public capabilities: citation posture, Qwen risk
  semantics, DeepSeek source interpretation, event-linked asset context, and
  post-publication market reaction windows.
- Removes internal Worker, backup, database and reviewer-progress language from
  the public surface.
- Keeps trading and account language out of the interface.

## Scope

This is a UI preview only. It does not change production Streamlit pages,
FastAPI routes, deployment files, dependency locks, or data contracts.
