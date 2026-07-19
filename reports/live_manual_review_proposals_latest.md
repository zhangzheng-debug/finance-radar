# Live Manual Review Proposals

- Prepared: `2026-07-16 Asia/Shanghai`
- Scope: top five rows from the 70-event review queue after source-semantic triage.
- Status: proposals only. Nothing in this file changes `candidate`, assigns a final grade, enables market observation, or sends Telegram.
- Rule: queue score is review priority, not severity.

## 1. Creative Media & Community Trust Corp — maturity default

- Event: `FR-LIVE-f43b242e7e14f8155e4962c095b23e00`
- Primary evidence: https://www.sec.gov/Archives/edgar/data/908311/000090831126000077/cmct-20260709.htm
- Confirmed: the company received a maturity-default notice on a non-recourse mortgage secured by 1 Kaiser Plaza; outstanding principal was disclosed as `$97.1 million`; monthly interest remained current; the company was discussing a resolution or extension with the special servicer.
- Not confirmed: acceleration against the parent, cross-default, loss of the property, cure outcome, or common-equity impairment.
- Proposed decision: `verified / A`, not `A++` or `S`.
- Proposed R/L/E/C/P/X: `2/2/1/3/0/-1 = 7`.
- Why capped: the default is concrete but asset-specific, non-recourse, still under negotiation, and no common-equity-death outcome is established.

## 2. Crescent Biopharma — equity offering

- Event: `FR-LIVE-08e88528183d20a146e73b7a7364c1f9`
- Primary evidence: https://www.sec.gov/Archives/edgar/data/1253689/000162828026048347/cbio-20260714.htm
- Confirmed: public offering of `8,094,793` ordinary shares plus `525,897` pre-funded warrants at approximately `$14.50`; underwriters received an option for up to `1,293,103` additional shares; expected net proceeds were approximately `$115.9 million` before the option.
- Still needed: pre-offering fully diluted share count, closing confirmation, use of proceeds, cash runway, and discount versus an unambiguous pre-announcement market reference.
- Proposed decision: retain `candidate`; prepare `A` review only after dilution percentage and closing are established.
- Why not automatic: capital raised can extend runway, and the filing excerpt alone does not establish net common-equity damage.

## 3. Catheter Precision — preferred/warrant financing update

- Event: `FR-LIVE-2dd8993137a3007e693990622012270e`
- Primary evidence: https://www.sec.gov/Archives/edgar/data/1716621/000143774926023639/vtak20260714_8k.htm
- Confirmed: the filing supplements prior private-placement disclosures and describes securities senior to common stock plus warrant/equity mechanics.
- Still needed: incremental securities issued in this filing, conversion and exercise totals, effective price, fully diluted denominator, and whether the event is new financing or only a terms update.
- Proposed decision: retain `candidate`; do not grade from the phrase `public offering`, which appears in a negated exemption statement.

## 4. TS Banking Group / TS Contrarian Bancshares — Fed enforcement

- Event: `FR-LIVE-8473fbad30faa23d6a87d1d72b4c86d3`
- Primary evidence: https://www.federalreserve.gov/newsevents/pressreleases/enforcement20260709a.htm
- Confirmed: the Federal Reserve published an enforcement-action release naming the institutions.
- Still needed: the attached order, restrictions, capital/liquidity requirements, penalties, duration, and whether the action constrains the listed issuer or only a subsidiary.
- Proposed decision: retain `candidate`; read the order before assigning `A`.

## 5. Triller Group — listing-compliance extension

- Event: `FR-LIVE-f7abda066632926c2ae88630805a7a0d`
- Primary evidence: https://www.sec.gov/Archives/edgar/data/1769624/000121390026078375/ea0297939-8k_triller.htm
- Confirmed: Nasdaq granted an exception to regain bid-price compliance until `2026-07-30`; trading had resumed after a prior periodic-filing issue.
- Not confirmed: compliance by the deadline, permanent continued listing, or a new delisting decision.
- Proposed decision: retag as `listing_compliance_extension`; if manually verified, cap at `B` unless later evidence shows an actual delisting order.
- Why capped: the current filing is relief with residual risk, not a completed delisting.

## Operator decision fields

For each proposal, the operator must explicitly record one of: `accept`, `revise`, `reject`, or `defer`. Only accepted/revised rows may be copied into `config/live_primary_adjudications.json`.
