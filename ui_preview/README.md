# Finance Radar latest public-reader UI preview

This preview is a standalone UI concept based on the final 2026.08.31.3
production line after PR #58 on `main` (`76caae3`).

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
- Adds a restrained login screen and safe logout affordance that match the
  public reader visual language.
- Aligns the model block with the production-approved Qwen hybrid semantic
  fields: polarity, materiality, and adverse strength.
- Removes internal Worker, backup, database and reviewer-progress language from
  the public surface.
- Keeps trading and account language out of the interface.

## Scope

The HTML file remains a local sample-data preview. This branch also adds a
server-side, fail-closed login gate to the production Streamlit public reader.
Credentials are configured as a username plus PBKDF2 verifier in a root-only
server file; no plaintext or default password is committed. FastAPI routes,
dependency locks, model artifacts, and data contracts are unchanged.
