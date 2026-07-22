# Risk Router v4 development evaluation

- Model: `risk-router-v4-c82cfde20465`
- Architecture: structured evidence gate + binary semantic router.
- Development rows: `139` `{'RISK_REVIEW': 84, 'NON_TARGET': 55}`
- OOF accuracy / macro F1: `0.914` / `0.910`
- OOF risk recall: `0.917`
- OOF normal-news false-risk: `0.091`
- Development gate: `PASS`
- Blind-v3 was hash-checked for separation only and not inferred.
- Labels are AI rubric adjudications, explicitly not human labels.
- Mode remains SHADOW / NO TRADING.
