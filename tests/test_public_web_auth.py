from __future__ import annotations

import pytest

from app.web.public_auth import (
    public_credential_fingerprint,
    hash_public_password,
    verify_public_password,
)


def test_public_password_hash_round_trip_and_rotation() -> None:
    encoded = hash_public_password(
        "correct-horse-battery-staple", salt=b"0123456789abcdef"
    )
    assert encoded.startswith("pbkdf2_sha256$600000$")
    assert "correct-horse" not in encoded
    assert verify_public_password("correct-horse-battery-staple", encoded) is True
    assert verify_public_password("wrong-password-value", encoded) is False
    assert public_credential_fingerprint("admin", encoded) != public_credential_fingerprint(
        "reader", encoded
    )


@pytest.mark.parametrize(
    "encoded",
    (
        "",
        "sha256$600000$c2FsdA$ZGlnaWVzdA",
        "pbkdf2_sha256$1$c2FsdA$ZGlnaWVzdA",
        "pbkdf2_sha256$600000$not-base64!$not-base64!",
    ),
)
def test_public_password_verifier_fails_closed(encoded: str) -> None:
    assert verify_public_password("any-password-value", encoded) is False


def test_public_password_hash_rejects_short_or_weak_storage_contracts() -> None:
    with pytest.raises(ValueError, match="at least 12"):
        hash_public_password("Admin123456")
    with pytest.raises(ValueError, match="iteration"):
        hash_public_password(
            "correct-horse-battery-staple", salt=b"0123456789abcdef", iterations=1
        )
