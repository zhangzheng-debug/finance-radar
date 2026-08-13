# Finance Radar release audit

`scripts/release_audit.py` creates a release record and verifies it without
committing code, contacting a cloud provider, restarting a service or deploying
anything. It uses only the Python standard library and the local `git` command,
so the same workflow runs from Windows PowerShell and Linux.

The record consists of four files:

- `<release-id>.release-manifest.json` — machine-readable source identity,
  critical-file hashes, archive hash, declared checks and rollback contract;
- `<release-id>.release-manifest.md` — concise human-readable summary;
- `<release-id>.rollback-checklist.md` — pre-cutover values, rollback triggers
  and a non-destructive rollback sequence;
- `<release-id>.release-records.SHA256` — hashes for all three records.

The SHA-256 sidecar detects corruption and mismatched handoffs; it is not a
cryptographic signature or proof of who created the release.

## Release identity contract

When Git identity is available, `release_audit.py` derives the release ID as
`YYYYMMDDTHHMMSSZ-<12-character-commit>`, for example
`20260804T010203Z-a1b2c3d4e5f6`. The release record, encrypted migration audit,
restore preparer, Windows orchestrator and Linux activation script use the same
path-safe contract: 1 to 96 ASCII characters, starting with an alphanumeric
character and then only letters, digits, `.`, `_` or `-`. This makes the ID one
safe component below `/opt/finance-radar/releases`; separators, whitespace,
quotes and shell metacharacters are rejected. Older timestamp-only release IDs
remain valid for recovery of historical snapshots.

The tool never reads environment values, Git diff contents or Git remotes. It
rejects `.env`, private-key, session and passphrase filenames, scans critical
text/code files for high-confidence live credentials, audits tar/zip member
paths, and verifies that every critical file inside the release archive exactly
matches the workspace hash. It records absolute paths only in console errors or
normal shell output; absolute workspace paths are not written into the record.

## Readiness states

- `READY`: clean Git source and every declared verification is `PASS`.
- `READY_WITH_DECLARED_DIRTY_SOURCE`: the worktree is dirty, the operator used
  `--allow-dirty`, and exactly one tar/zip was bound to a complete explicit
  source inventory. The inventory records every packaged path, type/mode and
  normalized content hash, and requires every tracked or unignored source file
  to appear in that archive. A partial critical-file-only archive is refused.
- `READY_WITH_EXPLICIT_SOURCE_ID`: Git identity is unavailable, an explicit
  release ID was supplied, and a complete archive was hash-bound.
- `REVIEW_REQUIRED_*`: incomplete checks, dirty source without an exception, or
  incomplete source identity. `--strict` writes the audit files and then exits
  non-zero so the evidence is retained.
- `BLOCKED_VERIFICATION_FAILED`: at least one declared check is `FAIL`.

Verification declarations use only `NAME=STATUS`; supported statuses are
`PASS`, `FAIL`, `SKIPPED`, and `NOT_RUN`. Do not mark a check `PASS` until its
evidence actually exists.

## Windows workflow

Keep release archives and records on `D:` so they do not consume the constrained
system drive. Build the deployment archive first with the normal approved
packaging process; it must place repository files at the archive root and must
exclude `.env`, `.git`, private keys, sessions and recovery material.

```powershell
$releaseId = (Get-Date).ToUniversalTime().ToString('yyyyMMddTHHmmssZ')
$artifact = "D:\FinanceRadarReleases\$releaseId\finance-radar-$releaseId.tgz"
$records = "D:\FinanceRadarReleases\$releaseId\records"

python scripts\release_audit.py create `
  --root . `
  --release-id $releaseId `
  --artifact $artifact `
  --output-dir $records `
  --verification pytest=PASS `
  --verification bash_syntax=PASS `
  --verification compose_config=PASS `
  --allow-dirty `
  --strict

python scripts\release_audit.py verify `
  --manifest "$records\$releaseId.release-manifest.json" `
  --root . `
  --artifact $artifact `
  --expected-release-id $releaseId `
  --require-ready `
  --require-sidecar `
  --require-artifact `
  --report-dir "$records\acceptance"
```

Omit `--allow-dirty` for a clean worktree. If no archive is supplied, the tool
can still document a clean Git release, but a dirty-source exception is refused.
Browser checks against the new public endpoint are post-deployment evidence;
store them beside the release records instead of claiming them in the
pre-deployment manifest.

For a Compose release, set `FINANCE_RADAR_RELEASE_ID` to the manifest release ID
and `FINANCE_RADAR_GIT_COMMIT` to its recorded commit before running
`docker compose config`. Compose uses them only for the image tag and OCI
container labels; the public Web container still receives only its three
non-secret runtime variables.

## Linux verification and systemd installer gate

The same create/verify commands work on Linux. The systemd installer remains an
explicit operator action and accepts the manifest as an optional fifth argument;
the optional sixth argument is the canonical public Web URL:

```bash
sudo deployment/systemd/install_remote.sh \
  /tmp/finance-radar-deploy.tgz \
  "$RELEASE_ID" \
  "$ARCHIVE_SHA256" \
  /tmp/finance-radar-source.env \
  "/tmp/$RELEASE_ID.release-manifest.json" \
  "https://radar.18-208-34-152.sslip.io:8443/radar"
```

Place `<release-id>.release-records.SHA256` beside the JSON manifest. When the
fifth argument is present, the installer verifies all of the following before
changing shared data, `/opt/finance-radar/current`, or any service unit:

- a Python tar preflight rejects traversal, links, devices, sensitive names,
  duplicate paths, setuid/setgid entries and excessive expanded size before
  `tar -xzf` writes the candidate release;
- explicit release ID equals the manifest release ID;
- the manifest JSON matches its SHA-256 sidecar;
- at least one complete release archive is declared;
- the uploaded archive matches the manifest hash and passes member-path safety;
- all critical files in the extracted release and inside the archive have the
  expected byte count and SHA-256;
- for a declared-dirty source, every inventory-listed release file in the
  extracted tree and every path/type/mode/content record in the supplied archive
  match the manifest's complete source inventory;
- declared release readiness starts with `READY`.

The installer stores the verified manifest and acceptance report under the new
release's `release-records/` directory. Omitting the fifth argument preserves
backward compatibility and prints `release_manifest=not_supplied`; production
cutovers should use the verified form.

Before any release switch, a verified production cutover also requires the
manual admin UI to be inactive and not boot-enabled and the backup service to be idle.
The installer then launches a one-shot, resource-bounded candidate backup bridge;
it uses candidate source without changing the active service unit or `current`, and
it does not migrate or write to the live operations database or its recovery-copy
schema. Only a successful full two-database recovery bundle can proceed to
cutover. The candidate source remains root-owned and runtime-read-only during the
bridge, and the verified bundle is physically copied to a root-only hold before
the release switch. Its activation receipt records the fresh snapshot ID and
manifest SHA-256. The backup policy remains one newest verified daily bundle, so a
failed backup never deletes the prior recovery point; exceptional failed-cutover
holds are retained for review and capped by an explicit two-hold operator gate.

## Rollback handoff

Before cutover, open the generated rollback checklist and record:

1. the exact result of `readlink -f /opt/finance-radar/current`;
2. the timestamped Nginx/systemd configuration backup directory;
3. the fresh SQLite backup identifier/hash and `quick_check=ok` evidence;
4. the operator and UTC maintenance window.

Rollback repoints the symlink to the recorded previous release, restores config
only when it changed, validates Nginx before reload, and repeats loopback, data,
public-read-only and edge-deny checks. It deliberately leaves the failed release
in place for diagnosis and keeps `finance-radar-admin` stopped.
