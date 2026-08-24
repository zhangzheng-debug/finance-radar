# Windows 老板入口

日常使用只需运行：

```powershell
powershell -ExecutionPolicy Bypass -File deployment/windows/Open-FinanceRadar-Backend.ps1
```

首次运行会要求填写 SSH 地址和位于 `D:` 的私钥路径，并只把这两个连接参数保存到
`D:\FinanceRadar\owner-backend.json`；不保存 API 令牌。以后运行同一命令会直接打开
Admin 的“老板总览”，不再要求在三个内部服务之间选择。窗口关闭或按 `Ctrl+C` 后，
启动器会关闭自己的隧道和服务。

The launcher opens one short-lived, loopback-only Finance Radar work surface.
It does not contain a default host, identity, token or public route, and it does
not write temporary files to `C:`. SSH keys and the repository may remain on
`D:`.

Review the exact commands first:

```powershell
python scripts/open_internal_ui.py `
  --host ubuntu@YOUR_EXPLICIT_HOST `
  --identity-file "D:\Keys\finance-radar.pem" `
  --role admin `
  --dry-run
```

Start the owner session directly (Admin is now the safe default):

```powershell
python scripts/open_internal_ui.py `
  --host ubuntu@YOUR_EXPLICIT_HOST `
  --identity-file "D:\Keys\finance-radar.pem"
```

Reviewer/Operator are advanced work surfaces. Use `--choose-role` only when a
specific maintenance or human-review session is required.

The launcher refuses to take ownership when any internal UI service is already
active, starts exactly one manual systemd unit, forwards only a local
`127.0.0.1` port, and opens the corresponding local URL. Keep the terminal open
and press `Ctrl+C` when finished; the launcher closes the tunnel and stops only
the service it started. If automatic cleanup reports a failure, use the exact
`stop` command printed by `--dry-run`.

Run `python scripts/open_internal_ui.py --help` for optional SSH/local-port and
browser flags. The internal units remain mutually exclusive, have no systemd
`[Install]` section, and must never receive a public Nginx route.
