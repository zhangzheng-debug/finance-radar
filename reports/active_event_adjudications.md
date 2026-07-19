# 主动历史事件证据裁决

日期：`2026-07-17`

- 已裁决：`792`
- Verified：`441`；Rejected controls：`351`
- 等级：S `14`、A++ `98`、A `142`、B `181`、C `6`
- 边界：不直接修改 `D:\short`，不自动启用训练，不使用事后收益定级，不产生交易动作。

## 最近裁决

以下内容来自显式人工复核配置；自动抓取和候选排序本身无权改变标签。

### FATAQ / A++ / substantially_all_FAT_Brands_assets_sold_by_595_million_credit_bid_in_Chapter_11

On June 15 the FAT Brands debtors closed the sale of substantially all restaurant, bar, entertainment, franchise and brand assets for an approximately $595 million credit bid comprising DIP and prepetition note obligations, plus assumed liabilities. The filing does not establish a confirmed effective plan or old-common distribution.

- 裁决：A++: the substantially-all-asset credit-bid sale actually closed. Without a final effective plan stating old-common cancellation and distribution, the event remains below S.
- 一手证据：[SEC Form 8-K Items 1.01 and 2.01](https://www.sec.gov/Archives/edgar/data/2011954/000149315226029245/form8-k.htm)
- 训练角色：`positive_substantially_all_asset_credit_bid_sale_closing_boundary`；状态：`verified`

### FELPQ / rejected / same_day_price_crash_with_later_FELPQ_identity_proxy_for_FELPU_Chapter_11_petition

Foresight Energy and all subsidiaries filed Chapter 11 on March 10, 2020, but the issuer identified the event-time OTCQX unit ticker as FELPU. Sharadar's identity history records the later FELPU-to-FELPQ change on April 6, so the March 10 FELPQ price row is a post-event identity and market-outcome proxy rather than the legal event itself.

- 裁决：Reject the price observation as a hard event and reject FELPQ as the March 10 identity. Preserve the FELPU petition, later plan confirmation and FELPQ plan-effective terminal outcome on their own dates.
- 一手证据：[SEC Form 8-K Items 1.01, 1.03 and 2.04](https://www.sec.gov/Archives/edgar/data/1540729/000095014220000757/eh2000444_8k.htm)
- 训练角色：`rejected_price_outcome_and_post_event_ticker_with_primary_event_recovery`；状态：`rejected`

### FELPU / A++ / parent_general_partner_and_all_subsidiaries_prearranged_Chapter_11_with_DIP_and_debt_equitization

FELPU, its general partner and all subsidiaries filed voluntary Chapter 11 petitions in the Eastern District of Missouri on March 10 under case 20-41308-659. The RSA had support from lenders holding more than 73% of approximately $1.4 billion of first- and second-lien claims, contemplated substantially all funded debt being equitized, and included a committed $100 million new-money DIP facility and proposed $225 million exit facility.

- 裁决：A++ on the petition date: court filing, broad debtor scope and debt equitization are realized, but the old-unit terminal treatment had not yet become effective.
- 一手证据：[SEC Form 8-K Items 1.01, 1.03 and 2.04 plus Exhibit 99.2](https://www.sec.gov/Archives/edgar/data/1540729/000095014220000757/eh2000444_8k.htm)
- 训练角色：`positive_prearranged_parent_level_Chapter_11_event_time_identity_boundary`；状态：`verified`

### FELPQ / A++ / Chapter_11_plan_confirmed_with_zero_recovery_old_units_pending_effectiveness

The Bankruptcy Court confirmed Foresight's plan on June 24. The confirmed plan reduced more than $1 billion of debt, eliminated about $94 million of anticipated annual cash interest and provided no recovery to 80,996,773 common units or 64,954,691 subordinated units, but the issuer expressly said the effective date remained subject to conditions.

- 裁决：A++ only on June 24: court confirmation and zero-recovery treatment are strong, but the issuer stated that effectiveness remained conditional. S is reserved for the June 30 effective date.
- 一手证据：[SEC Form 8-K Item 1.03 and court confirmation order](https://www.sec.gov/Archives/edgar/data/1540729/000156459020030501/felp-8k_20200624.htm)
- 训练角色：`positive_confirmed_zero_recovery_plan_pending_effectiveness_boundary`；状态：`verified`

### FELPQ / S / confirmed_plan_effective_all_common_and_subordinated_units_extinguished_zero_recovery

Foresight's confirmed plan became effective on June 30 after all conditions were satisfied or waived. Holders received no recovery, all existing common and subordinated units were extinguished without consideration, the partnership and related dissolving entities were slated for dissolution, and operating assets moved to creditor-owned successor entities.

- 裁决：S is anchored only to June 30 plan effectiveness. Explicit zero recovery plus extinguishment without consideration satisfies finality and is not backfilled to the March petition or June 24 confirmation.
- 一手证据：[SEC Form 8-K Items 1.03, 3.03 and 5.01](https://www.sec.gov/Archives/edgar/data/1540729/000156459020031377/felp-8k_20200630.htm)
- 训练角色：`positive_terminal_old_common_and_subordinated_unit_equity_death`；状态：`verified`

### CZOOF / rejected / post_suspension_CZOOF_identity_conflated_with_subsidiary_administration_and_later_parent_winding_up

On May 21 three English operating and holding subsidiaries entered administration while the Cayman parent, whose NYSE ticker was still CZOO, only decided to seek shareholder approval for a winding up. NYSE suspended CZOO that day, CZOOF began as the post-suspension OTC identity, and the parent did not enter voluntary winding up until shareholders approved it on July 2.

- 裁决：Reject CZOOF, May 21 and bankruptcy liquidation as one parent-level event. Preserve the subsidiary administrations, NYSE suspension, final delisting and parent voluntary winding up as separate events on their exact dates. The Teneo exhibit's generic bankruptcy-and-insolvency service description is not event evidence.
- 一手证据：[SEC Forms 6-K dated May 21, May 22 and July 3, 2024](https://www.sec.gov/Archives/edgar/data/1859639/000121390024045404/ea0206593-6k_cazoo.htm)
- 训练角色：`rejected_vendor_event_scope_date_and_ticker_conflation_with_primary_event_recovery`；状态：`rejected`

### CZOO / A++ / material_operating_and_holding_subsidiaries_entered_UK_administration

Cazoo Holdings Limited, Cazoo Ltd and Cazoo Properties Limited entered UK administration on May 21 and joint administrators from Teneo were appointed to manage their affairs, business and property. The listed Cayman parent was not itself in administration and only resolved to seek a winding up.

- 裁决：A++ for the realized administration of the three material English subsidiaries. Do not describe the Cayman parent as bankrupt or in liquidation on this date, and do not use the later CZOOF ticker.
- 一手证据：[SEC Form 6-K and Exhibit 99.1](https://www.sec.gov/Archives/edgar/data/1859639/000121390024045404/ea0206593-6k_cazoo.htm)
- 训练角色：`positive_material_subsidiary_administration_with_parent_scope_boundary`；状态：`verified`

### CZOO / A / distress_driven_NYSE_immediate_suspension_with_no_appeal_and_OTC_transition

NYSE notified Cazoo on May 21 that it would commence proceedings to delist the Class A ordinary shares and suspended trading immediately under Section 802.01D after the subsidiary administrations and parent winding-up disclosure. The issuer did not intend to appeal and expected OTC Pink trading.

- 裁决：Use CZOO for the security suspended from NYSE on May 21. CZOOF is the post-suspension OTC alias, not the event-time NYSE identity.
- 一手证据：[SEC Form 6-K](https://www.sec.gov/Archives/edgar/data/1859639/000121390024045906/ea020670801-6k_cazoogroup.htm)
- 训练角色：`positive_listing_suspension_exact_event_time_identity`；状态：`verified`

### CZOO / A / final_NYSE_delisting_after_distress_driven_suspension

The issuer's winding-up proxy states that its Class A ordinary shares were delisted by NYSE effective June 3, 2024 and were then trading on the OTC Pink Marketplace.

- 裁决：Preserve June 3 as the final NYSE delisting date, separate from the May 21 immediate suspension and OTC ticker transition.
- 一手证据：[SEC Form 6-K Exhibit 99.1 proxy statement](https://www.sec.gov/Archives/edgar/data/1859639/000121390024054499/ea020814501ex99-1_cazoo.htm)
- 训练角色：`positive_final_delisting_effective_date_separated_from_suspension`；状态：`verified`

### CZOOF / A++ / insolvent_Cayman_parent_voluntary_winding_up_commenced_with_no_expected_shareholder_distribution

On July 2 shareholders approved voluntary winding up because Cazoo Group Ltd was unable to pay its debts, joint voluntary liquidators were appointed immediately, all directors resigned, share transfer books closed, all realizable assets had been disposed of and the company expected no remaining proceeds for shareholders.

- 裁决：A++ only. Winding up commenced and transfers stopped, but the filing says the process will continue until affairs are finally wound up and liquidators later seek dissolution. Expected zero proceeds is not explicit final cancellation or completed dissolution, so S is prohibited.
- 一手证据：[SEC Form 6-K](https://www.sec.gov/Archives/edgar/data/1859639/000121390024058749/ea020890401-6k_cazoogroup.htm)
- 训练角色：`positive_parent_insolvent_winding_up_and_negative_premature_S_boundary`；状态：`verified`

## 完整记录

逐行证据、R/L/E/C/P/X、分数和裁决理由见 `active_event_adjudications.csv`。
