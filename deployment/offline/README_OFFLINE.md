# Finance Radar executable offline defense snapshot

This bundle is a read-mostly, evidence-linked snapshot for demonstrations when
the public VPS or external networks are unavailable. It contains selected real
ledger events, exact evidence passages, frozen replay cases, the five-page web
terminal, the read-mostly API and the accepted shadow model artifact.

It deliberately contains no `.env`, credential, Telegram sender/listener,
collector, worker, broker client, order route or trading project. The launcher
also installs a process-wide Python network guard that allows loopback only.

## Run on Windows

1. Extract the ZIP to a writable directory.
2. Ensure Python 3.11+ and the packages in `requirements-offline.txt` are
   already installed. The project development machine already satisfies this.
3. Run `powershell -ExecutionPolicy Bypass -File .\start_offline_demo.ps1`.
4. Open `http://127.0.0.1:18701` if it does not open automatically.
5. Stop with `powershell -ExecutionPolicy Bypass -File .\stop_offline_demo.ps1`.

Run `python .\scripts\verify_offline_demo.py --bundle-root .` to repeat the
integrity, API, replay, five-page render, no-network and no-trading checks.

## Honest scope

- This is a frozen snapshot, not a claim that fresh news is being collected.
- Replay uses a simulated clock and the same downstream shadow router.
- The model stays `SHADOW`; the failed external-blind gate remains visible.
- Adjudication samples are unlabeled until independent humans review them.
- Source URLs are retained for provenance, but the network guard prevents them
  from being fetched during an offline demonstration.
