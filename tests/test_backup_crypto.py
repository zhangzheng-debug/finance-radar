from __future__ import annotations

from pathlib import Path

import pytest

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
    source.write_bytes(b"sensitive migration archive")
    encrypt_file(source, encrypted, "correct horse battery staple")
    with pytest.raises(Exception):
        decrypt_file(encrypted, restored, "this passphrase is definitely wrong")
    assert not restored.exists()
