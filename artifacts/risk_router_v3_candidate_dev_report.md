# Risk Router v3 development evaluation

- Model: `risk-router-v3-240814bae596`
- Labels: AI rubric adjudications, explicitly not human labels
- Rows: `544`; grouped five-fold OOF; group overlap `0`
- Threshold selected without blind predictions: `0.00`
- Risk-rescue floor / margin selected on development only: `0.15` / `0.1`
- Accuracy / macro F1: `0.958` / `0.932`
- Risk recall: `0.857`
- Normal-news false-risk rate: `0.017`
- Abstain recall: `0.988`
- Development gate: `PASS`
- Blind v2 was hash-checked for separation only and was not inferred during training.
- Mode remains SHADOW / NO TRADING.
