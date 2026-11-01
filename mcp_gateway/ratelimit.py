"""Token-bucket rate limiting.

The in-memory backend is correct for a single gateway replica. For multiple replicas,
implement :class:`RateLimiter` over a shared store (e.g. Redis with a Lua token bucket);
the gateway only depends on the ``acquire`` interface.
"""

from __future__ import annotations

import time
from collections import OrderedDict
from dataclasses import dataclass
from typing import Protocol

from .config import RateLimitSpec


@dataclass
class _Bucket:
    tokens: float
    updated: float


class RateLimiter(Protocol):
    def acquire(self, key: str, spec: RateLimitSpec, cost: float = 1.0) -> float:
        """Consume ``cost`` tokens. Return 0 if allowed, else seconds until it would be."""
        ...


class InMemoryRateLimiter:
    def __init__(self, max_keys: int = 100_000, clock=time.monotonic) -> None:
        self._buckets: OrderedDict[str, _Bucket] = OrderedDict()
        self._max_keys = max_keys
        self._clock = clock

    def acquire(self, key: str, spec: RateLimitSpec, cost: float = 1.0) -> float:
        now = self._clock()
        capacity, rate = spec.capacity, spec.rate_per_s
        bucket = self._buckets.get(key)
        if bucket is None:
            bucket = _Bucket(tokens=capacity, updated=now)
            self._buckets[key] = bucket
            if len(self._buckets) > self._max_keys:
                self._buckets.popitem(last=False)  # evict least-recently-used key
        else:
            self._buckets.move_to_end(key)
            bucket.tokens = min(capacity, bucket.tokens + (now - bucket.updated) * rate)
            bucket.updated = now
        if bucket.tokens >= cost:
            bucket.tokens -= cost
            return 0.0
        return (cost - bucket.tokens) / rate
