# Risk Router v3 AI label audit

- AI adjudicator: `codex_gpt5_evidence_policy_v1`
- Provenance: `AI_RUBRIC_ADJUDICATOR_NOT_HUMAN`; these are not represented as human labels.
- Total / development / frozen blind rows: `1869` / `544` / `80`
- Development labels: `{"ABSTAIN":400,"NON_TARGET":60,"RISK_REVIEW":84}`
- Blind labels: `{"ABSTAIN":20,"NON_TARGET":30,"RISK_REVIEW":30}`
- Blind source groups: `16`
- Blind dataset SHA-256: `35fe0a851d63f2f34f2d25210cff07ce476c1c95b5eb148f32f0a94d64b323b2`
- Policy SHA-256: `828d5df53c1d66db10da980b8895b22a8a3d774984c509a9ad6341120d2d27a1`

Each row is adjudicated on materiality, polarity and evidence state. Source identity and event taxonomy are retained only in audit context and are excluded from learned text.
