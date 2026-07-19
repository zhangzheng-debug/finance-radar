# Gate 0 外部链路决策单

依据：`gate0_20260714T110306Z.json` 的本机真实请求结果。此文件只记录结论；原始 HTTP 状态、延迟和错误保留在 `latest.md` / `latest.json`。

## 可以直接进入最小实现

- Federal Reserve RSS：返回并解析出条目。
- BLS Public Data API：无需注册密钥即可返回 CPI 序列观测值。
- Binance Spot 公共行情：使用官方 market-data-only 入口 `data-api.binance.vision`，不要依赖当前网络下不稳定的 `api.binance.com`。
- Binance USD-M Futures 实时成交流：聚合成交 WebSocket 已收到数据帧。
- Binance USD-M Futures 标记价格/资金费率流：WebSocket 已收到数据帧。
- Twelve Data：股票、ETF、外汇和加密批量报价已经返回。
- 新加坡 SSH 只读中继：Binance 现货和 USD-M 合约多币种报价已经返回。

## 可以使用，但必须加保护

- GDELT DOC API：前两次实测成功，快速重复运行后返回 429。实现时必须有缓存、指数退避和低频轮询，不能按实时行情频率调用。
- BLS RSS：官网正式地址在当前网络返回 403；同一来源的 BLS Public Data API 已通过，因此首版只接 API，不把 RSS 作为硬依赖。

## 当前本机限制与替代方案

- 本机直连 Binance USD-M Futures REST：`fapi.binance.com` 返回 HTTP 451。
- 已验证替代方案：通过新加坡服务器只访问 Binance 公共行情端点，现货价格、合约价格、开放兴趣和资金费率均可返回；不调用现有量化程序或交易账户接口。

## 还没有被证明，等待本地凭证

- SEC Submissions API：需要带真实联系邮箱的 `SEC_USER_AGENT`。
- BEA Data API：需要 `BEA_API_KEY`。
- Marketaux News API：需要 `MARKETAUX_API_TOKEN`。
- Alpaca IEX Snapshot / Historical News：需要 Alpaca key/secret。
- Telegram `getMe` / `getChat`：需要 bot token，检查目标聊天还需要 chat id；预检不会发消息。
- FRED API：需要 `FRED_API_KEY`。

## Gate 0 结论

链路不是“全部可用”，但首个垂直切片已经有一条可工作的外部路径：

`Fed/BLS/SEC 事件 -> Twelve Data 多资产行情 + 新加坡 Binance 加密行情 -> 本地归一化与规则判断`

下一步先补 `.env` 中已有的免费凭证并复跑；在所有目标项变成 PASS 或有明确替代方案前，不开始数据库、LLM 编排和完整前端。
