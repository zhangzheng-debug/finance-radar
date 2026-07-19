# Finance Radar Risk Router Model Card

- Model version: `risk-router-v1-e1ce020d3445`
- Artifact SHA-256: `84b9186bd2d1c1d16dcc2f37560e034181bcbc257286c458c2bf7c600c4f7a28`
- Mode: shadow only; no trading or alert permission
- Task: Route full-polarity financial event text to RISK_REVIEW, NON_TARGET or ABSTAIN.
- Split: deterministic recent connected-group holdout
- Issuer overlap: 0
- Event-chain overlap: 0
- Coverage: 0.827
- Covered accuracy: 0.957

## Limitations

- The dataset is intentionally rich in negative events and controls, so this is not a general financial-sentiment model.
- Historical labels reflect the current evidence policy and can contain adjudication noise.
- Issuer, source and event-family language may shift over time; drift monitoring is required.
- The model is only a queueing aid; evidence and finality gates remain authoritative.
