# Public UI interaction acceptance

- Result: **PASS**
- Accepted at: `2026-07-18T22:09:32.413Z`
- Endpoint: `https://radar.167-172-69-16.sslip.io:8443/radar`
- Browser: headless Google Chrome through Playwright
- Viewport: `1920x1080`
- Replay run: `replay-01b4a5c94aca47258212edfade6a2c89`

| Check | Result | Evidence |
|---|---|---|
| `event_selected_on_load` | PASS | FR-LIVE-27b97a3819bea4d0743ab72686465b0d |
| `keyboard_j_selects_next_event` | PASS | FR-LIVE-27b97a3819bea4d0743ab72686465b0d -> FR-LIVE-5e3f0f7f56bdc5f42bd18b201e401ab9; VERIFIED 07-16 03:36 — · U.S. Central Command / M/T Belma -> VERIFIED 07-16 03:33 — · SBA Communications Corporation |
| `keyboard_k_selects_previous_event` | PASS | FR-LIVE-5e3f0f7f56bdc5f42bd18b201e401ab9 -> FR-LIVE-27b97a3819bea4d0743ab72686465b0d |
| `slash_focuses_global_search` | PASS | focused=true |
| `replay_starts_at_first_step` | PASS | run_id=replay-01b4a5c94aca47258212edfade6a2c89 |
| `replay_completes_with_expected_decision` | PASS | progress=2/2 expectation=MET decision=RISK_REVIEW |

The test validates both the URL transition and the visibly selected event row for J/K. The slash check validates the actual focused DOM element. Replay is advanced one evidence step at a time and accepted only after `STEP 02`, `2/2`, `MET`, and `RISK_REVIEW` are simultaneously visible.

- Console errors: `0`
- Page errors: `0`
- HTTP errors: `0`

A PASS requires every interaction check plus zero console errors, zero page errors, and zero HTTP 4xx/5xx responses.
