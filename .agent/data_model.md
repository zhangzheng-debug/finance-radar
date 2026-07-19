# Data model

The authoritative research ledger stays at Schema 12.

- `raw_observations`: immutable first capture per source/external id.
- `source_revisions`: append-only edit/delete/new history.
- `canonical_events`: current event pointer with `no_trading=1` hard constraint.
- `event_versions`: append-only event state and facts.
- `event_evidence`: event-to-exact-passage relation; automatic verification is prohibited.
- `event_assessments`: severity, credibility and scored rationale.
- `event_asset_impacts`: explainable asset relation; still `no_trading=1`.
- `market_snapshots`: read-only price observation.
- `event_market_metrics`: post-event audit only; model-feature flag must stay 0.
- `pipeline_jobs`, `runtime_leases`, `source_cursors`, `alert_outbox`: durable operations primitives.

The separate operations database is Schema 3 and holds `runtime_state`, `replay_runs`, `model_runs`, `worker_cycles`, `backup_runs`, `agent_decisions`, `evidence_objects`, `evidence_object_links`, `human_overrides`, and the dual-review adjudication tables. It may be reset without destroying the event ledger. Both databases run WAL and busy timeouts.

Exact evidence passages and bounded official-source snapshots are written to an immutable content-addressed filesystem. Paths follow `<sha256-prefix>/<sha256>.<ext>`; the operations database stores hash, MIME type, byte length, source URL, fetch time and event/evidence links. `object_kind=EXACT_EXCERPT` covers text passages; `object_kind=SOURCE_SNAPSHOT` covers raw HTML/PDF. Source snapshots are admitted only from registered official HTTPS domains after redirect revalidation, are capped at 10 MiB, and retain `auto_verification_allowed=false`, `allowed_as_model_feature=false`, and `no_trading=true`. The server keeps this store inside the shared persistent data volume and the migration archive includes it.
