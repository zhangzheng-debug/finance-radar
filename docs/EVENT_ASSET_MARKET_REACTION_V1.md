# Event-to-asset market reaction V1

## Product contract

The public event page may show one compact **研究信号** block. It is omitted
entirely when none of these results exists:

- a current, `PUBLIC_APPROVED` Qwen semantic result trained from independent
  dual-human gold labels;
- a completed, current-version read-only price observation for an explicitly
  mapped asset or proxy;
- a completed, timestamp-bound post-publication return.

Completed return lines say **消息发布后**, not “the event caused”; absolute-price
lines are explicitly presented as non-live price context. Asset mapping selects
instruments worth observing; it never predicts direction, magnitude, or a trade.

The public hierarchy remains deliberately short:

1. what happened and the key source passage;
2. optional **研究信号** (approved Qwen routing, current price context and/or
   completed market moves);
3. source link and timestamps.

Internal review checklists, pending jobs, provider failures and empty model
states do not appear on the public page.

## Mapping V1

`config/event_asset_mapping_v1.json` is strict and content-addressed. Each
selected mapping is capped at three assets and forces:

- `direction=ABSTAIN`;
- `impact_score=0`;
- `no_trading=1`.

Every applied event version records an immutable decision and one receipt per
selected asset, including rule id, policy version, policy SHA-256, rank and
display role. A company ticker is a direct security. GLD, USO, BNO, SPY and TLT
are observation proxies or benchmarks, not evidence that an event occurred.

A canonical company/ticker pair is not enough to create a direct-security
mapping. The selected source capture must name the same company, and V1 admits
canonical tickers only from the configured SEC, Sharadar research or FDA source
families. This prevents country names, ordinary uppercase words and unrelated
companies mentioned in an aggregator headline from being treated as securities.
Public-news company mapping stays fail-closed until an independently tested
issuer-resolution contract exists.

V1 is deliberately narrow: direct company securities, armed-conflict/energy
transmission, monetary-policy decisions and inflation releases. It does not
guess an asset for every story. Sector ETFs, currencies, rates futures and
commodity futures require separate, tested mapping rules before activation.

## Historical bars and cache

The production provider path requests the exact target one-minute bar. A late
request may therefore recover a historical window as `HISTORICAL_EXACT_BAR`.
Using a current/latest quote as an old window remains forbidden. A target bar
is not eligible until its minute has closed and the provider ingestion grace
has elapsed. Bars are cached by provider, stable asset identity, interval and
target minute, so multiple events mapped to the same registered instrument can
reuse one provider response without mixing identically named instruments.

Transient failures have a bounded retry budget. Older metrics without a
provable event-version binding remain in a legacy archive and are never
projected into the current public event.

The independent market timer defaults to at most six uncached exact-bar
requests per provider per cycle. Override only after checking provider quota:

```text
MARKET_EXACT_BAR_REQUEST_LIMIT=6
```

Initial production activation is deliberately current-day first:

```text
MARKET_MAPPING_FRESHNESS_DAYS=0
```

After the current-day queue drains and public samples are verified, expand the
same setting to `1`, `7` and finally `14`. Mapping a production-sized 14-day
history in one first cycle is forbidden because it can create many more exact
bar jobs than the provider budget can drain promptly. The cycle report records
the effective mode, freshness window and request cap without exposing secrets.

## Rollout controls

Automatic mapping remains disabled by default when the legacy continuous worker
is run directly. Run the versioned mapper in shadow mode first:

```powershell
python scripts/map_event_assets.py --db data/finance_radar.sqlite3 --dry-run
```

The report records mapped/unmapped counts, rule hits, sample assets and exact
timestamp coverage. Worker modes are:

```text
FINANCE_RADAR_ASSET_MAPPING_MODE=disabled
FINANCE_RADAR_ASSET_MAPPING_MODE=shadow
FINANCE_RADAR_ASSET_MAPPING_MODE=apply
```

Before enabling `finance-radar-market.timer`, review a 50–100 event sample for
rule precision, proxy clarity, exact-bar availability, provider quota and public
display rights. The production timer keeps mapping, scheduling and exact-bar
capture outside the five-minute collection cycle. The script defaults to
`shadow` when the environment setting is absent; production must explicitly
set `apply` before new mapping decisions are written. Mode activation and
production deployment remain separate operator actions.

Events whose source exposes only a calendar date do not receive intraday
T+5m/T+30m/T+2h claims. For exchange-traded assets the market worker instead
uses the prior close as its baseline, then observes the first full trading-day
close after the event date, the next trading close and the five-day close;
the public UI labels those session windows explicitly. A provider miss remains
missing and is never filled with a current quote.

The checked-in shadow report is a local snapshot, not a production claim. Its
low match rate is expected from a precision-first V1 and should be expanded by
adding reviewed rules, not by weakening the match boundary.
