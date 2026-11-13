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


class KeyedLimiter:
    """A RateLimiter per key (a client address), so one noisy client cannot lock out the others.

    At most `keys` clients are remembered; the least recently seen is dropped first.
    """

    def __init__(self, limit: int, window: float, keys: int = 2048, clock: Callable[[], float] = time.monotonic):
        self.limit, self.window, self.keys, self._clock = limit, window, keys, clock
        self._by_key: dict[str, RateLimiter] = {}
        self._lock = threading.Lock()

    def _of(self, key: str) -> RateLimiter:
        with self._lock:
            limiter = self._by_key.pop(key, None) or RateLimiter(self.limit, self.window, self._clock)
            self._by_key[key] = limiter  # most recently used last
            while len(self._by_key) > self.keys:
                self._by_key.pop(next(iter(self._by_key)))
            return limiter

    def full(self, key: str) -> bool:
        return self._of(key).full()

    def record(self, key: str) -> None:
        self._of(key).record()
