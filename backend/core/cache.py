"""Two-layer cache for autocomplete query results.

Layer 1 — LRU (in-process, ~0ms):
    Per-worker OrderedDict with TTL. Serves repeated queries within the same
    Uvicorn worker process instantly without any network I/O.

Layer 2 — Redis (shared across all workers, ~1ms):
    All 4 Uvicorn worker processes share one Redis instance. When Worker 1
    warms "diabetes", Workers 2-4 get a cache hit on their next "diabetes"
    request instead of hitting Solr again.

Lookup order for every /search request:
    1. LRU hit  → return immediately (~0ms)
    2. Redis hit → populate LRU, return (~1ms)
    3. Solr     → populate Redis + LRU, return (~5ms)

Environment variables:
    CACHE_LRU_MAX_SIZE   int, default 10000  (0 = disable LRU)
    CACHE_LRU_TTL_SEC    int, default 3600
    REDIS_URL            str, default redis://localhost:6379/0
    CACHE_REDIS_TTL_SEC  int, default 3600   (0 = disable Redis)
"""

from __future__ import annotations

import json
import logging
import os
import time
from collections import OrderedDict
from threading import Lock
from typing import Optional

log = logging.getLogger(__name__)


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, default))
    except (TypeError, ValueError):
        return default


LRU_MAX_SIZE: int = _env_int("CACHE_LRU_MAX_SIZE", 10_000)
LRU_TTL_SEC: int = _env_int("CACHE_LRU_TTL_SEC", 3600)
REDIS_URL: str = os.getenv("REDIS_URL", "redis://localhost:6379/0")
REDIS_TTL_SEC: int = _env_int("CACHE_REDIS_TTL_SEC", 3600)
REDIS_KEY_PREFIX = "autocomplete"


# ─── Layer 1: in-process LRU ─────────────────────────────────────────────────

class LRUCache:
    """Thread-safe LRU cache backed by OrderedDict + per-entry TTL."""

    def __init__(self, max_size: int, ttl_sec: int) -> None:
        self._max_size = max_size
        self._ttl_sec = ttl_sec
        self._store: OrderedDict[str, tuple] = OrderedDict()
        self._lock = Lock()
        self._hits = 0
        self._misses = 0

    @property
    def enabled(self) -> bool:
        return self._max_size > 0

    def _make_key(self, query: str, rows: int) -> str:
        return f"{query}::{rows}"

    def get(self, query: str, rows: int) -> Optional[dict]:
        if not self.enabled:
            return None
        key = self._make_key(query, rows)
        with self._lock:
            if key not in self._store:
                self._misses += 1
                return None
            value, expiry = self._store[key]
            if time.monotonic() > expiry:
                del self._store[key]
                self._misses += 1
                return None
            self._store.move_to_end(key)
            self._hits += 1
            return value

    def set(self, query: str, rows: int, value: dict) -> None:
        if not self.enabled:
            return
        key = self._make_key(query, rows)
        expiry = time.monotonic() + self._ttl_sec
        with self._lock:
            if key in self._store:
                self._store.move_to_end(key)
            self._store[key] = (value, expiry)
            if len(self._store) > self._max_size:
                self._store.popitem(last=False)

    def stats(self) -> dict:
        with self._lock:
            total = self._hits + self._misses
            return {
                "size": len(self._store),
                "max_size": self._max_size,
                "ttl_sec": self._ttl_sec,
                "hits": self._hits,
                "misses": self._misses,
                "hit_rate": round(self._hits / total, 4) if total else 0.0,
                "enabled": self.enabled,
            }

    def clear(self) -> None:
        with self._lock:
            self._store.clear()
            self._hits = 0
            self._misses = 0


# ─── Layer 2: Redis ───────────────────────────────────────────────────────────

class RedisCache:
    """Thin wrapper around redis-py with JSON serialization and graceful fallback.

    If Redis is unreachable at startup or during any operation the error is
    logged and the call returns None/False — the LRU + Solr path continues
    working normally. Redis is never a hard dependency.
    """

    def __init__(self, url: str, ttl_sec: int) -> None:
        self._url = url
        self._ttl_sec = ttl_sec
        self._client = None
        self._hits = 0
        self._misses = 0
        self._errors = 0
        if ttl_sec > 0:
            self._connect()

    @property
    def enabled(self) -> bool:
        return self._ttl_sec > 0 and self._client is not None

    def _connect(self) -> None:
        try:
            import redis
            self._client = redis.Redis.from_url(
                self._url,
                socket_connect_timeout=2,
                socket_timeout=1,
                decode_responses=True,
            )
            self._client.ping()
            log.info("Redis cache connected: %s", self._url)
        except Exception as exc:
            log.warning("Redis unavailable, cache disabled: %s", exc)
            self._client = None

    def _make_key(self, query: str, rows: int) -> str:
        return f"{REDIS_KEY_PREFIX}::{query}::{rows}"

    def get(self, query: str, rows: int) -> Optional[dict]:
        if not self.enabled:
            return None
        try:
            raw = self._client.get(self._make_key(query, rows))
            if raw is None:
                self._misses += 1
                return None
            self._hits += 1
            return json.loads(raw)
        except Exception as exc:
            self._errors += 1
            log.debug("Redis get error: %s", exc)
            return None

    def set(self, query: str, rows: int, value: dict) -> None:
        if not self.enabled:
            return
        try:
            self._client.setex(
                self._make_key(query, rows),
                self._ttl_sec,
                json.dumps(value, ensure_ascii=False),
            )
        except Exception as exc:
            self._errors += 1
            log.debug("Redis set error: %s", exc)

    def stats(self) -> dict:
        total = self._hits + self._misses
        return {
            "enabled": self.enabled,
            "url": self._url,
            "ttl_sec": self._ttl_sec,
            "hits": self._hits,
            "misses": self._misses,
            "errors": self._errors,
            "hit_rate": round(self._hits / total, 4) if total else 0.0,
        }

    def clear(self) -> None:
        if not self.enabled:
            return
        try:
            pattern = f"{REDIS_KEY_PREFIX}::*"
            keys = self._client.keys(pattern)
            if keys:
                self._client.delete(*keys)
            self._hits = 0
            self._misses = 0
            self._errors = 0
        except Exception as exc:
            log.debug("Redis clear error: %s", exc)


# ─── Two-layer facade ─────────────────────────────────────────────────────────

class TwoLayerCache:
    """LRU (Layer 1) + Redis (Layer 2) unified interface.

    get() checks L1 first, then L2, then returns None (caller hits Solr).
    set() populates both layers so the local LRU is warm immediately.
    """

    def __init__(self) -> None:
        self.lru = LRUCache(max_size=LRU_MAX_SIZE, ttl_sec=LRU_TTL_SEC)
        self.redis = RedisCache(url=REDIS_URL, ttl_sec=REDIS_TTL_SEC)

    def get(self, query: str, rows: int) -> Optional[dict]:
        # L1
        value = self.lru.get(query, rows)
        if value is not None:
            return value

        # L2
        value = self.redis.get(query, rows)
        if value is not None:
            # Backfill L1 so subsequent requests on this worker skip Redis too
            self.lru.set(query, rows, value)
            return value

        return None

    def set(self, query: str, rows: int, value: dict) -> None:
        self.lru.set(query, rows, value)
        self.redis.set(query, rows, value)

    def clear(self) -> None:
        self.lru.clear()
        self.redis.clear()

    def stats(self) -> dict:
        return {
            "lru": self.lru.stats(),
            "redis": self.redis.stats(),
        }


# Single shared instance used across the entire process
lru_cache = TwoLayerCache()
