# Security policy

## Scope

Finance Radar is a read-only financial-event intelligence project. It must not
gain order-entry, position, balance, account-management, or trading-execution
routes. The existing quantitative-trading project on the Singapore VPS is out
of scope and must not be copied, modified, started, stopped, or restored here.

## Never commit

- `.env` files other than `.env.example`
- Telegram bot tokens, API IDs/hashes, MTProto session files, or channel lists
- broker, exchange, market-data, or LLM credentials
- SSH keys, TLS private keys, cookies, recovery keys, or backup passphrases
- plaintext production databases or unencrypted server snapshots

This repository is public. Public GitHub Releases inherit repository
visibility and therefore must contain only artifacts explicitly safe for
public distribution. New production migration/recovery archives belong in an
operator-controlled private repository or private object store; their
passphrases must remain in a different recovery location.

A legacy encrypted migration archive was published before this visibility
contract was corrected. Do not upload another one and do not describe it as a
private Release. Retention, private migration, or deletion of that historical
asset is a separate destructive decision.

## Before every push

Run the test suite and inspect the exact staged diff:

```powershell
python -m pytest -q
git diff --cached --check
git diff --cached --stat
git status --short
```

If a credential is ever committed, remove it from the repository history and
rotate it at the provider. Deleting only the latest line is not sufficient.
