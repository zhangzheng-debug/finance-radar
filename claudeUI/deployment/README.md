# Archived static prototype — not a deployment path

This directory is retained only as a historical design reference. It must not
be copied to Nginx, served at `/radar/`, or used as an alternate production
frontend. The old static terminal can fall back to synthetic snapshots and
expects a public API route which production now deliberately denies.

There is one authoritative production UI and one release chain:

- Public UI: Streamlit behind `deployment/systemd/nginx-radar-direct.conf` at
  `/radar/`.
- Private review UI: loopback-only `finance-radar-admin.service`, opened only
  through an authenticated operator tunnel.
- Deployment: `deployment/systemd/install_remote.sh` plus its versioned release
  manifest and post-cutover recovery-bundle gate.

`nginx-finance-radar-static.conf` is intentionally inert and contains no Nginx
`server` block. Keeping it in this archive prevents an old operational note
from becoming an accidental second production surface. For design exploration, open `../prototype/index.html`
locally and treat every displayed snapshot as design-only unless it is
explicitly integrated into the Streamlit production UI.
