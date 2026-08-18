# Finance Radar 2026.08.18.2

This release fixes the deployment liveness contract and closes the Windows
off-host recovery gaps. It adds no trading, brokerage, order, position, balance
or external-message authority.

## Activation safety

- `/api/v1/live` proves that the API process and routing stack are responsive
  without opening or scanning either production database.
- The transactional installer uses that bounded probe during cutover. The full
  `/api/v1/health` assessment remains unchanged for operational diagnosis.
- The previous candidate timed out only because its five-second probe invoked a
  database-wide count endpoint repeatedly. The installer rolled back cleanly,
  all old services resumed, and the verified pre-cutover recovery bundle was
  retained under root-only failed-cutover custody.

## Off-host recovery

- The daily Windows task runs hidden and non-interactively, so it cannot keep
  opening terminal windows on the desktop.
- Encrypted archives default to `D:\FinanceRadarBackups`; the passphrase
  defaults to `D:\FinanceRadarRecovery\finance-radar-backup-passphrase.txt` and
  is rejected if placed under the ciphertext directory.
- Exactly one daily off-host copy is retained. Older directories are removed
  only after the new archive passes transport hashing, authenticated-encryption
  round trip and an isolated full restore audit.
- Detailed verification receipts remain local to the operator workstation.
  `/radar/offhost-status.json` is denied at the public edge.

## Verification before release

- Local suite: `680 passed, 5 skipped`
- PowerShell parser: all three off-host task scripts `PASS`
- systemd installer shell syntax: `PASS`
- Whitespace and source-diff gate: `PASS`

The exact production release identity, post-cutover recovery receipt and live
health evidence are recorded only after deployment; this document does not
claim that an uninstalled commit is already running on AWS.
