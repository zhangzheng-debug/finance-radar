from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any


ALLOWED_MIME_EXTENSIONS = {
    "text/html": ".html",
    "text/plain": ".txt",
    "application/pdf": ".pdf",
    "application/json": ".json",
}


class EvidenceObjectStore:
    """Immutable content-addressed storage for exact evidence objects."""

    def __init__(self, root: str | Path):
        self.root = Path(root)

    def put_bytes(self, content: bytes, *, mime_type: str) -> dict[str, Any]:
        if mime_type not in ALLOWED_MIME_EXTENSIONS:
            raise ValueError(f"unsupported evidence MIME type: {mime_type}")
        digest = hashlib.sha256(content).hexdigest()
        extension = ALLOWED_MIME_EXTENSIONS[mime_type]
        relative_path = Path(digest[:2]) / f"{digest}{extension}"
        destination = self.root / relative_path
        destination.parent.mkdir(parents=True, exist_ok=True)
        if destination.exists():
            existing_digest = hashlib.sha256(destination.read_bytes()).hexdigest()
            if existing_digest != digest:
                raise RuntimeError(f"content-address collision at {destination}")
        else:
            temporary = destination.with_suffix(destination.suffix + ".tmp")
            temporary.write_bytes(content)
            temporary.replace(destination)
        return {
            "sha256": digest,
            "relative_path": relative_path.as_posix(),
            "mime_type": mime_type,
            "byte_length": len(content),
        }

    def put_text(self, text: str) -> dict[str, Any]:
        return self.put_bytes(text.encode("utf-8"), mime_type="text/plain")

    def verify(self, relative_path: str, expected_sha256: str) -> bool:
        path = self.root / relative_path
        return path.is_file() and hashlib.sha256(path.read_bytes()).hexdigest() == expected_sha256
