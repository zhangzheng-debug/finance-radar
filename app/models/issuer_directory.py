"""Deterministic issuer-name and ticker resolution for market observation.

This module resolves only the instrument used for read-only price observation.
It does not verify an event, change its status, infer price direction, or create
any trading capability.
"""

from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping


CASHTAG_PATTERN = re.compile(r"(?<![A-Za-z0-9])\$([A-Z][A-Z0-9.-]{0,14})\b")
EDITORIAL_PREFIXES = frozenset(
    {"alert", "breaking", "exclusive", "new", "news", "update", "updated"}
)
LEGAL_SUFFIXES = frozenset(
    {
        "ag",
        "co",
        "company",
        "corp",
        "corporation",
        "group",
        "holding",
        "holdings",
        "inc",
        "incorporated",
        "limited",
        "llc",
        "lp",
        "ltd",
        "nv",
        "plc",
        "sa",
        "se",
    }
)
ISSUER_EVENT_PATTERNS = (
    re.compile(
        r"(?:bankrupt|capital|clinical|company|corporate|credit|delist|distress|"
        r"earning|financ|fund|governance|guidance|liquidity|listing|management|"
        r"merger|acquisition|operation|product|recall|regulatory|restructur)"
    ),
)


@dataclass(frozen=True)
class IssuerSecurity:
    name: str
    ticker: str
    exchange: str


@dataclass(frozen=True)
class IssuerResolution:
    company_name: str
    ticker: str
    exchange: str
    confidence: float
    reason_code: str
    directory_sha256: str


@dataclass(frozen=True)
class IssuerDirectory:
    """Immutable, content-addressed lookup built from the SEC ticker index."""

    aliases: Mapping[str, tuple[IssuerSecurity, ...]]
    tickers: Mapping[str, tuple[IssuerSecurity, ...]]
    source_sha256: str
    record_count: int

    @classmethod
    def from_document(
        cls, document: Mapping[str, Any], *, source_sha256: str = ""
    ) -> "IssuerDirectory":
        fields = document.get("fields")
        rows = document.get("data")
        if not isinstance(fields, list) or not isinstance(rows, list):
            raise ValueError("issuer directory has an unexpected structure")
        field_index = {str(name): index for index, name in enumerate(fields)}
        required = {"name", "ticker", "exchange"}
        if not required.issubset(field_index):
            raise ValueError("issuer directory is missing required fields")

        aliases: dict[str, list[IssuerSecurity]] = {}
        tickers: dict[str, list[IssuerSecurity]] = {}
        accepted = 0
        for row in rows:
            if not isinstance(row, list) or len(row) < len(fields):
                continue
            name = str(row[field_index["name"]] or "").strip()
            ticker = str(row[field_index["ticker"]] or "").strip().upper()
            exchange = str(row[field_index["exchange"]] or "").strip()
            if not name or not re.fullmatch(r"[A-Z][A-Z0-9.-]{0,14}", ticker):
                continue
            security = IssuerSecurity(name=name, ticker=ticker, exchange=exchange)
            accepted += 1
            tickers.setdefault(ticker, []).append(security)
            for alias in _issuer_aliases(name):
                aliases.setdefault(alias, []).append(security)

        return cls(
            aliases={key: _dedupe(values) for key, values in aliases.items()},
            tickers={key: _dedupe(values) for key, values in tickers.items()},
            source_sha256=source_sha256,
            record_count=accepted,
        )

    def resolve(self, event: Mapping[str, Any]) -> IssuerResolution | None:
        if not _issuer_event(event):
            return None
        source_title = str(event.get("source_title") or "").strip()
        source_summary = str(event.get("source_summary") or "").strip()
        if not source_title:
            return None

        explicit = {
            match.group(1).upper()
            for match in CASHTAG_PATTERN.finditer(source_title)
            if len(
                [
                    token
                    for token in _normalized_tokens(source_title[: match.start()])
                    if token not in EDITORIAL_PREFIXES
                ]
            )
            <= 1
        }
        if len(explicit) == 1:
            securities = self.tickers.get(next(iter(explicit)), ())
            if _single_ticker(securities):
                return self._resolution(
                    securities[0], confidence=0.995, reason="SOURCE_VALIDATED_CASHTAG"
                )

        title_tokens = _normalized_tokens(source_title)
        while title_tokens and title_tokens[0] in EDITORIAL_PREFIXES:
            title_tokens.pop(0)
        summary_tokens = _normalized_tokens(source_summary)
        candidates: list[tuple[int, IssuerSecurity]] = []
        for tokens in (title_tokens, summary_tokens[:24]):
            if not tokens:
                continue
            max_width = min(8, len(tokens))
            for width in range(max_width, 0, -1):
                # The company must lead the captured title.  A summary is only
                # considered over its first words, keeping unrelated mentions
                # from becoming direct-security mappings.
                alias = " ".join(tokens[:width])
                securities = self.aliases.get(alias, ())
                if not _single_ticker(securities):
                    continue
                if width == 1 and len(alias) < 5:
                    continue
                candidates.append((width, securities[0]))
                break
            if candidates:
                break
        if not candidates:
            return None
        _width, security = max(candidates, key=lambda item: item[0])
        return self._resolution(
            security, confidence=0.985, reason="SOURCE_LEADING_ISSUER_EXACT"
        )

    def _resolution(
        self, security: IssuerSecurity, *, confidence: float, reason: str
    ) -> IssuerResolution:
        return IssuerResolution(
            company_name=security.name,
            ticker=security.ticker,
            exchange=security.exchange,
            confidence=confidence,
            reason_code=reason,
            directory_sha256=self.source_sha256,
        )


