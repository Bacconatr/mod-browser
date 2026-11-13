"""Small sliding-window counters: public writes and admin password guesses are bounded."""

from __future__ import annotations

import threading
import time
from collections import deque
from typing import Callable


class RateLimiter:
    """At most `limit` events per `window` seconds, counted across all callers."""

    def __init__(self, limit: int, window: float, clock: Callable[[], float] = time.monotonic):
        self.limit, self.window, self._clock = limit, window, clock
        self._events: deque[float] = deque()
        self._lock = threading.Lock()

    def _trim(self, now: float) -> None:
        while self._events and now - self._events[0] >= self.window:
            self._events.popleft()

    def allow(self) -> bool:
        """Count one event if there is room; False when the window is full."""
        with self._lock:
            now = self._clock()
            self._trim(now)
            if len(self._events) >= self.limit:
                return False
            self._events.append(now)
            return True

    def full(self) -> bool:
        """True when no more events fit, without counting one."""
        with self._lock:
            self._trim(self._clock())
            return len(self._events) >= self.limit

    def record(self) -> None:
        """Count one event unconditionally (a failed password guess)."""
        with self._lock:
            now = self._clock()
            self._trim(now)
            self._events.append(now)
