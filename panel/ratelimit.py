"""In-memory sliding-window rate limiter (one counter per key)."""

import threading
import time
from collections import defaultdict
from collections.abc import Callable

from fastapi import Request


def rate_limit_key(request: Request) -> str:
    """Per-client rate-limit key for a request.

    Rightmost ``X-Forwarded-For`` entry = the real client IP appended by the
    trusted edge (Railway); the leftmost entries are client-spoofable.
    Falls back to the direct peer address when no header is present.
    """
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded:
        return forwarded.split(",")[-1].strip()
    return request.client.host if request.client else "unknown"


class RateLimiter:
    """Allow at most ``max_calls`` per key inside a sliding ``window_s`` window."""

    def __init__(
        self,
        window_s: int = 60,
        max_calls: int = 5,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.window_s = window_s
        self.max_calls = max_calls
        self.clock = clock
        self._hits: dict[str, list[float]] = defaultdict(list)
        self._lock = threading.Lock()

    def allow(self, key: str) -> bool:
        """Record a hit and return True, or return False over the limit."""
        now = self.clock()
        cutoff = now - self.window_s
        with self._lock:
            hits = [stamp for stamp in self._hits[key] if stamp > cutoff]
            if len(hits) >= self.max_calls:
                self._hits[key] = hits
                return False
            hits.append(now)
            self._hits[key] = hits
            return True
