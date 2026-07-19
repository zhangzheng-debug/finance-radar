# Risk Router input-contract audit v1

- Rows: `40`
- Risk rows with real body text: `0.0%`
- Risk rows that are title-only: `20`
- Content-ambiguous risk rows at discovery: `16`
- Benchmark content contract: `FAIL`

This audit does not train, tune or run inference. It checks whether the frozen bytes contain the evidence-stage text declared by the model contract.

## Gate details

- PASS — `minimum_rows`
- PASS — `risk_rows_present`
- FAIL — `risk_body_coverage`

## Required remediation

- Freeze exact official-page evidence passages before evaluating the content model; keep P0 enforcement-source discovery routing outside learned text features.
- The existing v1 failure remains valid evidence that the deployed router must stay shadow. It is not a promotion test for v2.

## Ambiguous risk samples

- `EXT-032e752e592a6d9c2671` · `sec_litigation_external` · NanoBit Limited, et al.
- `EXT-27e9b67edee61347785e` · `sec_litigation_external` · American Patriot Brands, Inc.; Urban Pharms, LLC; TSL Distribution, LLC; DJ&S Property #1, LLC; Robert Y. Lee; Brian L. Pallas
- `EXT-3581399f5f1a61d0d691` · `sec_litigation_external` · Michael Bowen and Chol Kim a/k/a Brandon Kim
- `EXT-37a05abddd1af5406326` · `sec_litigation_external` · Mingran Wang
- `EXT-3e740f3d88cde13d94dd` · `sec_litigation_external` · Giovanni Pennetta
- `EXT-4509117f96b2fb9c1b57` · `sec_litigation_external` · Sanders Family Office; Margaret Sanders; Francisco J. Herrera
- `EXT-5d9fb7b4b60ab40cdc6a` · `sec_litigation_external` · David Kushner and La Mancha Funding Corp.
- `EXT-6a5bdf6493215288a739` · `sec_litigation_external` · Charles E. Jones
- `EXT-71123388985bffc58ea4` · `sec_litigation_external` · Steve M. Bajic et al.
- `EXT-8f33d2a243141e232fa5` · `sec_litigation_external` · Shane Schmidt
- `EXT-95880770e67472aee1c7` · `sec_litigation_external` · AI Financial Education Foundation Ltd.
- `EXT-9c61cbc9684bcebb81df` · `sec_litigation_external` · Michael J. Forster
- `EXT-badf41c0b8f34b8568d4` · `sec_litigation_external` · Jamal (“Jimmy”) Chammout; Ali El Siblani; Ali Jawad; Rabih Rakha
- `EXT-c115bdb1147d6be1b8b8` · `sec_litigation_external` · Weiguo Zhai
- `EXT-d17aec80fdc0b86de511` · `sec_litigation_external` · Casey Muggleston
- `EXT-fc9f87a95436efda8c69` · `cftc_enforcement_external` · CFTC Orders New York Trader to Pay $200,000 for Spoofing
