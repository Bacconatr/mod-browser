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
    """A RateLimiter per key (a client address), so one noisy client cannot lock out the others,
    plus a ceiling over all keys together, so many addresses cannot add up to an unbounded rate.

    At most `keys` clients are remembered. A client still locked out is never forgotten to make
    room: the least recently seen client that is NOT locked goes first, and when every remembered
    client is locked, a new one is turned away rather than let in on a fresh window.
    """

    def __init__(self, limit: int, window: float, ceiling: int, keys: int = 2048,
                 clock: Callable[[], float] = time.monotonic):
        self.limit, self.window, self.keys, self._clock = limit, window, keys, clock
        self._overall = RateLimiter(ceiling, window, clock)
        self._by_key: dict[str, RateLimiter] = {}
        self._lock = threading.Lock()

    def _of(self, key: str) -> RateLimiter | None:
        """The key's limiter, or None when the table is full of locked-out clients."""
        with self._lock:
            limiter = self._by_key.pop(key, None)
            if limiter is None:
                while len(self._by_key) >= self.keys:
                    idle = next((k for k, v in self._by_key.items() if not v.full()), None)
                    if idle is None:
                        return None
                    del self._by_key[idle]
                limiter = RateLimiter(self.limit, self.window, self._clock)
            self._by_key[key] = limiter  # most recently used last
            return limiter

    def full(self, key: str) -> bool:
        limiter = self._of(key)
        return limiter is None or limiter.full() or self._overall.full()

    def record(self, key: str) -> None:
        limiter = self._of(key)
        if limiter is not None:
            limiter.record()
        self._overall.record()
