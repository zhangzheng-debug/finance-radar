from __future__ import annotations

import json
import stat
import time
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
from threading import Event, Lock, Thread
from typing import Any, Callable

from app.api.overview_projection import OVERVIEW_SNAPSHOT_SCHEMA, payload_sha256


class SnapshotUnavailable(RuntimeError):
    """Raised when a precomputed snapshot has not completed successfully yet."""


class PrecomputedSnapshot:
    """Keep an expensive read projection off the request path.

    ``start`` performs one synchronous refresh before the background thread is
    launched.  This deliberately moves cold-query latency into application
    startup so the first public reader receives an already-computed value.
    Later refresh failures preserve the last good snapshot.
    """

    def __init__(
        self,
        factory: Callable[[], Any],
        *,
        refresh_interval_seconds: float,
        name: str,
    ) -> None:
        self._factory = factory
        self._refresh_interval_seconds = max(1.0, float(refresh_interval_seconds))
        self._name = name
        self._state_lock = Lock()
        self._refresh_lock = Lock()
        self._lifecycle_lock = Lock()
        self._stop_event = Event()
        self._thread: Thread | None = None
        self._value: Any = None
        self._computed_monotonic: float | None = None
        self._computed_at: str | None = None
        self._last_attempt_at: str | None = None
        self._last_error_code: str | None = None
        self._generation = 0

    @staticmethod
    def _utc_now() -> str:
        return datetime.now(timezone.utc).isoformat()

    def refresh(self) -> bool:
        """Compute one generation without allowing overlapping refreshes."""

        if not self._refresh_lock.acquire(blocking=False):
            return False
        attempted_at = self._utc_now()
        try:
            value = self._factory()
        except Exception as exc:  # Keep the last known-good projection available.
            with self._state_lock:
                self._last_attempt_at = attempted_at
                self._last_error_code = type(exc).__name__
            return False
        else:
            with self._state_lock:
                self._value = deepcopy(value)
                self._computed_monotonic = time.monotonic()
                self._computed_at = self._utc_now()
                self._last_attempt_at = attempted_at
                self._last_error_code = None
                self._generation += 1
            return True
        finally:
            self._refresh_lock.release()

    def read(self) -> tuple[Any, dict[str, Any]]:
        """Return a defensive copy and safe freshness metadata in O(1)."""

        with self._state_lock:
            if self._computed_monotonic is None:
                raise SnapshotUnavailable(f"{self._name} snapshot is unavailable")
            age_seconds = max(0.0, time.monotonic() - self._computed_monotonic)
            metadata = {
                "status": (
                    "STALE_AFTER_REFRESH_ERROR"
                    if self._last_error_code
                    else "READY"
                ),
                "computed_at": self._computed_at,
                "age_seconds": round(age_seconds, 3),
                "generation": self._generation,
                "refresh_interval_seconds": self._refresh_interval_seconds,
                "last_refresh_attempt_at": self._last_attempt_at,
                "last_refresh_error_code": self._last_error_code,
            }
            return deepcopy(self._value), metadata

    def _run(self) -> None:
        while not self._stop_event.wait(self._refresh_interval_seconds):
            self.refresh()

    def start(self) -> None:
        """Build the first snapshot before declaring the lifecycle started."""

        with self._lifecycle_lock:
            if self._thread is not None and self._thread.is_alive():
                return
            self._stop_event.clear()
            self.refresh()
            self._thread = Thread(
                target=self._run,
                name=f"finance-radar-{self._name}-snapshot",
                daemon=True,
            )
            self._thread.start()

    def stop(self) -> None:
        with self._lifecycle_lock:
            thread = self._thread
            self._stop_event.set()
            self._thread = None
        if thread is not None:
            thread.join(timeout=5.0)


