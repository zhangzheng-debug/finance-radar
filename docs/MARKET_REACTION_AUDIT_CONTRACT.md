# 事件后价格审计合同

## 当前可用能力

- 时间锚使用事件家族声明的 `source_published / filing_effective / event_occurred`；`known_at=max(source_published_at, local_received_at)` 防止后见信息进入旧时点。
- 只有精确时间戳才允许分钟窗口；只有日期时，T+5m、T+30m、T+2h 均为不可用。
- 固定窗口为 initial、T+5m、T+30m、T+2h、下个收盘、T+1d、T+5d。
- 默认供应商路径请求指定分钟的 1m OHLCV bar：非加密资产使用 Twelve Data `/time_series`，加密资产使用 Binance public `/klines`。
- 每个快照保存供应商 bar 时间和系统采集时间；供应商时间不在所请求分钟内时失败关闭。
- 错过宽限期写 `MISSED_WINDOW`，绝不拿最新价格回填。
- 结果只属于 `post_event_audit_only`，数据库约束要求 `allowed_for_discovery_rank=0`、`allowed_as_model_feature=0`，且没有账户、订单、持仓或余额接口。

## 尚未伪装为已完成的能力

基准相对收益、行业相对收益、完整路径的 MFE/MAE、累计成交量异常、拆股/分红全复权和交易所官方日历仍需要经过审阅的数据映射与区间 bar 获取。当前一分钟 bar 的 high/low 不能冒充从事件到目标窗口的完整路径，因此系统不会生成这些指标。

上线前还必须用真实供应商账户做小批 shadow 验证：检查盘前/盘后、周末、节假日、停牌、ticker 变更、复权和限频错误。仓库单元测试不代表供应商授权或生产可用性。