def load_issuer_directory(path: str | Path | None) -> IssuerDirectory | None:
    if path is None:
        return None
    selected = Path(path)
    if not selected.is_file() or selected.stat().st_size <= 0:
        return None
    payload = selected.read_bytes()
    document = json.loads(payload.decode("utf-8-sig"))
    if not isinstance(document, dict):
        raise ValueError("issuer directory must be a JSON object")
    return IssuerDirectory.from_document(
        document, source_sha256=hashlib.sha256(payload).hexdigest()
    )


def _dedupe(values: Iterable[IssuerSecurity]) -> tuple[IssuerSecurity, ...]:
    unique: dict[tuple[str, str, str], IssuerSecurity] = {}
    for value in values:
        unique[(value.name, value.ticker, value.exchange)] = value
    return tuple(unique[key] for key in sorted(unique))


def _single_ticker(values: Iterable[IssuerSecurity]) -> bool:
    selected = tuple(values)
    return bool(selected) and len({value.ticker for value in selected}) == 1


def _normalized_tokens(value: str) -> list[str]:
    normalized = unicodedata.normalize("NFKC", value).casefold().replace("&", " and ")
    return re.sub(r"[^a-z0-9]+", " ", normalized).strip().split()


def _issuer_aliases(name: str) -> set[str]:
    tokens = _normalized_tokens(name)
    aliases: set[str] = set()
    if tokens:
        aliases.add(" ".join(tokens))
    stripped = list(tokens)
    while stripped and stripped[-1] in LEGAL_SUFFIXES:
        stripped.pop()
    if stripped:
        aliases.add(" ".join(stripped))
    return {alias for alias in aliases if len(alias) >= 4}


def _issuer_event(event: Mapping[str, Any]) -> bool:
    family = re.sub(
        r"[^a-z0-9]+", " ", str(event.get("event_family") or "").casefold()
    )
    event_type = re.sub(
        r"[^a-z0-9]+", " ", str(event.get("event_type") or "").casefold()
    )
    text = f"{family} {event_type}".strip()
    return any(pattern.search(text) for pattern in ISSUER_EVENT_PATTERNS)
