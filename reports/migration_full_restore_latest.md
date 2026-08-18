# Encrypted migration archive — full isolated restore audit

- Verified at: `2026-08-18T09:38:10.552019+00:00`
- Result: **PASS**
- Snapshot: `20260818T083746Z`
- Accepted release: `20260818T080656Z-a39224683399`
- Encrypted bytes: `1547871368`
- Authenticated decrypted archive SHA-256: `fdd140bdec21cb34fed5094438b197ab7d984d257f7dd71908cdf8c1bff91f35`

## Archive proof

- 54,468 archive members and 51,271 regular files scanned.
- 9,273,235,760 uncompressed bytes processed without arbitrary path extraction.
- All 51,270 `MANIFEST.sha256` entries matched.
- Safe path scan: `True`; required files present: `True`.

## Restored databases

- Ledger: Schema 12; quick/integrity `ok` / `ok`; 13,789 events and 14,244 evidence rows.
- Operations: Schema 6; quick/integrity `ok` / `ok`; 7,183 worker cycles and 92 backup runs.
- Both databases were opened read-only/immutable during the audit.

## Migration source consistency

- Bound to a verified full recovery bundle: `True`; legacy contract: `False`.
- Consistency source: `verified_full_recovery_bundle`; mapped files: `24587`.

## Shadow model recovery proof

- Model: `risk-router-v4-c82cfde20465`.
- Blind report: `risk_router_external_blind_v3_report.json`; gate `True`; decision `QUALIFIED_SHADOW`.
- Artifact/card/report SHA-256 chain matched: `True`.
- SHADOW / no-trading: `True` / `True`.

## Optional local evidence model

- Capability declaration present: `True`; legacy contract: `False`.
- Source installed / archive included: `True` / `True`; restore policy: `DISABLED_AFTER_RESTORE`.
- Model unit present: `True`; pinned SHA-256 matched: `True`.

## Safety boundaries

- Trading project included: `False`.
- TLS private keys included: `False`.
- Arbitrary archive paths extracted: `False`.
- Temporary plaintext workspace cleaned: `True`.
