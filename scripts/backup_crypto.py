#!/usr/bin/env python3
"""Streaming authenticated encryption for Finance Radar migration archives."""

from __future__ import annotations

import argparse
import base64
import os
import secrets
import shutil
import tempfile
from pathlib import Path

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from cryptography.hazmat.primitives.kdf.scrypt import Scrypt


MAGIC = b"FRBKP01\0"
SALT_BYTES = 16
NONCE_BYTES = 12
TAG_BYTES = 16
CHUNK_BYTES = 1024 * 1024


def _derive_key(passphrase: str, salt: bytes) -> bytes:
    if len(passphrase) < 16:
        raise ValueError("backup passphrase must contain at least 16 characters")
    return Scrypt(salt=salt, length=32, n=2**15, r=8, p=1).derive(passphrase.encode("utf-8"))


def _paths(source: str | Path, destination: str | Path) -> tuple[Path, Path, Path]:
    source_path = Path(source).resolve()
    destination_path = Path(destination).resolve()
    if source_path == destination_path:
        raise ValueError("source and destination must differ")
    if not source_path.is_file():
        raise FileNotFoundError(source_path)
    destination_path.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination_path.with_name(destination_path.name + ".partial")
    return source_path, destination_path, temporary


def encrypt_file(source: str | Path, destination: str | Path, passphrase: str) -> dict[str, int | str]:
    source_path, destination_path, temporary = _paths(source, destination)
    salt = secrets.token_bytes(SALT_BYTES)
    nonce = secrets.token_bytes(NONCE_BYTES)
    header = MAGIC + salt + nonce
    encryptor = Cipher(algorithms.AES(_derive_key(passphrase, salt)), modes.GCM(nonce)).encryptor()
    encryptor.authenticate_additional_data(header)
    try:
        with source_path.open("rb") as source_handle, temporary.open("wb") as destination_handle:
            destination_handle.write(header)
            while chunk := source_handle.read(CHUNK_BYTES):
                destination_handle.write(encryptor.update(chunk))
            destination_handle.write(encryptor.finalize())
            destination_handle.write(encryptor.tag)
        os.replace(temporary, destination_path)
    except Exception:
        temporary.unlink(missing_ok=True)
        raise
    return {
        "mode": "AES-256-GCM+scrypt",
        "source_bytes": source_path.stat().st_size,
        "encrypted_bytes": destination_path.stat().st_size,
        "destination": str(destination_path),
    }


def decrypt_file(source: str | Path, destination: str | Path, passphrase: str) -> dict[str, int | str]:
    source_path, destination_path, _ = _paths(source, destination)
    temporary = destination_path.with_name(
        f"{destination_path.name}.partial-{secrets.token_hex(8)}"
    )
    minimum = len(MAGIC) + SALT_BYTES + NONCE_BYTES + TAG_BYTES
    if source_path.stat().st_size < minimum:
        raise ValueError("encrypted backup is truncated")
    with source_path.open("rb") as source_handle:
        magic = source_handle.read(len(MAGIC))
        if magic != MAGIC:
            raise ValueError("not a Finance Radar encrypted backup")
        salt = source_handle.read(SALT_BYTES)
        nonce = source_handle.read(NONCE_BYTES)
        header = magic + salt + nonce
        ciphertext_bytes = source_path.stat().st_size - len(header) - TAG_BYTES
        source_handle.seek(-TAG_BYTES, os.SEEK_END)
        tag = source_handle.read(TAG_BYTES)
        source_handle.seek(len(header))
        decryptor = Cipher(algorithms.AES(_derive_key(passphrase, salt)), modes.GCM(nonce, tag)).decryptor()
        decryptor.authenticate_additional_data(header)
        remaining = ciphertext_bytes
        try:
            # GCM authenticates only at finalize().  Do not stream those
            # unauthenticated bytes into a named destination-side .partial
            # file.  TemporaryFile is anonymous on supported POSIX systems
            # (and delete-on-close elsewhere); only after authentication
            # succeeds is a named 0600 staging file created for atomic replace.
            with tempfile.TemporaryFile(
                mode="w+b",
                dir=destination_path.parent,
            ) as authenticated_plaintext:
                while remaining:
                    chunk = source_handle.read(min(CHUNK_BYTES, remaining))
                    if not chunk:
                        raise ValueError("encrypted backup is truncated")
                    remaining -= len(chunk)
                    authenticated_plaintext.write(decryptor.update(chunk))
                authenticated_plaintext.write(decryptor.finalize())
                authenticated_plaintext.seek(0)
                descriptor = os.open(
                    temporary,
                    os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                    0o600,
                )
                with os.fdopen(descriptor, "wb") as destination_handle:
                    shutil.copyfileobj(
                        authenticated_plaintext,
                        destination_handle,
                        length=CHUNK_BYTES,
                    )
                    destination_handle.flush()
                    os.fsync(destination_handle.fileno())
            os.replace(temporary, destination_path)
        except Exception:
            temporary.unlink(missing_ok=True)
            raise
    return {
        "mode": "AES-256-GCM+scrypt",
        "encrypted_bytes": source_path.stat().st_size,
        "restored_bytes": destination_path.stat().st_size,
        "destination": str(destination_path),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="operation", required=True)
    for operation in ("encrypt", "decrypt"):
        command = subparsers.add_parser(operation)
        command.add_argument("source", type=Path)
        command.add_argument("destination", type=Path)
        command.add_argument("--passphrase-file", type=Path)
    keygen = subparsers.add_parser("keygen")
    keygen.add_argument("destination", type=Path)
    args = parser.parse_args()
    if args.operation == "keygen":
        args.destination.parent.mkdir(parents=True, exist_ok=True)
        if args.destination.exists():
            parser.error(f"refusing to replace existing key: {args.destination}")
        passphrase = base64.urlsafe_b64encode(secrets.token_bytes(48)).decode("ascii")
        descriptor = os.open(args.destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(passphrase + "\n")
        print(f"key_file={args.destination.resolve()}")
        return 0
    passphrase = os.getenv("FINANCE_RADAR_BACKUP_PASSPHRASE")
    if not passphrase and args.passphrase_file:
        passphrase = args.passphrase_file.read_text(encoding="utf-8").strip()
    if not passphrase:
        parser.error("FINANCE_RADAR_BACKUP_PASSPHRASE or --passphrase-file is required")
    result = (
        encrypt_file(args.source, args.destination, passphrase)
        if args.operation == "encrypt"
        else decrypt_file(args.source, args.destination, passphrase)
    )
    for key, value in result.items():
        print(f"{key}={value}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
