# Test strategy

1. Unit: pure parsers, collectors, gates, risk-router fallbacks and replay state.
2. Contract: every API response carries schema version, trace id and timestamp; error codes are stable.
3. Integration: temporary Schema 12 ledger through repository → API → replay/ops DB.
4. Replay: frozen risk, positive, rumor-conflict and official-correction cases; no network; expected terminal label and alert-eligibility transition asserted.
5. Safety: no trading routes; all event/market rows preserve no-trading and leakage invariants.
6. Operations: online backup restores into an isolated temporary database; the encrypted full migration archive additionally passes authenticated decryption, path/link safety, every-file manifest verification, read-only SQLite quick/integrity checks, release inventory and automatic plaintext cleanup; production retains 30 daily and 12 weekly server snapshots, while unit tests exercise bounded pruning with smaller explicit values.
7. Browser E2E: Situation Room, event detail, replay and model page on the live stack.
8. Evidence Agent: primary support becomes `EVIDENCE_READY`; missing exact passages force `INSUFFICIENT`; contradictions force `HUMAN_REVIEW`; object hashes and human overrides persist.
9. Raw-source archive: fixture HTML/PDF bytes are content-addressed and idempotent; HTTP, URL credentials, malformed ports, non-official hosts, redirect escapes, unsupported MIME and oversized responses fail closed; API samples recompute SHA-256 integrity.
10. UI state: named saved Flows normalize and cap status/family/source/query/limit only; browser storage is capped at eight, uses no network call, clears stale event IDs and renders user names through `textContent`.
11. Facets and commands: repository/API facets expose aggregate family/source counts only, preserve read-only/no-trading flags, source filtering is parameterized and exact, fuzzy selectors preserve unknown deep-link values, and command labels/URLs are escaped and encoded.
12. Course gate evolution: a public acceptance report may grow beyond the original 15 checks, but must contain at least the minimum count and every reported check must be literal `true`.
13. Worker freshness: the parent removes the previous cycle report before launching a child, so an import or launch failure cannot reuse stale success evidence.
14. Defense drills: malformed XML, model artifact corruption, backup corruption, evidence-gate transition, official-correction withdrawal and forbidden-route injection.

Every bug fix should first gain a failing test. CI compiles all Python, runs the suite with coverage and scans tracked files for obvious secret assignments and forbidden mutation routes.
