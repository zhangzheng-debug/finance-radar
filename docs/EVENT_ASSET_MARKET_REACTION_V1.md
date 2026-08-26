# Event-to-asset market reaction V1

## Product contract

The public event page may show one compact **研究信号** block. It is omitted
entirely when neither of these results exists:

- a current, `PUBLIC_APPROVED` Qwen semantic result trained from independent
  dual-human gold labels;
- a completed, timestamp-bound post-publication return.

The price line says **消息发布后**, not “the event caused”. Asset mapping selects
instruments worth observing; it never predicts direction, magnitude, or a
trade.

The public hierarchy remains deliberately short:

1. what happened and the key source passage;
2. optional **研究信号** (approved Qwen routing and/or completed market moves);
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

The worker defaults to at most six uncached exact-bar requests per provider per
cycle. Override only after checking provider quota:

```text
MARKET_EXACT_BAR_REQUEST_LIMIT=6
```

## Rollout controls

Automatic mapping is disabled by default in the continuous worker. Run the
versioned mapper in shadow mode first:

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

Before switching production to `apply`, review a 50–100 event sample for rule
precision, proxy clarity, exact-bar availability, provider quota and public
display rights. Mode activation and production deployment remain separate
operator actions.

The checked-in shadow report is a local snapshot, not a production claim. Its
low match rate is expected from a precision-first V1 and should be expanded by
adding reviewed rules, not by weakening the match boundary.
