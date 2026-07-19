# Event Ledger Import

Generated: `2026-07-16T17:51:27.614199+00:00`

- Database: `C:\Users\MR\Desktop\Vibecoder\finance radar\data\finance_radar.sqlite3`
- Backup before first migration: `C:\Users\MR\Desktop\Vibecoder\finance radar\data\backups\finance_radar_before_event_ledger_20260715T211307Z.sqlite3`
- Imported queue rows: `932`
- Canonical events: `1160`
- Raw observations: `3556`
- Event versions: `2081`
- Evidence rows: `2386`
- Post-event market metrics: `1898`
- Pipeline jobs: `1160`
- No-trading violations: `0`
- Auto-verification violations: `0`
- Market-metric scope violations: `0`

## Event Status

- `candidate`: `263`
- `rejected`: `351`
- `verified`: `546`

## Job Status

- `COMPLETED_DISCOVERY_FILTERED`: `7`
- `COMPLETED_DUPLICATE_CLUSTER`: `1`
- `COMPLETED_MANUAL_ADJUDICATION`: `897`
- `PENDING_EVIDENCE_REVIEW`: `139`
- `PENDING_PRIMARY_EVIDENCE`: `116`

The import is idempotent. It does not enqueue Telegram messages, mutate D:/short, or create any trading/order capability.
