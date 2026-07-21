# Finance Radar Evidence Terminal v2 AWS deployment

- Date: 2026-07-21 (Asia/Shanghai)
- Target: AWS EC2 `i-0fa9bfafa5eab00bf` (`18.208.34.152`)
- Public URL: `https://radar.18-208-34-152.sslip.io:8443/radar/`
- Mode: `LIVE CORE · READ ONLY`

## Deployed artifacts

- UI source: `claudeUI/prototype/index.html`
- UI SHA-256: `61f0c011188e58dbb7a3d9cbaaf80be759ab4b32693c517caf7667e7e7342a47`
- Server UI path: `/var/www/finance-radar-terminal/index.html`
- Nginx source: `claudeUI/deployment/nginx-finance-radar-static.conf`
- Nginx SHA-256: `7720f745885c4ac38b2046184df6d96a5236161fd02f01c463d0f2effd9fb138`
- Server Nginx path: `/etc/nginx/conf.d/finance-radar-aws.conf`

## Safety and rollback

- Previous routing was backed up to `/opt/finance-radar/ui-backups/20260720T184500Z/finance-radar-aws.conf` before cutover.
- The Streamlit service remains active on `127.0.0.1:18501`; it was not deleted or rewritten.
- API, Worker, evidence model, database, backup timer, Xray, and WireGuard were not restarted or reconfigured.
- The public UI exposes only read-only API operations and visibly states `NO TRADING`.

Rollback:

```bash
sudo cp /opt/finance-radar/ui-backups/20260720T184500Z/finance-radar-aws.conf \
  /etc/nginx/conf.d/finance-radar-aws.conf
sudo nginx -t
sudo systemctl reload nginx
```

## Acceptance evidence

- Public UI returned HTTP `200` with the expected 101,790-byte pre-label-adjustment artifact and then the final content-addressed artifact above.
- Browser state showed `LIVE CORE · READ ONLY`, schema `12`, `quick_check=ok`, and no horizontal overflow.
- Situation Room loaded `1,352` live events, `546` verified events, `455` review candidates, and `2,396` evidence edges at acceptance time.
- A real `Laser Photonics Corp` SEC event opened in the workbench with its event identity, timeline, evidence hard gate, shadow routing, and explicit unavailable-market-data state.
- Situation, Workbench, Replay, Operations/Model, and Adjudication views all navigated successfully.
- API health after cutover: `status=ok`, Worker `SUCCESS`, backup `VERIFIED`, trading-boundary violations `0`.
