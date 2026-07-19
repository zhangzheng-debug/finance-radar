# Gate 0：外部依赖预检

这个阶段只回答一个问题：项目依赖的外部来源、API、账号权限和网络链路，是否在当前机器上真的可用。

预检不创建数据库、不调用模型、不发送 Telegram 消息，也不会把密钥写入报告。全部探针使用 Python 标准库，不需要安装第三方包。

## 快速运行

PowerShell：

```powershell
Copy-Item .env.example .env
# 在 .env 中填写已有凭证；暂时没有的可以留空。
python scripts/gate0_external_preflight.py
Get-Content reports/gate0/latest.md
```

本地没有可直接调用的 `python` 时，可使用 Codex 工作区运行时：

```powershell
& 'C:\Users\MR\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe' scripts/gate0_external_preflight.py
```

## 状态含义

- `PASS`：接口真实返回并通过最小结构校验。
- `WARN`：该具体入口失败，但已经有计划内、同来源的替代入口；仍保留为兼容性证据。
- `FAIL`：已发出请求，但发生网络、TLS、HTTP、鉴权或响应结构错误。
- `BLOCKED`：因缺少必要身份、密钥或 Telegram 目标而没有调用；这不是接口失败。

默认只要存在 `FAIL`，脚本就返回非零退出码。若希望缺凭证也让 CI 失败，可增加：

```powershell
python scripts/gate0_external_preflight.py --strict-blocked
```

## 当前探针

无需密钥即可测试：

- Federal Reserve RSS
- BLS RSS 与未注册 Public Data API
- GDELT DOC API
- Binance Spot REST
- Binance 官方 market-data-only Spot REST 备用入口
- Binance USD-M Futures REST、成交 WebSocket 与标记价格/资金费率 WebSocket

配置后测试：

- SEC Submissions API（`SEC_USER_AGENT` 必须含真实联系邮箱）
- BEA Data API
- Marketaux News API
- Alpaca IEX Snapshot 与 Historical News
- Telegram `getMe` 与 `getChat`（只读，不发送消息）
- FRED API

每次运行会在 `reports/gate0/` 生成带 UTC 时间戳的 JSON 和 Markdown 证据，同时更新 `latest.json` 与 `latest.md`。

## 新加坡 Binance 只读行情中继

当本机访问 Binance REST 受限时，可通过配置好的 SSH 主机调用 Binance 公开行情端点：

```powershell
python scripts/remote_binance_quotes.py --market both
python scripts/remote_binance_quotes.py --market spot --symbols BTCUSDT ETHUSDT SOLUSDT
python scripts/remote_binance_quotes.py --market usdm --symbols BTCUSDC ETHUSDC XRPUSDC
```

该适配器有固定安全边界：

- 只执行固定的远程 `curl` 公共行情请求；
- 不读取远端项目目录或 `.env`；
- 不使用 Binance 账户/API 凭证；
- 不包含账户、持仓、下单、撤单或服务管理方法；
- 输出中显式记录上述安全断言。

## Twelve Data 多资产探针

配置 `TWELVE_DATA_API_KEY` 后，Gate 0 用一次批量请求验证：

- 股票：`AAPL`
- ETF：`SPY`
- 外汇：`EUR/USD`
- 加密：`BTC/USD`

报告只记录 Key 是否存在，不记录 Key 值。
