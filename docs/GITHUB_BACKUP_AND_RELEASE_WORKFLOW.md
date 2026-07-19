# GitHub backup and release workflow

This repository separates maintainable source history from large immutable
backup artifacts. That makes ordinary clones small while preserving a complete
recovery point in GitHub Releases.

## Repository model

- `main`: reviewed, reproducible baseline
- `codex/<topic>` or another short feature branch: one focused update
- tag `vYYYY.MM.DD.N`: accepted backup/release point
- GitHub Release with the same tag: deploy/demo/evidence archives and, when
  needed, the encrypted migration snapshot

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
7. Create the matching annotated tag and private GitHub Release.
8. Upload large archives as Release assets, then download or query them once to
   confirm asset names and sizes.

Example after the version commit is on `main`:

```powershell
$version = (Get-Content VERSION).Trim()
git tag -a "v$version" -m "Finance Radar v$version"
git push origin "v$version"
gh release create "v$version" --title "Finance Radar v$version" --notes-file BACKUP_INVENTORY.md
gh release upload "v$version" <archive-paths> --clobber
```

## Recovery rule

The Git repository restores development history. The encrypted migration asset
restores production history and server runtime. The passphrase must come from
the operator-controlled recovery location described in
`docs/SERVER_MIGRATION_HANDOFF.md`; GitHub alone must never be sufficient to
decrypt production data.
