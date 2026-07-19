# Architecture

Finance Radar 是模块化单体：同一镜像以 API、Web、Worker、Backup、Notifier 五种命令运行，Caddy 统一提供 HTTPS。

```text
official feeds / aggregators / read-only quotes
                    |
              continuous worker
                    |
       Schema 12 SQLite WAL ledger
                    |
       official HTML/PDF snapshotter
         |                    |
 SHA-256 object store    Schema 3 operations
                    |
          read-only repository adapter
              /                 \
        FastAPI                 shadow model
          |                          |
 Streamlit Situation Room       replay service
          |
 Telegram deep links (secondary output)
```

核心原则：原始观测不可变、修订追加、事件版本追加、证据边可追溯、行情只读且禁止进入训练特征、所有模型输出为 shadow、默认无 Telegram 外部写入。经人工证据路由选中的官方页面由快照器保存原始 HTML/PDF 字节：仅 HTTPS、仅注册官方域后缀、重定向后重新校验域名、单对象最多10 MiB、仅接受HTML/PDF，并按SHA-256内容寻址；越域链接只记`policy_skipped`。快照只提升可复核性，不自动核验事实、不进入模型特征、不触发交易。加密资产的持久化行情使用 Binance 公共免认证仅行情接口，非加密资产使用 Twelve Data，IBKR 仅作操作者本机只读能力探针；三者在 Operations 中分别披露，不混成一个虚假“全市场实时源”。事件后的市场观察以首次真实观察快照为基线，分别调度 T+5m/T+30m/T+1d；超出宽限期则写入 `MISSED_WINDOW`，不得拿当下最新价伪装历史窗口；仅在基线与实际窗口快照都存在时计算收益，且收益保持 `post_event_audit_only`、`allowed_as_model_feature=0`。运维状态（回放、模型运行、Worker、备份、证据对象和链接）单独存放在 operations SQLite，避免污染研究账本。

PostgreSQL 是未来扩展门，不是当前重写目标。VPS 的专业性由持久卷、WAL、租约、幂等 outbox、在线备份、隔离恢复、健康检查和自动重启证明。
