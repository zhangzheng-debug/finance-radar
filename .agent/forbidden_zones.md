# Forbidden zones and authorship boundary

The following final grading kernels must be designed, handwritten, tested and explained by students after teacher approval:

- `app/core/event_fingerprint.py`: event identity and revision-aware fingerprint.
- `app/core/evidence_gate.py`: claim/evidence authority, independence, support and contradiction.
- `app/core/finality_gate.py`: legal finality, identity conflict, grade cap and no-trading invariant.

Existing AI-assisted code, including `app/models/risk_router.py` and `app/services/replay.py`, is product scaffolding and must not be retroactively claimed as handwritten forbidden-zone work. Until the final kernels exist, replay uses a clearly named baseline evidence state inside `ReplayService`; it is not the final grading kernel.

For each final zone preserve: design note, first student commit, at least 20 focused tests, bug-fix commits, individual walkthrough and teacher approval. If custom zones are not approved, switch to the course A10 fallback rather than misrepresenting authorship.
