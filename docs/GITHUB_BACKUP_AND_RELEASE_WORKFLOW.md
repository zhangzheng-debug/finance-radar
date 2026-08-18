# GitHub backup and release workflow

This repository separates maintainable source history from large immutable
backup artifacts. That makes ordinary clones small while public Releases carry
only artifacts that are safe for public distribution. Production recovery data
uses a separately controlled private store.

## Repository model

- `main`: reviewed, reproducible baseline
- `codex/<topic>` or another short feature branch: one focused update
- tag `vYYYY.MM.DD.N`: accepted backup/release point
- public GitHub Release with the same tag: public deploy/demo/evidence archives
- separate private repository/object store: encrypted production migration
  snapshots, with passphrases held somewhere else

Never develop by editing a generated deployment archive. Change the source
tree, run tests, build a new archive, then publish a new tag and Release.

## Routine update

```powershell
git switch main
git pull --ff-only
git switch -c codex/short-description
python -m pytest -q
git add <intended-files>
git diff --cached --check
git diff --cached --stat
git commit -m "Describe the focused change"
git push -u origin codex/short-description
```

Open a pull request, wait for CI, and merge only when the acceptance evidence
matches the change. Do not mix unrelated generated reports into a code-only
change.

## New accepted backup version

1. Finish and verify the application change.
2. Update `VERSION`, `CHANGELOG.md`, `ACCEPTANCE_STATUS.md`, and this inventory
   when the recovery point changes.
3. Build a fresh deploy archive, offline demo, and defense evidence bundle.
4. Pull and independently audit a fresh encrypted VPS migration snapshot.
5. Generate a new `release/*.json` manifest containing filenames, byte sizes,
   and SHA-256 values; never include a passphrase.
6. Commit and merge the metadata.
7. Create the matching annotated tag and public GitHub Release.
8. Upload only artifacts reviewed as safe for public distribution, then
   download or query them once to confirm asset names, sizes and digests.
9. Move the encrypted production migration snapshot to the separately
   controlled private recovery store and verify its identity there. Never
   upload it to this repository's Release.

Example after the version commit is on `main`:

```powershell
$version = (Get-Content VERSION).Trim()
git tag -a "v$version" -m "Finance Radar v$version"
git push origin "v$version"
gh release create "v$version" --title "Finance Radar v$version" --notes-file BACKUP_INVENTORY.md
gh release upload "v$version" <archive-paths> --clobber
```

## Recovery rule

The Git repository and public Release restore development and public deployment
artifacts. The encrypted migration asset in the separate private store restores
production history and server runtime. The passphrase must come from a different
operator-controlled recovery location described in
`docs/SERVER_MIGRATION_HANDOFF.md`; GitHub alone must never be sufficient to
decrypt production data.
