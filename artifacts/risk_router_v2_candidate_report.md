# Risk Router v2 candidate report

- Model: `risk-router-v2-candidate-3629350054e0`
- Status: `REJECTED_CANDIDATE_NOT_DEPLOYED`
- Content-only rows: `823`
- Total rows with hard negatives: `877`
- Development coverage / covered accuracy: `40.4%` / `78.5%`
- ECB/EIA source-held-out coverage / accuracy: `4.2%` / `0.0%`
- Forbidden shortcut hits in top coefficients: `0`
- Development candidate gate: `FAIL`
- Legacy blind-v1 diagnostic false-risk rate: `5.0%`
- Legacy blind-v1 diagnostic risk recall: `35.0%`
- Promotion: `REMAIN_SHADOW`; this artifact is not deployed.

v2 removes event family, event type and discovery source from learned text and strips taxonomy strings leaked into legacy observation text. Microsoft and Apple official posts are training hard negatives; ECB/EIA are held out by source. Exact legacy-blind rows are excluded from development data.

The development candidate gate failed, so no blind-v2 was frozen and no deployment was attempted. The legacy blind remains diagnostic only after its first failure; its input contract is invalid for evidence-stage promotion because all 20 risk rows are title-only.
