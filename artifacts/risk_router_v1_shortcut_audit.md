# Risk Router v1 shortcut audit

- Model: `risk-router-v1-e1ce020d3445`
- External freeze: `external-blind-v1-2dd91c8b9acf`
- External gate: `FAIL`
- Blind routes: `{"ABSTAIN": 1, "RISK_REVIEW": 39}`
- Mean confidence among RISK_REVIEW routes: 75.7%
- This audit did not train, tune or mutate model v1 or the frozen labels.

## Findings

- The in-domain grouped holdout did not expose the cross-source failure seen on the external set.
- Training text includes event_family, event_type and discovery_source-derived language; top coefficients contain internal taxonomy/control markers.
- The NON_TARGET class is composed of rejected candidates and controls, not a representative sample of ordinary official company and macro news.
- All frozen blind rows used the trained artifact; the positive keyword guardrail was not a general-domain solution.

## Suspected shortcut features

- `word_tfidf__candidate official` -> RISK_REVIEW (+0.5992); markers=candidate official
- `word_tfidf__official sec` -> RISK_REVIEW (+0.5193); markers=official sec
- `word_tfidf__distress_equity_death` -> RISK_REVIEW (+0.4435); markers=distress_equity_death
- `word_tfidf__delisting_or_suspension` -> RISK_REVIEW (+0.3498); markers=delisting_or_suspension
- `word_tfidf__bankruptcyliquidation` -> NON_TARGET (-0.7260); markers=bankruptcyliquidation
- `word_tfidf__source_metadata_control` -> NON_TARGET (-0.4789); markers=source_metadata_control
- `word_tfidf__bankruptcy_liquidation` -> NON_TARGET (-0.4288); markers=bankruptcy_liquidation
- `word_tfidf__bankruptcyliquidation value` -> NON_TARGET (-0.4288); markers=bankruptcyliquidation
- `word_tfidf__bankruptcy_liquidation candidate` -> NON_TARGET (-0.4288); markers=bankruptcy_liquidation
- `word_tfidf__value bankruptcyliquidation` -> NON_TARGET (-0.4288); markers=bankruptcyliquidation
- `word_tfidf__action bankruptcyliquidation` -> NON_TARGET (-0.4288); markers=bankruptcyliquidation

## Locked v2 protocol

- Do not reuse external-blind-v1 as a promotion test after diagnosis.
- Build development hard negatives from separate ordinary official company and macro news.
- Train only on publish-time content fields; remove event_family, event_type, discovery_source and internal control strings from model input.
- Add a coefficient shortcut audit and source-held-out development split before freezing model v2.
- Freeze a new label-first external-blind-v2 only after the v2 artifact and thresholds are locked.
