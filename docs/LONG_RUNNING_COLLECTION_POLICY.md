# Finance Radar 长期采集与证据留存策略

## 运行目标

Finance Radar 应作为只读情报采集系统长期运行。服务器 Worker 每 300 秒启动一轮；
systemd 设置开机自启、进程异常后 20 秒重启。单一来源失败只能使当轮降级，不能阻断
其他来源、事件账本、行情窗口或后续周期。

## 永久数据

- `canonical_events`、`raw_observations`、`event_versions`、`event_evidence` 和行情窗口没有 TTL。
- 官方原文以 SHA-256 内容寻址保存；相同字节去重，但不同事件与证据链接分别保留。
- 原文存档仅允许登记过的官方域名，限制 HTTPS、重定向复核、HTML/PDF/JSON MIME 和单文件 10 MiB。
- 官方 Feed 遗留的 HTTP 链接只允许在登记域名内升级为 HTTPS，最终跳转仍需再次验域名。
- SEC EDGAR、BLS、Fed、FDA、Treasury 和 MARAD 等登记官方来源按每轮最多 4 份逐步补档，避免突发流量。
- 扫描器按页越过已归档记录和失败记录；单个长期 403/超时不能把后续证据永久挡在前 100 条之后。
- 原始证据不参与自动核验，不进入模型特征，也不产生任何交易能力。

## 备份层次

- VPS 在线 SQLite 备份：保留最近 30 份日备份。
- VPS 周快照：保留最近 12 周。
- Windows 异机备份：每日拉取、AES-256-GCM 加密、完整隔离恢复验证，保留策略独立。
- 换机前必须再生成一次最终异机快照；旧 VPS 只在新端点验收通过后下线。

## 容量与停止条件

当前证据对象不做自动删除。运维应在磁盘使用率达到 70% 时评估扩容，在 80% 时暂停
新增大对象而不是删除既有证据。事件元数据和已有原文必须继续可读；不得通过清空数据库
或覆盖备份恢复空间。

## 验证命令

```bash
systemctl is-active finance-radar-worker finance-radar-backup.timer
systemctl is-enabled finance-radar-worker finance-radar-backup.timer
systemctl show finance-radar-worker -p Restart -p RestartUSec -p NRestarts
systemctl list-timers finance-radar-backup.timer --no-pager
df -h /opt/finance-radar
journalctl -u finance-radar-worker --since "-20 min" --no-pager
```

公网 Operations 页面和 `/api/v1/health` 同时展示最近 Worker、来源游标、备份和证据对象数量。
