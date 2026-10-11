"""Event-driven snapshot scheduling (push-state).

State changes on mapped entities arrive via async_track_state_change_event.
Each change asks the scheduler for a snapshot; the scheduler coalesces bursts
and throttles so a chatty sensor cannot flood the HA bus / attention path:

- debounce: a snapshot runs SNAPSHOT_DEBOUNCE_SECONDS after the last change
  in a burst (trailing edge);
- max wait: a continuous stream of changes still emits at most
  SNAPSHOT_MAX_WAIT_SECONDS after the first pending change;
- throttle: two emits are never closer than SNAPSHOT_MIN_INTERVAL_SECONDS;
  changes inside that window are coalesced into one trailing emit.

No timer polls HA. Nothing here logs entity ids.
"""

from __future__ import annotations

import time
from typing import Any, Callable

from .const import (
    SNAPSHOT_DEBOUNCE_SECONDS,
    SNAPSHOT_MAX_WAIT_SECONDS,
    SNAPSHOT_MIN_INTERVAL_SECONDS,
)


class SnapshotScheduler:
    def __init__(
        self,
        loop: Any,
        emit: Callable[[], None],
        *,
        debounce: float = SNAPSHOT_DEBOUNCE_SECONDS,
        max_wait: float = SNAPSHOT_MAX_WAIT_SECONDS,
        min_interval: float = SNAPSHOT_MIN_INTERVAL_SECONDS,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._loop = loop
        self._emit = emit
        self.debounce = debounce
        self.max_wait = max_wait
        self.min_interval = min_interval
        self._clock = clock
        self._handle: Any = None
        self._first_pending: float | None = None
        self._last_emit: float | None = None
        self.emits = 0

    @property
    def pending(self) -> bool:
        return self._handle is not None

    def request(self) -> None:
        now = self._clock()
        if self._first_pending is None:
            self._first_pending = now
        due = now + self.debounce
        due = min(due, self._first_pending + self.max_wait)
        if self._last_emit is not None:
            due = max(due, self._last_emit + self.min_interval)
        self._cancel_handle()
        self._handle = self._loop.call_later(max(0.0, due - now), self._fire)

    def note_emitted(self) -> None:
        """An emit happened outside the scheduler (setup / options reload)."""
        self._last_emit = self._clock()

    def _fire(self) -> None:
        self._handle = None
        self._first_pending = None
        self._last_emit = self._clock()
        self.emits += 1
        self._emit()

    def _cancel_handle(self) -> None:
        if self._handle is not None:
            try:
                self._handle.cancel()
            except Exception:
                pass
            self._handle = None

    def cancel(self) -> None:
        self._cancel_handle()
        self._first_pending = None
