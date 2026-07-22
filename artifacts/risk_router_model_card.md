# Finance Radar Risk Router Model Card

- Model: `risk-router-v4-c82cfde20465`
- Artifact SHA-256: `1f1d2c1bbfeaf2fd22280474787b89c717646d4dd651472fca57514f1a0d695d`
- Architecture: structured evidence gate + high-precision semantic policy + binary small model
- Status: `QUALIFIED_SHADOW`; no trading and no automatic verification
- Labels: AI rubric adjudications, explicitly not human labels
- Development macro F1 / risk recall: `0.910` / `0.917`
- Blind-v3 full accuracy / macro F1: `0.963` / `0.967`
- Blind-v3 risk recall / normal false-risk: `0.967` / `0.067`
- Blind-v3 ABSTAIN recall: `1.000`
- Blind-v2 FAIL remains published as predecessor failure evidence.

## Limitations

- AI rubric labels are not independent human double adjudication.
- The semantic model must never run as if unreviewed discovery text were primary-supported.
- The model is a shadow queue aid, not a fact verifier, sentiment engine, or trading model.
