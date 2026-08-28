# 事件原子化与市场映射修复

完整的候选资产覆盖及启用边界见 [ASSET_UNIVERSE_V1.md](ASSET_UNIVERSE_V1.md)。

## 当前合同

- 一条 canonical event 只能绑定一个原子事件跨度；事件规则、主体和受影响资产必须来自同一段来源文本。
- 多主题市场综述继续保存在 `raw_observations`，但不得进入事件雷达，也不得产生资产映射或价格任务。
- BTC 是 24×7 直接观察资产，供应商标识为 `BTCUSDT`；IBIT 是美国上市代理，只按 NASDAQ 交易时段观察。二者不是同一种资产，也不会互相替代。
- 自动映射只生成只读行情审计，`direction=ABSTAIN`、`no_trading=1`，不参与事件真假、极性或风险评级。

## 连续处理

每轮事件提取都会重新检查仍处于 candidate 的 OpenNews 捕获。若当前规则认定来源不是原子事件：

1. 原始捕获继续保留；
2. 来源边标为 `filtered_aggregated_noise`；
3. 事件生成新的 rejected 版本；
4. 当前自动资产投影设为不可观察；
5. 尚未完成的价格任务标为 `CANCELLED_EVENT_REJECTED`；
6. 已完成的旧价格快照与映射收据继续保留作审计历史，但不会投影到当前事件版本。

## 历史修复

历史修复默认为只读。先生成包含事件版本、来源内容哈希、关系边、活动资产投影和未完成价格任务的绑定计划：

```bash
python scripts/repair_event_atomicity_history.py \
  --db /path/to/finance_radar.sqlite3 \
  --plan /path/to/event_atomicity_plan.json
```

应用时必须提供计划中精确的 `plan_sha256`，并输出一份不可覆盖的执行收据：

```bash
python scripts/repair_event_atomicity_history.py \
  --db /path/to/finance_radar.sqlite3 \
  --plan /path/to/event_atomicity_plan.json \
  --apply \
  --expect-plan-sha256 <exact-plan-sha256> \
  --receipt /path/to/event_atomicity_apply_receipt.json
```

应用前会逐条重算绑定；任一目标的当前版本、来源 revision、关系或活动市场状态变化，整次执行立即停止并要求重新生成计划。保留下来的事件会按当前映射策略强制重算，以便历史 BTC 事件获得 BTC 直接资产和 IBIT 美国上市代理，并使旧错误映射失效。

生产执行仍应走标准备份、验证和回滚流程。该工具本身不删除原始捕获、不删除完成行情、不核验事件，也不提供任何交易能力。
