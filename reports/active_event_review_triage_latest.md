# Active Historical Review Triage

Generated: `2026-07-16T17:51:27.777575+00:00`

- Unreviewed queue rows ranked: `127`
- Inputs: Sharadar candidate metadata plus contemporaneous SEC evidence passages.
- Forbidden inputs: post-event return, drawdown, recovery, and future delisting outcome.
- Review score is workload priority only; it cannot mutate labels or enable trading.

## Buckets

- delisting_cause_review: `7`
- event_time_identity_control: `20`
- expected_effective_date_control: `1`
- fundamental_context: `2`
- listing_compliance_reverse_split: `6`
- low_evidence_fundamental: `20`
- ordinary_corporate_action: `2`
- price_cause_review: `4`
- price_only_control: `14`
- single_quarter_interest_coverage_boundary: `6`
- source_mismatch_review: `45`

## Top 20

| rank | score | ticker | date | detected | bucket | proposed disposition | ceiling | evidence |
|---:|---:|---|---|---|---|---|---|---|
| 1 | 90 | PTRAQ | 2023-08-08 | volume_crash | price_cause_review | possible_hard_event_cause | A++_review_ceiling | [SEC](https://www.sec.gov/Archives/edgar/data/1820630/000162828023027865/ptra-20230807.htm) |
| 2 | 90 | SFTGQ | 2023-10-09 | volume_crash | price_cause_review | possible_hard_event_cause | A++_review_ceiling | [SEC](https://www.sec.gov/Archives/edgar/data/1762322/000121390023081257/ea186500-8k_shifttech.htm) |
| 3 | 88 | DTCKF | 2026-03-24 | delisted | delisting_cause_review | negative_cause_needs_outcome | A_review_ceiling | [SEC](https://www.sec.gov/Archives/edgar/data/1949478/000168316826002847/davis_6k.htm) |
| 4 | 88 | UOKAF | 2026-03-19 | delisted | delisting_cause_review | negative_cause_needs_outcome | A_review_ceiling | [SEC](https://www.sec.gov/Archives/edgar/data/1741534/000110465926031749/tm269264d1_ex99-1.htm) |
| 5 | 88 | BHATF | 2026-03-13 | delisted | delisting_cause_review | negative_cause_needs_outcome | A_review_ceiling | [SEC](https://www.sec.gov/Archives/edgar/data/1759136/000173112226000389/e7426_ex99-1.htm) |
| 6 | 88 | TIRXF | 2026-03-04 | delisted | delisting_cause_review | negative_cause_needs_outcome | A_review_ceiling | [SEC](https://www.sec.gov/Archives/edgar/data/1782941/000110465926023225/tm267985d1_ex99-1.htm) |
| 7 | 88 | TSEOF | 2026-03-02 | delisted | delisting_cause_review | negative_cause_needs_outcome | A_review_ceiling | [SEC](https://www.sec.gov/Archives/edgar/data/1519061/000110465926022537/tse-20260302x8k.htm) |
| 8 | 88 | CREVF | 2026-02-06 | delisted | delisting_cause_review | negative_cause_needs_outcome | A_review_ceiling | [SEC](https://www.sec.gov/Archives/edgar/data/1960208/000149315226005461/form6-k.htm) |
| 9 | 86 | SVUHF | 2025-01-15 | reverse_split | expected_effective_date_control | verify_split_but_do_not_accept_expected_date_as_realized | B_review_ceiling | [SEC](https://www.sec.gov/Archives/edgar/data/1973368/000149315225001914/form6-k.htm) |
| 10 | 83 | YTENQ | 2024-05-15 | bankruptcy_liquidation | event_time_identity_control | do_not_accept_q_suffix_metadata_without_primary_petition_evidence | A++_review_ceiling | [SEC](https://www.sec.gov/Archives/edgar/data/1121702/000110465924061645/tm2414256d2_8k.htm) |
| 11 | 83 | DCFCQ | 2024-04-19 | bankruptcy_liquidation | event_time_identity_control | do_not_accept_q_suffix_metadata_without_primary_petition_evidence | A++_review_ceiling | [SEC](https://www.sec.gov/Archives/edgar/data/1862490/000110465924048646/tm247645d3_6k.htm) |
| 12 | 83 | EIGRQ | 2024-04-10 | bankruptcy_liquidation | event_time_identity_control | do_not_accept_q_suffix_metadata_without_primary_petition_evidence | A++_review_ceiling | no_sec_candidate_yet |
| 13 | 83 | GMDAQ | 2024-04-05 | bankruptcy_liquidation | event_time_identity_control | do_not_accept_q_suffix_metadata_without_primary_petition_evidence | A++_review_ceiling | no_sec_candidate_yet |
| 14 | 83 | MIMOQ | 2024-04-01 | bankruptcy_liquidation | event_time_identity_control | do_not_accept_q_suffix_metadata_without_primary_petition_evidence | A++_review_ceiling | no_sec_candidate_yet |
| 15 | 83 | JOANQ | 2024-03-27 | bankruptcy_liquidation | event_time_identity_control | do_not_accept_q_suffix_metadata_without_primary_petition_evidence | A++_review_ceiling | no_sec_candidate_yet |
| 16 | 83 | CUROQ | 2024-03-11 | bankruptcy_liquidation | event_time_identity_control | do_not_accept_q_suffix_metadata_without_primary_petition_evidence | A++_review_ceiling | no_sec_candidate_yet |
| 17 | 83 | BFXXQ | 2024-03-04 | bankruptcy_liquidation | event_time_identity_control | do_not_accept_q_suffix_metadata_without_primary_petition_evidence | A++_review_ceiling | no_sec_candidate_yet |
| 18 | 83 | POLCQ | 2024-02-29 | bankruptcy_liquidation | event_time_identity_control | do_not_accept_q_suffix_metadata_without_primary_petition_evidence | A++_review_ceiling | no_sec_candidate_yet |
| 19 | 83 | SIENQ | 2024-02-21 | bankruptcy_liquidation | event_time_identity_control | do_not_accept_q_suffix_metadata_without_primary_petition_evidence | A++_review_ceiling | no_sec_candidate_yet |
| 20 | 83 | NVIVQ | 2024-02-12 | bankruptcy_liquidation | event_time_identity_control | do_not_accept_q_suffix_metadata_without_primary_petition_evidence | A++_review_ceiling | no_sec_candidate_yet |
