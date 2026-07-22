# Encrypted migration archive — full isolated restore audit

- Verified at: `2026-07-22T08:55:33.575145+00:00`
- Result: **PASS**
- Snapshot: `20260722T084527Z`
- Accepted release: `20260722T084500Z`
- Encrypted bytes: `867634922`
- Authenticated decrypted archive SHA-256: `bf554e45178441c889c4c5d4c5f5be68ca14d883e860e7b7f7aab319aad874e3`

## Archive proof

- 20,807 archive members and 18,645 regular files scanned.
- 1,953,496,026 uncompressed bytes processed without arbitrary path extraction.
- All 18,644 `MANIFEST.sha256` entries matched.
- Safe path scan: `True`; required files present: `True`.

## Restored databases

- Ledger: Schema 12; quick/integrity `ok` / `ok`; 1,872 events and 3,101 evidence rows.
- Operations: Schema 4; quick/integrity `ok` / `ok`; 1,162 worker cycles and 54 backup runs.
- Both databases were opened read-only/immutable during the audit.

## Shadow model recovery proof

- Model: `risk-router-v4-c82cfde20465`.
- Blind report: `risk_router_external_blind_v3_report.json`; gate `True`; decision `QUALIFIED_SHADOW`.
- Artifact/card/report SHA-256 chain matched: `True`.
- SHADOW / no-trading: `True` / `True`.

## Safety boundaries

- Trading project included: `False`.
- TLS private keys included: `False`.
- Arbitrary archive paths extracted: `False`.
- Temporary plaintext workspace cleaned: `True`.