class PublishedSnapshot:
    """Read an atomically published projection without computing it in-process.

    The producer is a separate systemd oneshot process.  Requests only check a
    file signature and reload a complete JSON generation when that signature
    changes.  A malformed or failed new generation cannot replace the last
    known-good value.
    """

    _MAX_BYTES = 32 * 1024 * 1024

    def __init__(
        self,
        path: Path,
        *,
        refresh_interval_seconds: float,
        name: str,
    ) -> None:
        self._path = path.resolve()
        self._refresh_interval_seconds = max(1.0, float(refresh_interval_seconds))
        self._name = name
        self._state_lock = Lock()
        self._refresh_lock = Lock()
        self._signature: tuple[int, int, int] | None = None
        self._value: Any = None
        self._computed_at: str | None = None
        self._computed_epoch: float | None = None
        self._build_started_at: str | None = None
        self._build_duration_seconds: float | None = None
        self._payload_sha256: str | None = None
        self._last_attempt_at: str | None = None
        self._last_error_code: str | None = None
        self._generation = 0

    @staticmethod
    def _utc_now() -> str:
        return datetime.now(timezone.utc).isoformat()

    def _read_generation(self) -> tuple[dict[str, Any], tuple[int, int, int]]:
        file_stat = self._path.lstat()
        if stat.S_ISLNK(file_stat.st_mode) or not stat.S_ISREG(file_stat.st_mode):
            raise ValueError("published snapshot must be a regular non-symlink file")
        if file_stat.st_size <= 2 or file_stat.st_size > self._MAX_BYTES:
            raise ValueError("published snapshot size is outside the accepted envelope")
        signature = (file_stat.st_ino, file_stat.st_mtime_ns, file_stat.st_size)
        raw = self._path.read_bytes()
        if len(raw) != file_stat.st_size:
            raise ValueError("published snapshot changed while being read")
        envelope = json.loads(raw)
        if not isinstance(envelope, dict) or envelope.get("schema") != OVERVIEW_SNAPSHOT_SCHEMA:
            raise ValueError("published snapshot schema is invalid")
        payload = envelope.get("payload")
        if not isinstance(payload, dict):
            raise ValueError("published snapshot payload is invalid")
        expected_hash = str(envelope.get("payload_sha256") or "")
        if len(expected_hash) != 64 or payload_sha256(payload) != expected_hash:
            raise ValueError("published snapshot payload hash is invalid")
        computed_at = str(envelope.get("computed_at") or "")
        computed = datetime.fromisoformat(computed_at.replace("Z", "+00:00"))
        if computed.tzinfo is None:
            raise ValueError("published snapshot timestamp lacks a timezone")
        envelope["_computed_epoch"] = computed.timestamp()
        envelope["_signature"] = signature
        return envelope, signature

    def refresh(self) -> bool:
        if not self._refresh_lock.acquire(blocking=False):
            return False
        attempted_at = self._utc_now()
        try:
            envelope, signature = self._read_generation()
        except Exception as exc:
            with self._state_lock:
                self._last_attempt_at = attempted_at
                self._last_error_code = type(exc).__name__
            return False
        else:
            with self._state_lock:
                if signature != self._signature:
                    self._value = envelope["payload"]
                    self._computed_at = envelope["computed_at"]
                    self._computed_epoch = float(envelope["_computed_epoch"])
                    self._build_started_at = envelope.get("build_started_at")
                    self._build_duration_seconds = float(
                        envelope.get("build_duration_seconds") or 0.0
                    )
                    self._payload_sha256 = envelope["payload_sha256"]
                    self._signature = signature
                    self._generation += 1
                self._last_attempt_at = attempted_at
                self._last_error_code = None
            return True
        finally:
            self._refresh_lock.release()

    def read(self) -> tuple[Any, dict[str, Any]]:
        try:
            file_stat = self._path.lstat()
            signature = (file_stat.st_ino, file_stat.st_mtime_ns, file_stat.st_size)
        except OSError:
            signature = None
        with self._state_lock:
            loaded_signature = self._signature
        if signature != loaded_signature:
            self.refresh()

        with self._state_lock:
            if self._computed_epoch is None:
                raise SnapshotUnavailable(f"{self._name} snapshot is unavailable")
            age_seconds = max(0.0, time.time() - self._computed_epoch)
            metadata = {
                "status": (
                    "STALE_AFTER_REFRESH_ERROR"
                    if self._last_error_code
                    else "READY"
                ),
                "computed_at": self._computed_at,
                "age_seconds": round(age_seconds, 3),
                "generation": self._generation,
                "refresh_interval_seconds": self._refresh_interval_seconds,
                "last_refresh_attempt_at": self._last_attempt_at,
                "last_refresh_error_code": self._last_error_code,
                "build_started_at": self._build_started_at,
                "build_duration_seconds": self._build_duration_seconds,
                "payload_sha256": self._payload_sha256,
                "producer": "external_atomic_file",
            }
            return deepcopy(self._value), metadata

    def start(self) -> None:
        self.refresh()

    def stop(self) -> None:
        return None
