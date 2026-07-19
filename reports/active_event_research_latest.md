# Active Event Research Queue

Generated: `2026-07-16T17:03:42.410694+00:00`

- Queue rows: `150`
- Historical window: `2020-01-01` to `latest available`
- Source research workspace: `D:\short`
- Purpose: keep a research backlog even when the live-news stream is quiet.
- Status: candidate discovery only; no label mutation, model training, trading, or recommendation.
- Ranking uses only event metadata available at the candidate date; no post-event return or drawdown is used.

## Queue Policy

1. Corporate actions and point-in-time fundamentals create evidence-review candidates.
2. Price crashes create evidence-search candidates only and remain capped at `C_price_only`.
3. S/A++ requires primary evidence of truth death, legality death, or common-equity death.
4. Reviewed event IDs, unmatched securities, and non-common-equity instruments are excluded.
5. Family and within-family event-type quotas prevent one metadata category from dominating the queue.
6. Known semantic artifacts are excluded: revenue YoY below -100% with positive current revenue, gross-margin deltas below -200pp, previous-quarter FCF turns, and generic cash/debt ratios for SPACs, financials, and utilities.
7. Five-letter tickers ending in F, Q, or Y remain candidates but are routed to event-time identity review; the detector never rewrites the ticker or assigns a final label.

## By Event Family

| event_family | len |
| --- | --- |
| fundamental_shock | 30 |
| price_crash | 30 |
| equity_dilution | 30 |
| delisting_or_suspension | 30 |
| bankruptcy_or_distress | 30 |

## By Selection Strategy

| selection_strategy | len |
| --- | --- |
| event_time_identity_review | 146 |
| corporate_action_evidence_review | 4 |

## Highest-Priority Evidence Reviews

| queue_rank | family_rank | ticker_at_event | identity_review_flag | event_date | event_family | event_type | priority_score | provisional_grade_cap |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| 1 | 1 | DLAPQ | True | 2024-07-01 | bankruptcy_or_distress | bankruptcy_liquidation | 137.0 | A++_candidate |
| 2 | 1 | CMBMF | True | 2026-03-26 | delisting_or_suspension | delisted | 127.0 | A_candidate |
| 3 | 1 | ALCYF | True | 2025-12-31 | fundamental_shock | negative_equity | 104.5 | A_candidate |
| 4 | 1 | DMKPQ | True | 2023-05-22 | equity_dilution | reverse_split | 76.225 | B_candidate |
| 5 | 1 | FELPQ | True | 2020-03-10 | price_crash | volume_crash | 56.811 | C_price_only |
| 6 | 2 | DMTKQ | True | 2024-06-26 | bankruptcy_or_distress | bankruptcy_liquidation | 137.0 | A++_candidate |
| 7 | 2 | DTCKF | True | 2026-03-24 | delisting_or_suspension | delisted | 127.0 | A_candidate |
| 8 | 2 | QVCAQ | True | 2025-12-31 | fundamental_shock | negative_equity | 104.5 | A_candidate |
| 9 | 2 | GTIJF | True | 2025-08-25 | equity_dilution | reverse_split | 75.89 | B_candidate |
| 10 | 2 | TIRXF | True | 2026-01-29 | price_crash | volume_crash | 56.803 | C_price_only |
| 11 | 3 | AUVIQ | True | 2024-05-28 | bankruptcy_or_distress | bankruptcy_liquidation | 137.0 | A++_candidate |
| 12 | 3 | UOKAF | True | 2026-03-19 | delisting_or_suspension | delisted | 127.0 | A_candidate |
| 13 | 3 | ALCYF | True | 2025-09-30 | fundamental_shock | negative_equity | 104.5 | A_candidate |
| 14 | 3 | LYTHF | True | 2024-02-23 | equity_dilution | reverse_split | 75.89 | B_candidate |
| 15 | 3 | BBUCQ | True | 2021-02-16 | price_crash | volume_crash | 56.786 | C_price_only |
| 16 | 4 | ISUNQ | True | 2024-05-22 | bankruptcy_or_distress | bankruptcy_liquidation | 137.0 | A++_candidate |
| 17 | 4 | ZENVF | True | 2026-03-18 | delisting_or_suspension | delisted | 127.0 | A_candidate |
| 18 | 4 | QVCAQ | True | 2025-09-30 | fundamental_shock | negative_equity | 104.5 | A_candidate |
| 19 | 4 | VIEWQ | True | 2023-07-27 | equity_dilution | reverse_split | 75.89 | B_candidate |
| 20 | 4 | YUANF | True | 2021-10-18 | price_crash | volume_crash | 56.75 | C_price_only |
| 21 | 5 | CZOOF | True | 2024-05-21 | bankruptcy_or_distress | bankruptcy_liquidation | 137.0 | A++_candidate |
| 22 | 5 | BHATF | True | 2026-03-13 | delisting_or_suspension | delisted | 127.0 | A_candidate |
| 23 | 5 | HSPOF | True | 2025-09-30 | fundamental_shock | negative_equity | 104.5 | A_candidate |
| 24 | 5 | QVCAQ | True | 2025-05-23 | equity_dilution | reverse_split | 75.495 | B_candidate |
| 25 | 5 | PTRAQ | True | 2023-08-08 | price_crash | volume_crash | 56.666 | C_price_only |

## Required Next Action

For each row, retrieve contemporaneous SEC, court, regulator, exchange, or company evidence. Only a separate evidence-review step may promote `candidate` to `verified`, `weak`, or `rejected`.
