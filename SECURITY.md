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

The encrypted migration archive is distributed only as a private GitHub
Release asset. Its passphrase is deliberately stored outside this repository.

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
