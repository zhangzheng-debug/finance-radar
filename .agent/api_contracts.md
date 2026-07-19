# API contracts

所有成功响应：

```json
{"schema_version":"1.0","trace_id":"...","generated_at":"...","data":{}}
```

所有错误响应：

```json
{"schema_version":"1.0","trace_id":"...","generated_at":"...","error":{"code":"...","message":"...","details":null}}
```

Public/read-mostly:

- `GET /api/v1/health`
- `GET /api/v1/overview`
- `GET /api/v1/sources/health`
- `GET /api/v1/events`
- `GET /api/v1/events/{event_id}`
- `GET /api/v1/events/{event_id}/timeline`
- `GET /api/v1/events/{event_id}/evidence`
- `GET /api/v1/events/{event_id}/trace`
- `GET /api/v1/evidence/archive`
- `GET /api/v1/model/status`
- `GET /api/v1/replays`
- `GET /api/v1/demo/mode`

Controlled mutation; when configured, requires `X-Admin-Token`:

- `POST /api/v1/replays/{case_id}/run`
- `POST /api/v1/replays/{case_id}/reset`
- `POST /api/v1/events/{event_id}/agent/run`
- `POST /api/v1/events/{event_id}/human-override`
- `POST /api/v1/demo/mode/{LIVE|RECENT_CAPTURE|REPLAY}`

`GET /api/v1/evidence/archive` 返回内容对象总数、原始来源快照/精确引文分类、MIME与字节统计、最近对象、策略和最近对象重新计算的SHA-256完整性结果；它只暴露元数据，不下载受版权保护的原始正文。`agent/run` 返回结构化 `EventClaim`、`EvidenceEdge`、引用摘要、工具轨迹和守卫状态；无获批 LLM 配置时必须返回 `llm_used=false` 与实际 provider，不得伪称大模型调用。`human-override` 仅记录人工复核前后状态、人员与理由，不改变事实账本或触发任何交易动作。

Forbidden endpoints: orders, positions, balances, brokerage accounts and trade execution. Adding one is an architecture violation, not a feature.
