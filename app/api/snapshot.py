from __future__ import annotations

import time
from copy import deepcopy
from datetime import datetime, timezone
from threading import Event, Lock, Thread
from typing import Any, Callable


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
