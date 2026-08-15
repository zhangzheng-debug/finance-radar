# Report artifact retention record

Date: 2026-08-15 (Asia/Singapore)

## Scope

This cleanup is deliberately limited to generated coverage output and byte-for-byte duplicate PNG files under historical DOCX/UI QA folders. It does not delete unique reports, source evidence, ledgers, release receipts, migration bundles, `latest` report pointers, or Git history.

## Result

- Before cleanup: 323 tracked files under `reports/`, 35,714,627 bytes.
- Exact duplicate analysis: SHA-256 over every tracked report file found 23 duplicate groups.
- Removed: 43 duplicate PNG copies, reclaiming 10,736,625 working-tree bytes.
- Removed: tracked generated `reports/coverage.json` and `reports/coverage.xml` (679,125 bytes combined).
- Retained: at least one byte-identical copy for every removed PNG hash. Release-stamped `docx_qa_v5_1_release_20260719T032300Z` was preferred, then `docx_qa_v5_1_final`, then an older release-stamped copy; descriptive current UI paths were retained where appropriate.
- Added ignore rules for root `coverage.xml` and `coverage.json` so CI output does not return to source control.

## Recovery and boundary

The removed files remain recoverable from Git history. This is a forward cleanup only: no history rewrite, force-push, release deletion, or destruction of unique evidence is permitted. Similar-looking files with different hashes were retained.

The remaining semantic duplicates such as dated Gate-0 output plus its `latest` pointer are intentional navigation aliases and were not removed merely because their contents currently match.
