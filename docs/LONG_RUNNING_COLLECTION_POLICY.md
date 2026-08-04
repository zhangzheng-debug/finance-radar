# Finance Radar 长期采集与证据留存策略

## 运行目标

Finance Radar 应作为只读情报采集系统长期运行。服务器 Worker 每 300 秒启动一轮；
systemd 设置开机自启、进程异常后 20 秒重启，并在主 unit 中固定
`MemoryHigh=380M`、`MemoryMax=520M`、`TasksMax=128`。超过软阈值先由 systemd
施加回收/节流压力；达到硬上限时按正常重启策略恢复。单一来源失败只能使当轮降级，不能阻断
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

- VPS 在线 SQLite 备份：只保留最近 1 份已通过恢复验证的日备份。
- VPS 周快照：当前不保留（`weekly-retention=0`）。新备份只有在账本、运维库和
  清单恢复验证通过后，才会删除上一份。
- Windows 异机备份：每日拉取、AES-256-GCM 加密、完整隔离恢复验证，保留策略独立。
- 换机前必须再生成一次最终异机快照；旧 VPS 只在新端点验收通过后下线。

2026-07-19 的迁移快照和旧报告中出现的“30 份日备份、12 周快照”仅描述当时的
历史状态，不能作为当前保留策略或容量估算依据。

## 备份锁与可用性

备份器使用带 PID、令牌和时间戳的专用锁，避免两个备份同时清理同一份恢复包。锁在
6 小时租约到期且记录的 owner 进程已不存活时，才会被原子隔离并由下一次备份自动恢复；
它不会因为一次定时触发失败就删除一个仍可能在运行的锁。若日志显示锁占用，先确认
`finance-radar-backup.service` 不在运行、核对锁的年龄和 PID，并保留 `journalctl` 证据；
不要用计划任务或粗暴的 `rm` 自动清锁。

## 容量与停止条件

当前证据对象不做自动删除。运维应在磁盘使用率达到 70% 时评估扩容，在 80% 时暂停
新增大对象而不是删除既有证据。事件元数据和已有原文必须继续可读；不得通过清空数据库
或覆盖备份恢复空间。

## 验证命令

```bash
systemctl is-active finance-radar-worker finance-radar-backup.timer
systemctl is-enabled finance-radar-worker finance-radar-backup.timer
systemctl show finance-radar-worker -p Restart -p RestartUSec -p NRestarts -p MemoryHigh -p MemoryMax -p TasksMax
systemctl list-timers finance-radar-backup.timer --no-pager
df -h /opt/finance-radar
journalctl -u finance-radar-worker --since "-20 min" --no-pager
journalctl -u finance-radar-backup.service --since "-2 days" --no-pager
```

公网 Operations 页面和 `/api/v1/health` 同时展示最近 Worker、来源游标、备份和证据对象数量。
