# Encrypted migration archive — full isolated restore audit

- Verified at: `2026-07-19T05:01:18.739119+00:00`
- Result: **PASS**
- Snapshot: `20260719T045536Z`
- Accepted release: `20260719T044852Z`
- Encrypted bytes: `788301307`
- Authenticated decrypted archive SHA-256: `ac3ed8ba2a1ebd0f90eddfff921b39f683f17a397bfa9b2d6e074a467b24c1a5`

## Archive proof

- 11,091 archive members and 9,861 regular files scanned.
- 1,559,757,804 uncompressed bytes processed without arbitrary path extraction.
- All 9,860 `MANIFEST.sha256` entries matched.
- Safe path scan: `True`; required files present: `True`.

## Restored databases

- Ledger: Schema 12; quick/integrity `ok` / `ok`; 1,194 events and 2,394 evidence rows.
- Operations: Schema 3; quick/integrity `ok` / `ok`; 262 worker cycles and 35 backup runs.
- Both databases were opened read-only/immutable during the audit.

## Safety boundaries

- Trading project included: `False`.
- TLS private keys included: `False`.
- Arbitrary archive paths extracted: `False`.
- Temporary plaintext workspace cleaned: `True`.
