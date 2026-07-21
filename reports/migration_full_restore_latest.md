# Encrypted migration archive — full isolated restore audit

- Verified at: `2026-07-21T18:59:24.850820+00:00`
- Result: **PASS**
- Snapshot: `20260721T185507Z`
- Accepted release: `20260721T184054Z`
- Encrypted bytes: `779640430`
- Authenticated decrypted archive SHA-256: `f04d4407bc86b7286ba54974355c4e6e62a0fd8c6169cf0ca8e192b8cff1ebc7`

## Archive proof

- 14,353 archive members and 12,779 regular files scanned.
- 1,610,929,297 uncompressed bytes processed without arbitrary path extraction.
- All 12,778 `MANIFEST.sha256` entries matched.
- Safe path scan: `True`; required files present: `True`.

## Restored databases

- Ledger: Schema 12; quick/integrity `ok` / `ok`; 1,669 events and 2,399 evidence rows.
- Operations: Schema 4; quick/integrity `ok` / `ok`; 992 worker cycles and 48 backup runs.
- Both databases were opened read-only/immutable during the audit.

## Safety boundaries

- Trading project included: `False`.
- TLS private keys included: `False`.
- Arbitrary archive paths extracted: `False`.
- Temporary plaintext workspace cleaned: `True`.
