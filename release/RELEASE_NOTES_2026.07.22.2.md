# Finance Radar 2026.07.22.2

This release closes the model-governance and off-host-recovery gaps without
adding any trading or order-execution capability.

## Runtime

- AWS application release: `20260722T084500Z`
- Model: `risk-router-v4-c82cfde20465`
- Architecture: structured evidence gate plus binary semantic router
- Mode: `SHADOW`; `no_trading=true`
- Frozen blind-v3: 80 rows, 96.25% accuracy, 96.67% downside-risk recall,
  6.67% normal-news false-risk rate, and 100% ABSTAIN recall

The predecessor blind-v2 failure remains in the repository. It is not erased
or relabeled: the redesign makes evidence sufficiency a deterministic gate
instead of asking the text model to infer it.

## Recovery proof

- Encrypted snapshot: `finance-radar-migration-20260722T084527Z.tgz.aesgcm`
- Accepted release: `20260722T084500Z`
- Restored ledger: 1,872 events and 3,101 evidence rows
- Restored operations: 1,162 worker cycles and 2,236 evidence objects
- Archive manifest: all 18,644 file hashes matched
- Model artifact, SHA declaration, model card, and blind-v3 report hashes match
- Trading project and TLS private keys are absent from the archive

Exact sizes and SHA-256 values are in `release/backup-20260722.json`.

## Validation

- Local suite: `392 passed, 17 subtests passed`
- AWS API, Web terminal, worker, and backup timer: active
- Public API reports the exact v4 artifact hash and `QUALIFIED_SHADOW`
- Latest worker cycle: `SUCCESS`
- Ledger boundary violations: zero

AI rubric labels are explicitly identified as AI-generated and are not
represented as independent human double adjudication.
