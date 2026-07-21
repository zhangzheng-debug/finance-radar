# AWS static terminal deployment

The production UI is the single-file `../prototype/index.html`. It is served by
Nginx at `/radar/` and reads the existing same-origin, read-only API at
`/finance-radar-api`.

Production paths:

- UI artifact: `/var/www/finance-radar-terminal/index.html`
- Nginx config: `/etc/nginx/conf.d/finance-radar-aws.conf`
- Deployment backups: `/opt/finance-radar/ui-backups/<UTC timestamp>/`

The existing `finance-radar-web.service` is intentionally left active as a
rollback backend on `127.0.0.1:18501`. The UI cutover changes only Nginx routing;
it does not restart the API, worker, model, database, backup timer, Xray, or
WireGuard.

Rollback:

```bash
sudo cp /opt/finance-radar/ui-backups/<UTC timestamp>/finance-radar-aws.conf \
  /etc/nginx/conf.d/finance-radar-aws.conf
sudo nginx -t
sudo systemctl reload nginx
```
