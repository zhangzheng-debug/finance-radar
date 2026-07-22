# Risk Router v4 dataset audit

- Architecture: structured evidence gate + binary semantic router.
- Blind-v2 is preserved as an exposed failed diagnostic and is not reused as a blind test.
- Development rows: `139` `{'RISK_REVIEW': 84, 'NON_TARGET': 55}`
- Exposed v2 substantive rows reused in development: `50`
- Frozen blind-v3: `external-blind-v3-7e5eac882b03`; rows `80`; labels `{'ABSTAIN': 20, 'NON_TARGET': 30, 'RISK_REVIEW': 30}`
- Blind-v3 source groups: `9`
- All leakage counts: `{'event_id': 0, 'entity_group': 0, 'event_chain_group': 0, 'near_duplicate': 0, 'exposed_v2_event': 0, 'exposed_v2_entity': 0, 'exposed_v2_near_duplicate': 0}`
- Labels are auditable AI rubric adjudications, explicitly not human labels.
- Mode remains SHADOW / NO TRADING.
