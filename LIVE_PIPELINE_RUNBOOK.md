# Finance Radar Live Pipeline Runbook

## Purpose

Run an active, repeatable event-discovery cycle even when there are no rare S-grade events. The live path and historical Sharadar research path remain separate but share the same event ledger.

## 2026-07-16 current research state

- Historical adjudications: 614 total, 346 verified and 268 rejected controls.
- The current 150-row Sharadar queue has completed SEC scanning and collapses to 140 review threads; all 140 are adjudicated and triage is empty. Official filings create distinct events at their true legal dates instead of being attached to later price, vendor or current-identity proxies.
- Canonical ledger: 787 events, 451 verified, 268 rejected and 68 candidates.
- Evidence state: 2,753 immutable raw observations, 710 source revisions, 1,894 evidence rows and 1,527 event versions across 18 sources.
- Durable adjudications that rotated out of the active queue are replayed into SQLite. A changed human decision appends an event version and updates the canonical current state.
- Validation: 223 tests pass; the live-pipeline audit passes with all 19 safety/integrity violation counters at zero.
- Next research work: generate the next balanced, auditable Sharadar batch while pushing the 268 rejected-control patterns upstream into discovery. Preserve event-time identity, financing, split mechanics, petition, plan, delisting consequence and terminal old-common outcome as separate facts.

## One-cycle command

Dry run, including external read-only collection and market observation:

```powershell
python scripts/run_live_cycle.py --timeout 30
```

Allow delivery of newly eligible Telegram outbox rows:

```powershell
python scripts/run_live_cycle.py --timeout 30 --send
```

`--send` does not widen the event gate. Only fresh, verified A/A++/S events with primary evidence can enter the outbox.

## Cycle stages

1. Poll Federal Reserve, SEC current filings/litigation/trading suspensions, CFTC enforcement, FDA MedWatch, FTC press, FDIC press and BLS key-series endpoints with independent cursors.
2. Fetch OpenNews free `macro`, `ai`, and `web3` feeds as discovery-only secondary coverage.
3. Write immutable first captures to `raw_observations`, append edits to `source_revisions`, and read current content through `latest_source_content`.
4. Apply conservative source-aware rules; P0 official observations still remain candidates.
5. Fetch SEC filing index, primary document, and selected EX-99 exhibits to refine generic filing types.
6. Fetch trusted official HTML pages and extract a review passage; machine extraction remains `machine_extracted_unreviewed` and cannot verify or grade an event.
7. Rank pending reviews by source-aware meaning and evidence readiness; the score changes review order only.
8. Build official-source evidence routes for every pending live candidate.
9. Re-apply reviewed P0/P1 adjudication rows idempotently.
10. Archive bounded raw HTML/PDF bytes for reviewed official evidence into the immutable SHA-256 object store. Reuse the trusted-page cache when present; otherwise fetch only HTTPS from the registered official domain, revalidate the final redirect host, cap at 10 MiB and record cross-domain links as policy skips. A snapshot never verifies an event and never becomes a model feature.
11. Apply explicit event-entity and event-asset relations.
12. Observe reviewed crypto proxies through Binance's public market-data-only spot endpoint and reviewed non-crypto proxies through Twelve Data. A missing Twelve Data key does not block public crypto observations.
13. Treat the first successful real observer snapshot as the baseline; schedule T+5m/T+30m/T+1d follow-ups with bounded grace. If a window is missed, persist `MISSED_WINDOW` and never substitute a current quote. Compute returns only from actual paired captures, keep them post-event-audit-only, and never expose them as model features.
14. Enqueue fresh verified alerts; send only when `--send` is explicit.

The `runtime_leases` table prevents concurrent live cycles. The `alert_delivery_leases` table separately prevents concurrent Telegram deliveries.

## Current verified runtime state

