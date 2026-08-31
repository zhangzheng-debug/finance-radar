from __future__ import annotations

import base64
import hashlib
import hmac
import secrets


PUBLIC_PASSWORD_SCHEME = "pbkdf2_sha256"
PUBLIC_PASSWORD_ITERATIONS = 600_000
PUBLIC_PASSWORD_MIN_LENGTH = 12
_MIN_ACCEPTED_ITERATIONS = 200_000
_MAX_ACCEPTED_ITERATIONS = 2_000_000


def _encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode("ascii").rstrip("=")


def _decode(value: str) -> bytes:
    padding = "=" * (-len(value) % 4)
    return base64.urlsafe_b64decode(value + padding)


def hash_public_password(
    password: str,
    *,
    salt: bytes | None = None,
    iterations: int = PUBLIC_PASSWORD_ITERATIONS,
) -> str:
    """Return a versioned password verifier without retaining plaintext."""

    if len(password) < PUBLIC_PASSWORD_MIN_LENGTH:
        raise ValueError(
            f"public password must contain at least {PUBLIC_PASSWORD_MIN_LENGTH} characters"
        )
    if not _MIN_ACCEPTED_ITERATIONS <= int(iterations) <= _MAX_ACCEPTED_ITERATIONS:
        raise ValueError("public password iteration count is outside the accepted range")
    salt_bytes = salt if salt is not None else secrets.token_bytes(18)
    if not 16 <= len(salt_bytes) <= 64:
        raise ValueError("public password salt must contain between 16 and 64 bytes")
    digest = hashlib.pbkdf2_hmac(
        "sha256", password.encode("utf-8"), salt_bytes, int(iterations)
    )
    return "$".join(
        (
            PUBLIC_PASSWORD_SCHEME,
            str(int(iterations)),
            _encode(salt_bytes),
            _encode(digest),
        )
    )


def verify_public_password(password: str, encoded: str) -> bool:
    """Verify a candidate in constant time and fail closed on malformed input."""

    try:
        if len(encoded) > 512:
            return False
        scheme, iterations_text, salt_text, digest_text = encoded.split("$", 3)
        iterations = int(iterations_text)
        if scheme != PUBLIC_PASSWORD_SCHEME:
            return False
        if not _MIN_ACCEPTED_ITERATIONS <= iterations <= _MAX_ACCEPTED_ITERATIONS:
            return False
        salt = _decode(salt_text)
        expected = _decode(digest_text)
        if not 16 <= len(salt) <= 64 or len(expected) != hashlib.sha256().digest_size:
            return False
        actual = hashlib.pbkdf2_hmac(
            "sha256", password.encode("utf-8"), salt, iterations
        )
        return hmac.compare_digest(actual, expected)
    except (UnicodeError, ValueError, TypeError):
        return False


def public_credential_fingerprint(username: str, encoded: str) -> str:
    """Bind an authenticated Streamlit session to the active credential."""

    payload = f"{username}\0{encoded}".encode("utf-8")
    return hashlib.sha256(payload).hexdigest()
