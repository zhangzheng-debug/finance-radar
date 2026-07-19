# Public UI browser QA

Event Workbench P3 addendum (2026-07-18 15:19 UTC): release `20260718T151927Z` adds bounded Previous/Next controls, J/K and arrow navigation, `/` search focus, complete `flow/family/q/limit/event_id` URL state, an actionable empty-result reset, visible keyboard focus, ARIA status semantics, reduced-motion handling and a non-leaking outage card. Three automated page-level AppTests use deterministic fake API responses and verify that Next changes the selected event, an empty saved view resets without stale state, and an unavailable API does not reveal its internal target or development command. A separate real local-API AppTest also clicked Next and changed the selected live event ID without an exception. The browser-control interface remained unavailable, so no claim is made that the new keyboard behavior or 1920x1080 layout has fresh image evidence.

Replay control addendum (2026-07-18 14:24 UTC): Streamlit AppTest against a temporary local operations database rendered `Run frozen replay`, `Next step`, `Show all` and `Reset history` without exceptions. A real Run click set `visible_steps=1`; a real Next-step click advanced it to `visible_steps=2`. Browser image evidence for these new controls remains pending because the browser-control interface was unavailable in this run.

Verified against releases `20260718T120609Z`, `20260718T124833Z` and `20260718T130548Z` on 2026-07-18 with desktop Google Chrome through Playwright.

## Acceptance matrix

| Surface | Viewport | Evidence | Result |
|---|---:|---|---|
| Situation Room | 1366x768 | `home_1366x768.png` | PASS: command bar, compact SLO strip, event table, queue chart and safety boundary render correctly |
| Event Workbench | 1366x768 | `event_intelligence_1366x768.png` | PASS: three-column queue/evidence/context layout remains readable |
| Replay Lab | 1366x768 | `replay_lab_1366x768.png` | PASS: deterministic case selector and replay evidence are visible without scrolling |
| Operations & Model | 1366x768 | `operations_model_1366x768.png` | PASS: SLO strip, mode controls, source-health table and tabs render correctly |
| External blind governance | 1366x768 | `operations_model_external_blind_1366x768.png` | PASS: frozen set metrics, 95% false-risk failure banner, source breakdown and REMAIN_SHADOW guard are visible |
| 22-source operations table | 1366x768 | `operations_sources_22_1366x768.png` | PASS after waiting for Streamlit canvas render: source-health table is populated and source errors remain zero |
| New official feeds | 1366x900, table scrolled | `operations_sources_new_feeds_1366x900.png` | PASS: ECB press/statistics, EIA press and NVIDIA newsroom rows render with P0/P1 authority and success state |
| Situation Room | 390x844 | `home_mobile_390x844.png` | PASS: navigation collapses, command bar wraps, metrics become a two-column grid, wide data remains horizontally scrollable |
| Event Workbench | 390x844 | `event_intelligence_mobile_390x844.png` | PASS after remediation: filter controls stack without clipping |
| Event Workbench detail | 390x844, main scroll 780px | `event_intelligence_mobile_scrolled.png` | PASS after remediation: the 25-event queue is an independent 480px scroll panel and no longer pushes the evidence matrix several screens down |

The first narrow-screen run exposed the unbounded event queue. Release `20260718T120609Z` fixed it with a bounded independently scrollable queue. This is a real Chromium render check, separate from the four-page Streamlit AppTest regression.

The first release-`20260718T130548Z` source-table screenshot was taken while the canvas was still a loading skeleton. The final evidence waits for five dataframes, nine canvases and zero Streamlit skeletons before capture; no DOM-text claim is made for canvas-rendered rows.