- Verified on 2026-07-16 after full tail-queue closure: schema 12, 18 registered sources, 2,753 immutable raw observations, 710 append-only revisions, 1,894 evidence rows and 1,527 event versions.
- The ledger contains 787 canonical events: 451 verified, 268 rejected controls and 68 candidates. No event or asset relation permits trading.
- Live manual-review queue: three events. CBIO and Q32 require actual offering-closing evidence; Obsidian requires actual debt-closing evidence. The historical triage queue is empty.
- Newly active official adapters: CFTC enforcement (5 observations), FDA MedWatch (11), FTC press (10), SEC litigation releases (7), SEC trading suspensions (1) and FDIC press releases (8).
- Trusted-page enrichment only follows allow-listed official hosts. Its evidence rows set `auto_verification_allowed=0`; link-only and no-relevant-passage outcomes remain visible for manual follow-up.
- Current top queue: CBIO and Q32 offering closings, then Obsidian debt closing. Happy City's SEC trading suspension, SBA Communications' debt refinancing and the CENTCOM blockade-enforcement event have been manually adjudicated from primary evidence.
- CBIO, Q32 and Obsidian remain unresolved until closing evidence exists. Signing, pricing or an expected closing date is not treated as completion.
- The latest dry run completed with no source errors, no new candidates, no Telegram delivery and no market-order path. OpenNews made a one-time transition from provider-payload hashes to semantic hashes; an immediate follow-up poll left the OpenNews revision timestamp unchanged, confirming stable metadata-only deduplication.
- Historical Sharadar research remains a prior/candidate generator rather than a real-time source. The current 150-row queue collapses to 140 review threads; SEC scanning and all 140 adjudications are complete. The durable historical adjudication ledger contains 614 rows: 346 verified and 268 rejected controls.
- The final 16 price-only threads were all rejected as event labels. OST, MGN and ELPW financing disclosures plus the Town Sports and Ability judicial-restructuring facts were recovered as five separately dated official events; price observations remain non-trainable discovery inputs.
- Source-mismatch review is closed at zero. SPAC trust redemptions, target-versus-SPAC entity leakage, later OTC aliases, Form 25 consequences and future insolvency backfills are explicit rejected controls; genuine court insolvency, administration, receivership, dissolution and forced-delisting events are stored separately at their legal dates.
- Delisting cause now precedes severity: EM and MRCC are paid merger mechanics and rejected controls; SEAC and BNSO are going-dark exits; ABB, CAJ, CEA and ZNH retained foreign primary listings; DTEA transferred to TSXV and PTNR consolidated on TASE. Event-time exchange tickers are stored separately from later OTC symbols.
- Event-time identity is now mandatory: IVP and MULN historical events reject later IVPR/BINI metadata. Later financing is also time-isolated: IVP and SINT unit offerings receive their own closing-date events and cannot raise earlier compliance-split labels.
- Reverse-split review now distinguishes pure mechanics from causal recapitalization: BNED's 1.925 billion-share issuance is a June 10 A++ event while the June 12 split remains B; SIDU's February 1 offering is A while its December split remains B; CISS is A because post-split resettable/cashless warrant coverage exceeded outstanding common.
- The ordinary reverse-split tail is closed. MAXN and CANO remain B because the filings show proportional mechanics; ASTI, VLCN, SBFM, AWIN, KAL, NUWE and DBGI are A only because primary evidence establishes linked issuance, warrant, note-default or financing chains. Later identities MAXNQ, EMPD, CANOQ and KALRQ are rejected and replaced with event-time records.
- Historical issuer identity is checked before severity: June 2024 `JBIO` is restored to Aerovate/`AVTE`, August 2023 `VBIO` to Tivic/`TIVC`, August 2023 `CHAI` to Siyata/`SYTA`, and January 2023 `DVLT` to WiSA/`WISA`. Wrong-identity vendor rows are rejected controls and replaced by event-time manual records.
- Price mechanics are isolated from news: LIAN's March 15 move is a $4.80 ex-dividend adjustment rather than a second negative event; GNLN's April 24 price row remains C discovery while April 23 resettable-warrant exercisability and the May 5 Nasdaq Rule 5101 determination carry the causal A labels.
- Detector precision is now measured by family and detected type. Reverse-split discovery has 96.7% reviewed-event yield, `voluntarydelisting` has 93.1%, and generic `delisted` has only 26.7%. `interest_coverage_below_1` has 23.8% reviewed-event yield and `negative_equity` has 86.4%, but the latter includes three explicit SPAC accounting controls; delisting therefore requires cause-first review and fundamental ratios require debt-service, covenant, payment, liquidity and issuer-structure context.
- Bankruptcy-family evidence now separates court insolvency, actual debt default, secured-creditor Article 9 enforcement and cash-returning corporate liquidation. SANW is A++ creditor enforcement over substantially all collateral, while EQC is a B cash-and-trust liquidation boundary rather than common-equity death.
- Multi-passage selection prefers evidence that resolves the detected event over a higher-scored ambiguous passage. This prevents generic cash-flow or contract language from hiding explicit covenant compliance, lender relief, merger consideration or holder recovery.
- SEC evidence extraction now follows EX-99 exhibits attached to periodic reports as well as 8-K/6-K filings. This closed the AREB gap where the final Nasdaq panel outcome was attached to a 10-Q rather than present in the primary quarterly-report document.
- Completed-candidate and completed-thread registries prevent exhausted historical batches from being regenerated. Queue rotation no longer breaks the review-only `D:\short` export: old rows fall back to durable stable-ID/ticker/date identity while richer metadata is rejoined during downstream review.
- BLS public API rows remain snapshots, not release-timestamp evidence. `source_published_at` stays null until an archived BLS release confirms it.
- Event-chain rules keep one FOMC meeting and one annual stress-test episode from being double counted; every chain has exactly one primary member.
- Bankruptcy labels now split petition and terminal truth. Enviva, Spirit, Vertex Energy, Edgio and Tupperware receive A++ on the petition event and S only on the later confirmed plan-effective old-common cancellation event. Hypothetical Chapter 7 analysis, repeated pending-case language and bankruptcy-driven delisting notices are explicit controls rather than new primary bankruptcies.
- Latest audit: PASS with every safety/integrity counter at zero. Validation baseline: 223 passing tests.
- Personal Telegram MTProto listener remains disabled until valid `TELEGRAM_API_ID` and `TELEGRAM_API_HASH` exist. Telegram Bot delivery is never enabled by a normal dry run.

