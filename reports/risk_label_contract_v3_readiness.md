# Risk label contract v3 readiness

- Status: **NOT_READY_FOR_BLIND_V2**
- Input: `C:\Users\MR\Desktop\Vibecoder\finance radar\artifacts\risk_router_v2_candidate_manifest.jsonl`
- Rows: 877
- Contract-valid rows: 0
- Production changed: `False`
- No blind-v2 claim: `True`

## Why the current candidate manifest cannot become blind-v2

- `missing:adjudicated_at`: 877
- `missing:adjudicator_id`: 877
- `missing:authority_tier`: 877
- `missing:content_present`: 877
- `missing:entity_group`: 877
- `missing:event_chain_group`: 877
- `missing:evidence_state`: 877
- `missing:materiality`: 877
- `missing:polarity`: 877
- `missing:rationale`: 877
- `missing:reviewer_id`: 877
- `missing:sample_id`: 877
- `missing:source_id`: 877
- `missing:source_lane`: 877
- `missing:source_used_as_label`: 877
- `legacy:preassigned_split`: 877
- `legacy:source_or_corpus_label_basis`: 877

The v3 contract requires content-present dual adjudication across materiality, polarity and evidence state. P0/P1/P2 determine only the deterministic evidence lane; source identity is forbidden as a target label. Splits stay `UNASSIGNED` until validation succeeds, after which entity/event-chain/source groups are frozen without overlap.

Current action: **do not train or freeze blind-v2**. Obtain authentic human content labels first; then rerun this audit.
