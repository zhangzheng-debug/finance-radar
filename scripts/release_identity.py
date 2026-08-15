"""Shared, path-safe Finance Radar release identity contract.

The in-place release auditor, migration backup audit, restore preparer and
activation tooling all place a release ID below ``/opt/finance-radar/releases``.
Keep the permitted alphabet deliberately small so an ID can never introduce a
path separator, shell quote, wildcard, or control character.  The normal
``release_audit.py`` default (``YYYYMMDDTHHMMSSZ-<git-commit>``) is a member of
this contract, while older timestamp-only IDs remain restorable.
"""

from __future__ import annotations

import re


# 1--96 ASCII characters, beginning with an alphanumeric character.  This
# exactly matches the established in-place installer and release auditor
# contract, and excludes slash, backslash, whitespace, quotes, ``$`` and glob
# metacharacters.
RELEASE_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,95}$")


def validate_release_id(value: str) -> str:
    """Return a release ID only when it is safe as one path component."""

    if not isinstance(value, str) or not RELEASE_ID_PATTERN.fullmatch(value):
        raise ValueError(
            "release id must use 1-96 ASCII letters, digits, dot, dash or underscore"
        )
    return value
