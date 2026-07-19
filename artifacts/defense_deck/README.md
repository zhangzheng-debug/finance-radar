# Finance Radar defense deck

Deliverable: `finance-radar-defense-deck-v1.pptx`

- Slides: 12, 16:9
- Visual system: Calm Institutional, aligned with the deployed Web terminal
- Primary evidence: four current public Chrome screenshots, public acceptance,
  deterministic Replay, local Evidence Agent evaluation, external-blind failure,
  V3 label gate, operations/migration reports and the offline evidence pack
- Presenter notes: included on every slide
- SHA-256: `6ebf1be17d4d4a031966b8e1059e2606470459cdb023fbdc45370c2b640ad8ce`

## Narrative

The deck moves from the credibility problem to the evidence-first architecture,
then proves the live system, event workbench and deterministic replay. It treats
the V1/V2 model failures as governance evidence rather than hiding them, shows
the accepted server-migration recovery point, and closes with the remaining
human/time gates.

For a short defense, use slides 1, 4, 5, 6, 9, 10 and 12. The full 12-slide
version is suitable for a longer technical review.

## QA

- Exported with `@oai/artifact-tool` as editable PowerPoint content.
- Rendered through the bundled PowerPoint-compatible renderer at 1600×900.
- All 12 rendered slides inspected individually at full size.
- Slide 9 category-label truncation found in the first render and corrected.
- Final `slides_test.py`: `Test passed. No overflow detected.`
- No unresolved placeholders, accidental overlaps, clipped titles or repeated
  screenshots remain.
