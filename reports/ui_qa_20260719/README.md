# Public UI browser acceptance · release 20260718T220657Z

Captured from the public HTTPS endpoint on 2026-07-18 UTC with headless Google
Chrome through Playwright. These images are from the current deployed Calm
Institutional release, not reused from an earlier layout. Every accepted capture
has a sibling JSON diagnostic containing the requested/final URL, viewport,
render counts, text excerpt, browser errors and capture time.

| Surface | Viewport | Evidence | Result |
|---|---:|---|---|
| Situation Room · defense display | 1920×1080 | `home_1920x1080.png` + `.json` | PASS: the full event stream, human queue and source-health rail use the presentation canvas without clipping; zero skeletons, console errors, HTTP errors and page errors |
| Situation Room | 1366×768 | `home_1366x768.png` | PASS: command bar, six-fact status strip, dense UTC event stream and human/source rail are visible; zero skeletons and zero page errors |
| Situation Room | 390×844 | `home_390x844.png` | PASS: navigation collapses, status facts become a two-column grid and event rows remain readable without horizontal clipping |
| Verified Event Workbench · live interaction | 1920×1080 | `event_keyboard_after_jk_1920x1080.png` + `public_interaction_acceptance.json` | PASS: J selects the next visible row and event ID, K returns to the prior row/ID, `/` focuses the actual global-search input, and the complete three-column evidence surface renders |
| Verified Event Workbench | 1366×768 | `event_intelligence_verified_1366x768.png` | PASS: queue, exact P0 evidence, deterministic Evidence Agent summary and independent decision dimensions coexist in one work surface |
| Verified Event Workbench detail | 390×844, scrolled | `event_intelligence_verified_390x844_scrolled.png` | PASS: exact passage, source action, agent claims/edges and audit accordions stack cleanly on narrow screens |
| Replay Lab · completed interaction | 1920×1080 | `replay_completed_1920x1080.png` + `public_interaction_acceptance.json` | PASS: a new persisted replay advances from `1/2 · PENDING` to `2/2 · MET · RISK_REVIEW`; STEP 01 abstains without primary evidence and STEP 02 becomes alert-eligible after P0 evidence |
| Replay Lab | 1366×768 | `replay_lab_1366x768.png` | PASS: frozen case, external-network boundary, step controls and recent evidence are visible; zero skeletons and zero page errors |
| Operations & Model · defense display | 1920×1080 | `operations_model_1920x1080.png` + `.json` | PASS: seven system facts, evidence modes and source-health ledger are simultaneously visible; latest Worker is `SUCCESS`, source errors are 0 and the 24-hour window is honestly `PARTIAL` |
| Operations & Model | 1366×768 | `operations_model_1366x768.png` | PASS after recovery: latest Worker is `SUCCESS`, source errors are 0, and API/ledger/backup/model remain healthy |
| Operations degradation | 1366×768 | `operations_model_degraded_1366x768.png` | PASS as failure evidence: transient FTC HTTP 503 is visibly first in the error table and Worker is honestly `DEGRADED` |
| Adjudication Studio · public read-only | 1920×1080 and 1366×768 | `adjudication_readonly_1920x1080.png` / `adjudication_readonly_1366x768.png` + sibling diagnostics | PASS: 24 unlabeled tasks, dual-review control plane, label deficits and integrity gates render without skeleton/page errors; public write controls are visibly closed |

Release `20260718T220657Z` canonicalizes every nested Streamlit `_stcore`
request and bootstraps fresh secondary-page deep links through an in-session
page switch that restores filters and Event ID. The current 6/6 interaction
report records zero console errors, zero page errors and zero HTTP errors; the
accepted current-state matrix above was refreshed after deployment. The
`operations_model_degraded_1366x768.*` pair remains intentionally historical:
it preserves the real FTC source failure before this routing fix, while the
current Operations captures preserve the successful self-recovery.

The Adjudication Studio was also checked at the API boundary: aggregate status
is public, raw review queue access without an administrator token is HTTP 403,
the public response contains no annotations, and production/freeze flags remain
false. The reproducible 11/11 result is in
`reports/adjudication_v3_public_acceptance.*`.

The reusable capture script is `scripts/capture_public_ui_qa.js`. It rejects the
transient Streamlit state in which the page shell exists but substantive body
content has not arrived, and it waits for all skeletons to clear before capture.
The reusable interaction script is `scripts/verify_public_ui_interactions.js`.
Its machine-readable and reviewer-readable results are
`public_interaction_acceptance.json` and `public_interaction_acceptance.md`.
It waits for the visibly selected event row and complete evidence surface rather
than accepting a transient URL change or partially rendered Streamlit rerun.
