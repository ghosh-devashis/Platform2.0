"""Gateway controls the open-source Portkey lacks (they are enterprise-only there), kept in the router:

- `GatewayKeys`   each agent/team gets its own gateway credential, never a provider key (GW-02)
- `RateLimiter`   requests per minute per client (GW-07)
- `TokenBudgets`  tokens per client per UTC day (GW-07)
- `ResponseCache` exact-match cache for non-sensitive prompts (GW-09)
- `UrlPool`       several gateway instances with failover and round-robin (GW-10)

All in memory and per router process: fine for local/CI use and for a small deployment; with Portkey enterprise these
become its native budgets, rate limits, caching and high availability and this module goes away.
"""

from __future__ import annotations

import hmac
import threading
import time
from collections import OrderedDict, defaultdict, deque
from collections.abc import Callable
from datetime import datetime, timezone
from typing import Any


def parse_pairs(spec: str, what: str) -> dict[str, str]:
    """'a=1,b=2' -> {'a': '1', 'b': '2'}."""
    pairs: dict[str, str] = {}
    for item in filter(None, (part.strip() for part in spec.split(","))):
        name, _, value = item.partition("=")
        if not name.strip() or not value.strip():
            raise ValueError(f"Invalid {what} entry '{item}'; expected name=value.")
        pairs[name.strip()] = value.strip()
    return pairs


class GatewayKeys:
    """Maps gateway credentials (`Authorization: Bearer <key>`) to client labels. No keys configured = open (dev)."""

    def __init__(self, keys: dict[str, str] | None) -> None:
        self._by_label = dict(keys or {})

    @property
    def enabled(self) -> bool:
        return bool(self._by_label)

    def authenticate(self, authorization: str | None) -> str | None:
        """The client label for a valid credential, else None. Constant-time comparison."""
        if not self.enabled:
            return "anonymous"
        token = (authorization or "").removeprefix("Bearer ").strip()
        match = None
        for label, key in self._by_label.items():
            if hmac.compare_digest(token.encode(), key.encode()):
                match = label
        return match


class RateLimiter:
    """Sliding one-minute window per client."""

    def __init__(self, requests_per_minute: int = 0, clock: Callable[[], float] = time.monotonic) -> None:
        self.limit = requests_per_minute
        self._clock = clock
        self._hits: dict[str, deque[float]] = defaultdict(deque)
        self._lock = threading.Lock()

    def check(self, client: str) -> int | None:
        """None if allowed (and counted); else the seconds to wait before retrying."""
        if self.limit <= 0:
            return None
        now = self._clock()
        with self._lock:
            hits = self._hits[client]
            while hits and now - hits[0] >= 60:
                hits.popleft()
            if len(hits) >= self.limit:
                return max(1, int(60 - (now - hits[0])) + 1)
            hits.append(now)
        return None


class TokenBudgets:
    """Tokens per client per UTC day, counted from the usage providers report on non-streaming answers."""

    def __init__(self, per_day: int = 0, today: Callable[[], str] | None = None) -> None:
        self.per_day = per_day
        self._today = today or (lambda: datetime.now(timezone.utc).strftime("%Y-%m-%d"))
        self._used: dict[tuple[str, str], int] = defaultdict(int)
        self._lock = threading.Lock()

    def exceeded(self, client: str) -> bool:
        return self.per_day > 0 and self._used[(client, self._today())] >= self.per_day

    def add(self, client: str, tokens: int) -> None:
        if self.per_day > 0 and tokens > 0:
            with self._lock:
                self._used[(client, self._today())] += tokens

    def used(self, client: str) -> int:
        return self._used[(client, self._today())]


class ResponseCache:
    """Small TTL cache of whole answers, keyed by the request fingerprint."""

    def __init__(self, max_entries: int = 1000, clock: Callable[[], float] = time.monotonic) -> None:
        self.max_entries = max_entries
        self._clock = clock
        self._items: OrderedDict[str, tuple[float, Any]] = OrderedDict()
        self._lock = threading.Lock()

    def get(self, key: str) -> Any | None:
        with self._lock:
            entry = self._items.get(key)
            if entry is None:
                return None
            if entry[0] <= self._clock():
                del self._items[key]
                return None
            self._items.move_to_end(key)
            return entry[1]

    def put(self, key: str, value: Any, ttl_seconds: float) -> None:
        with self._lock:
            self._items[key] = (self._clock() + ttl_seconds, value)
            self._items.move_to_end(key)
            while len(self._items) > self.max_entries:
                self._items.popitem(last=False)


class UrlPool:
    """Round-robin over gateway instances; an instance that fails is skipped for a while (GW-10)."""

    def __init__(self, urls: list[str], cooldown_seconds: float = 10.0, clock: Callable[[], float] = time.monotonic) -> None:
        if not urls:
            raise ValueError("At least one gateway URL is required.")
        self.urls = [u.rstrip("/") for u in urls]
        self.cooldown = cooldown_seconds
        self._clock = clock
        self._down_until: dict[str, float] = {}
        self._next = 0
        self._lock = threading.Lock()

    def order(self) -> list[str]:
        """Instances to try, starting from the next in rotation; healthy ones first."""
        with self._lock:
            start = self._next
            self._next = (self._next + 1) % len(self.urls)
            rotated = self.urls[start:] + self.urls[:start]
            now = self._clock()
            healthy = [u for u in rotated if self._down_until.get(u, 0) <= now]
            return healthy + [u for u in rotated if u not in healthy]  # a downed one is still a last resort

    def mark_down(self, url: str) -> None:
        with self._lock:
            self._down_until[url] = self._clock() + self.cooldown
