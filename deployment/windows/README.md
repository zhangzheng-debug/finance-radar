# Windows internal UI launcher

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

Start an interactive session (omit `--role` to choose Admin, Reviewer or
Operator from a numbered menu):

```powershell
python scripts/open_internal_ui.py `
  --host ubuntu@YOUR_EXPLICIT_HOST `
  --identity-file "D:\Keys\finance-radar.pem"
```

The launcher refuses to take ownership when any internal UI service is already
active, starts exactly one manual systemd unit, forwards only a local
`127.0.0.1` port, and opens the corresponding local URL. Keep the terminal open
and press `Ctrl+C` when finished; the launcher closes the tunnel and stops only
the service it started. If automatic cleanup reports a failure, use the exact
`stop` command printed by `--dry-run`.

Run `python scripts/open_internal_ui.py --help` for optional SSH/local-port and
browser flags. The internal units remain mutually exclusive, have no systemd
`[Install]` section, and must never receive a public Nginx route.
