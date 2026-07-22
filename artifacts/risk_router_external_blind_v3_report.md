# Risk Router external blind v3

- Freeze / model: `external-blind-v3-7e5eac882b03` / `risk-router-v4-c82cfde20465`
- Architecture: structured evidence gate + high-precision semantic policy + binary small model.
- Rows / labels: `80` / `{'ABSTAIN': 20, 'NON_TARGET': 30, 'RISK_REVIEW': 30}`
- Full accuracy / macro F1: `0.963` / `0.967`
- Full risk recall / normal false-risk: `0.967` / `0.067`
- Full abstain recall: `1.000`
- Semantic macro F1 / risk recall: `0.950` / `0.967`
- Blind gate: `PASS`; decision `QUALIFIED_SHADOW`
- Blind-v2 FAIL remains preserved; blind-v3 is disjoint and was frozen before v4 inference.
- Frozen blind-v3 file remains prediction-free.
- Labels are AI rubric adjudications, explicitly not human labels.
- Mode remains SHADOW / NO TRADING.
