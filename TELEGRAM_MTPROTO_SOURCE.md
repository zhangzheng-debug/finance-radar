# Telegram 个人账号只读事件源

## 边界

- 个人账号 MTProto 只用于读取已能访问的频道，作为事件发现源。
- 不发消息，不回复，不自动加入或退出频道，不调用交易接口。
- `mr_finance_radar_bot` 仍只负责后续输出；两条链路彼此隔离。
- 原文保存在本机 SQLite，用于内部分析。对外输出应以摘要和原帖链接为主。

## 一次性配置

1. 在 `https://my.telegram.org/apps` 创建 API 应用，得到 `api_id` 和 `api_hash`。
2. 把它们写入本机 `.env`：

   ```text
   TELEGRAM_API_ID=
   TELEGRAM_API_HASH=
   ```

3. 安装依赖并初始化数据库：

   ```powershell
   python -m pip install -r requirements.txt
   python scripts/telegram_mtproto_listener.py --init-db
   ```

4. 扫码授权个人账号。二维码只短暂写入 `data/telegram/telegram_login_qr.png`，完成后自动删除：

   ```powershell
   python scripts/telegram_mtproto_listener.py --authorize-qr
   ```

## 频道配置

编辑本机 `config/telegram_channels.json`。频道必须是账号已经能读取的公开或已加入频道；程序不会自动加入。

```json
{
  "channels": [
    {
      "handle": "channel_username",
      "tier": "discovery",
      "enabled": true,
      "note": "用途说明"
    }
  ]
}
```

层级：`primary`（官方/一手）、`secondary`（可信媒体/研究机构）、`discovery`（线索源，需交叉验证）。

## 验证和运行

```powershell
python scripts/telegram_mtproto_listener.py --probe
python scripts/telegram_mtproto_listener.py --backfill 20
python scripts/telegram_mtproto_listener.py
```

数据库默认位于 `data/finance_radar.sqlite3`，记录新增、编辑和删除状态。会话文件、数据库、真实频道清单和 `.env` 均已排除在版本控制之外。
