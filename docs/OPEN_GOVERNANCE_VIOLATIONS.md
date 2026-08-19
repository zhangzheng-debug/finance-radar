# Open governance violations

This register records known violations that must not be normalized as policy or
silently called an exception. Closing an entry requires evidence and, where the
action is destructive, a separate owner authorization.

## GOV-2026-08-19-01 — Production recovery ciphertext in a public Release

- Status: **OPEN**
- Repository visibility verified 2026-08-19: `PUBLIC`
- Release: `v2026.07.22.2`
- Asset: `finance-radar-migration-20260722T084527Z.tgz.aesgcm`
- Public asset size: `867,634,922` bytes
- Public asset digest reported by GitHub: `sha256:9caeec6a73fcbc54eaa575db1417cef4a8aaa23ba9b8b124fdf187d678437f2f`
- Confirmed plaintext/passphrase exposure: **none found**
- Why still non-compliant: a public GitHub repository cannot make one Release
  asset private; production recovery material belongs in separately private
  storage even when encrypted.
- Current recovery safety evidence: a newer off-host bundle exists at
  `D:\FinanceRadarBackups\20260818T083746Z`; its local full-restore report says
  `PASS` and `offhost-verification.json` says `full_restore_verified=true`.
- Required close order:
  1. independently revalidate the newer off-host bundle and key separation;
  2. copy it to approved private off-host storage;
  3. record storage receipt, digest and restore drill;
  4. obtain action-specific owner authorization to delete the public asset;
  5. delete only the named asset and verify the Release inventory again.
- This repository change performs none of steps 2–5 and does not imply deletion
  authority.
