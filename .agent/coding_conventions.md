# Coding conventions

- Python 3.12+, UTF-8, type hints on public functions.
- Product code lives under `app/`; legacy research/collector entrypoints remain under `scripts/`.
- API queries go through `LedgerRepository`; do not scatter new raw SQL through Web pages.
- Every SQLite connection must close explicitly; context-managed transactions alone are insufficient.
- Time is stored as timezone-aware ISO 8601 UTC.
- External writes require an explicit flag and must be idempotent.
- Never add account, balance, order, position or execution capability.
- Market outcomes remain audit-only and cannot enter discovery rank or model features.
- All new response shapes include schema version, trace id and generation time.
- Do not claim AI-assisted files as student-authored forbidden-zone work.