## Source-only and audit commands

Force one official-source collection while testing an adapter:

```powershell
python scripts/official_event_collector.py --force --timeout 30
```

Run the repeatable ledger safety and source-health audit:

```powershell
python scripts/audit_live_pipeline.py
```

Advance one 25-event, no-lookahead historical research batch and refresh quality metrics:

```powershell
python scripts/run_active_research_cycle.py
python scripts/build_research_quality_report.py
```

Apply only explicitly reviewed historical decisions, then render the human-readable ledger:

```powershell
python scripts/apply_active_event_adjudications.py
python scripts/build_active_adjudication_report.py
```

Fed and SEC use conditional HTTP cursors. BLS website RSS currently returns HTTP 403 from this host, so production collection uses the official free BLS Public Data API in one grouped request, rate-limited to once per 90 minutes.

## Non-negotiable boundaries inherited from D:\short

- Discovery scores, market reactions, and price crashes cannot verify an event.
- `R/L/E/C/P/X` scores are required for reviewed hard decisions.
- S requires truth death, legality death, common-equity death, or equivalent hard finality with strong evidence.
- Source date and available date are used; no future outcomes enter discovery or current-event features.
- All market relations default to `ABSTAIN` and `no_trading=1`.
- Binance collection uses `data-api.binance.vision` without authentication; it never enters the separate trading project. IBKR remains a local snapshot-only capability probe and is not scheduled by the server Worker.
- No order, position, balance, account, or trade endpoint exists in this pipeline.

## Evidence outputs

- `data/research/live_evidence_review_queue.csv`
- `data/research/live_review_triage.csv`
- `reports/live_evidence_review_latest.md`
- `reports/live_review_triage_latest.md`
- `reports/live_manual_review_proposals_latest.md`
- `reports/live_primary_adjudications_latest.md`
- `reports/live_asset_relations_latest.md`
- `reports/live_market_observation_latest.md`
- `reports/live_cycle_latest.json`
- `reports/live_pipeline_audit_latest.md`
- `reports/evidence_source_snapshots_latest.json`
- `reports/evidence_source_snapshots_latest.md`
