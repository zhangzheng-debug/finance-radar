# Public adjudication workflow acceptance

- Generated: `2026-07-18T20:13:41.038216+00:00`
- Result: **PASS (11/11)**
- Samples: **24**
- Workflow: **NOT_READY_FOR_FREEZE**
- Unauthenticated queue request: **HTTP 403**

## Checks

- [x] `public_health_ok`
- [x] `api_schema_1_1`
- [x] `operations_schema_3`
- [x] `dual_review_capability_advertised`
- [x] `aggregate_status_public`
- [x] `aggregate_status_hides_annotations`
- [x] `public_write_controls_default_closed`
- [x] `no_unauthenticated_queue_read`
- [x] `production_and_blind_freeze_unchanged`
- [x] `public_web_route_rendered`
- [x] `browser_QA_no_runtime_error`

The aggregate progress page is public and read-only. Raw review tasks remain behind the API administrator gate, while the Streamlit write controls are disabled by default. This probe used no administrator token and submitted no review.
