from __future__ import annotations

from pathlib import Path

import pytest

import scripts.backup_crypto as backup_crypto
from scripts.backup_crypto import MAGIC, decrypt_file, encrypt_file


def test_authenticated_backup_round_trip(tmp_path: Path) -> None:
    source = tmp_path / "migration.tgz"
    encrypted = tmp_path / "migration.tgz.aesgcm"
    restored = tmp_path / "restored.tgz"
    source.write_bytes((b"finance-radar-backup\0" * 100_000) + b"tail")
    result = encrypt_file(source, encrypted, "correct horse battery staple")
    assert result["mode"] == "AES-256-GCM+scrypt"
    assert encrypted.read_bytes()[:8] == MAGIC
    decrypt_file(encrypted, restored, "correct horse battery staple")
    assert restored.read_bytes() == source.read_bytes()


def test_authenticated_backup_rejects_wrong_passphrase(tmp_path: Path) -> None:
    source = tmp_path / "migration.tgz"
    encrypted = tmp_path / "migration.tgz.aesgcm"
    restored = tmp_path / "restored.tgz"
    restored.write_bytes(b"keep-the-existing-restore")
    source.write_bytes(b"sensitive migration archive")
    encrypt_file(source, encrypted, "correct horse battery staple")
    with pytest.raises(Exception):
        decrypt_file(encrypted, restored, "this passphrase is definitely wrong")
    assert restored.read_bytes() == b"keep-the-existing-restore"
    assert not list(tmp_path.glob("restored.tgz.partial*"))


def test_decrypt_does_not_publish_unauthenticated_plaintext_before_finalize(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "migration.tgz"
    encrypted = tmp_path / "migration.tgz.aesgcm"
    restored = tmp_path / "restored.tgz"
    source.write_bytes(b"sensitive migration archive")
    encrypt_file(source, encrypted, "correct horse battery staple")

    class RejectingDecryptor:
        def authenticate_additional_data(self, _header: bytes) -> None:
            return None

        def update(self, chunk: bytes) -> bytes:
            return b"unauthenticated:" + chunk[:8]

        def finalize(self) -> bytes:
            assert not restored.exists()
            assert not list(tmp_path.glob("restored.tgz.partial*"))
            raise ValueError("authentication failed")

    class RejectingCipher:
        def decryptor(self) -> RejectingDecryptor:
            return RejectingDecryptor()

    monkeypatch.setattr(backup_crypto, "Cipher", lambda *_args, **_kwargs: RejectingCipher())
    with pytest.raises(ValueError, match="authentication failed"):
        decrypt_file(encrypted, restored, "correct horse battery staple")
    assert not restored.exists()
    assert not list(tmp_path.glob("restored.tgz.partial*"))
