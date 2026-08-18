# Deployment runbook

This file is a short operator index. Authoritative commands and prerequisites
are in [`deployment/README.md`](../deployment/README.md); repository and live
evidence boundaries are in [`CURRENT_STATE.md`](../CURRENT_STATE.md).

## Current-state rule

No hostname, IP address, release symlink, service state, event age, backup age,
or public URL in a historical report is evidence of the current production
deployment. Before any release or incident action, identify the authenticated
AWS account and region and verify the target instance, active release, systemd
units, Nginx candidate, worker cycle, database integrity, and restore receipt.

Do not encode a permanent "current" tag in this runbook. The latest accepted
release and recovery baseline are the timestamped facts in `CURRENT_STATE.md`,
and they must still be rechecked on the authenticated host before an action.
At the 2026-08-18 audit the deployed tag remained `v2026.08.15.4`; that dated
observation is not authority to skip a later live check. A development branch
remains unreleased until its exact SHA passes local and GitHub gates and is
separately deployed and accepted on the target host.

## Release order

1. Verify a clean candidate commit and the no-trading route gate.
2. Create a quiesced backup and complete its isolated restore verification.
3. Keep the previous release and backup until the new candidate passes all
   loopback, public-edge, worker, memory, event-freshness, and rollback checks.
4. Install through `deployment/systemd/install_remote.sh`; never replace the
   `current` symlink or reload Nginx by hand.
5. Delete the previous daily backup only after the new bundle is verified.

The public edge must expose only the read-only Streamlit product. FastAPI,
OpenAPI, admin pages, model operations, and mutation endpoints remain loopback
only. Telegram remains dry-run unless an operator separately supplies
`--send`; this runbook grants no trading or external-message authority.

## Historical evidence

Dated migration, load, UI, and runtime reports are retained for audit and
recovery design. They must not be relabeled as a current endpoint or current
health result. Use environment-supplied host parameters when running operator
scripts; do not add a current server address to this file.
