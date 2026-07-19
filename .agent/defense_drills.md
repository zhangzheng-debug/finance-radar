# Defense fault-injection drills

Run the deterministic drill pack without network access or the trading system:

```powershell
python scripts/run_defense_drills.py
```

The latest report is `reports/defense_drills_latest.json`. On 2026-07-18 all six drills passed:

1. **Malformed official XML** — inject a bare ampersand; strict parsing fails, the narrow repair path activates, and the item text is preserved.
2. **Corrupt model artifact** — load invalid joblib bytes; the load error remains visible while routing degrades to the transparent keyword fallback with `shadow=true` and `no_trading=true`.
3. **Corrupt backup** — attempt an isolated restore from non-SQLite bytes; verification rejects it with `DatabaseError`.
4. **Primary-evidence gate** — replay a P2 rumor followed by a P0 SEC filing; Step 1 is `ABSTAIN`/not alert-eligible, Step 2 is `RISK_REVIEW`/alert-eligible, and the audit row persists.
5. **Official revision withdrawal** — replay an initial P0 SEC risk filing followed by a P0 `CORRECTION` that supersedes it; the system moves from alert-eligible `RISK_REVIEW` to `CONFLICT_REVIEW / ABSTAIN` with no alert.
6. **Forbidden-route guard** — enumerate the generated API routes and assert that order, position, balance, brokerage and trade-execution paths do not exist.

The first corrupt-model rehearsal initially failed because the drill expected an invented runtime label (`transparent_fallback`) instead of the real contract (`fallback`). The system itself degraded safely. The drill was corrected and a regression test was added; this is retained as evidence of diagnose → patch → retest behavior.

## Live improvised-change template

For a teacher-requested field change, keep the scope to one reversible slice:

1. Add the field to the versioned API envelope without renaming existing fields.
2. Add one contract assertion in `tests/test_product_layer.py`.
3. Render it in one Web panel.
4. Run the targeted test, `python -m compileall -q app scripts tests`, and `python scripts/collect_product_acceptance.py`.
5. If any check fails, restore the prior immutable VPS release symlink; never modify the trading project.
