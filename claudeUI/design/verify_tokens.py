"""Verify that the self-contained prototype embeds every canonical design token."""

from __future__ import annotations

import re
from pathlib import Path


ROOT = Path(__file__).resolve().parent
TOKEN_FILE = ROOT / "tokens.css"
PROTOTYPE = ROOT.parent / "prototype" / "index.html"
PATTERN = re.compile(r"(--fr-[\w-]+)\s*:\s*([^;]+)")


def tokens(path: Path) -> dict[str, str]:
    return {
        name: re.sub(r"\s+", "", value)
        for name, value in PATTERN.findall(path.read_text(encoding="utf-8"))
    }


def main() -> None:
    canonical = tokens(TOKEN_FILE)
    embedded = tokens(PROTOTYPE)
    missing = sorted(canonical.keys() - embedded.keys())
    changed = sorted(
        name
        for name in canonical.keys() & embedded.keys()
        if canonical[name] != embedded[name]
    )
    if missing or changed:
        raise SystemExit(f"token drift: missing={missing}, changed={changed}")
    print(f"token_sync_ok canonical={len(canonical)} embedded={len(embedded)}")


if __name__ == "__main__":
    main()
