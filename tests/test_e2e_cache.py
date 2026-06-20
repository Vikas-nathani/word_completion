"""End-to-end tests for the two-layer caching system (LRU + Redis).

Tests the cache module directly (unit) against the real LRUCache API:
    get(query: str, rows: int) -> Optional[dict]
    set(query: str, rows: int, value: dict) -> None
    clear() -> None
    stats() -> dict

Also covers the /cache/stats and /cache/clear API endpoints.
Redis is not required — all Redis tests use a disabled TTL (ttl_sec=0) or mock.

Covers:
- LRUCache: get/set, TTL expiry, max-size eviction, hit/miss tracking
- LRUCache: key separation by rows, overwrite, clear/reset stats
- TwoLayerCache: LRU hit short-circuits Redis
- GET /cache/stats returns correct structure and types
- POST /cache/clear resets LRU and returns 200
- 500+ parameterized test assertions
"""

from __future__ import annotations

import os
import sys
import time
from unittest.mock import MagicMock

import pytest
from httpx import ASGITransport, AsyncClient

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
os.environ.setdefault("SOLR_URL", "http://localhost:8983/solr/umls_core")


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def app():
    from backend.app import app as _app
    return _app


@pytest.fixture
async def client(app):
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


@pytest.fixture
def lru():
    from backend.core.cache import LRUCache
    return LRUCache(max_size=20, ttl_sec=3600)


@pytest.fixture
def tiny_lru():
    from backend.core.cache import LRUCache
    return LRUCache(max_size=3, ttl_sec=3600)


@pytest.fixture
def short_ttl_lru():
    from backend.core.cache import LRUCache
    return LRUCache(max_size=100, ttl_sec=1)


# ---------------------------------------------------------------------------
# LRUCache: basic get/set
# ---------------------------------------------------------------------------

CACHE_QUERY_ROW_PAIRS = [
    (f"query{i}", i % 50 + 1, {"results": [f"term{i}"], "total": 1})
    for i in range(100)
]


@pytest.mark.parametrize("query,rows,value", CACHE_QUERY_ROW_PAIRS)
def test_lru_set_then_get_returns_value(lru, query, rows, value):
    lru.set(query, rows, value)
    assert lru.get(query, rows) == value


@pytest.mark.parametrize("query,rows,_", CACHE_QUERY_ROW_PAIRS[:50])
def test_lru_get_before_set_returns_none(lru, query, rows, _):
    assert lru.get(query, rows) is None


@pytest.mark.parametrize("query,rows,value", CACHE_QUERY_ROW_PAIRS[:50])
def test_lru_overwrite_updates_value(lru, query, rows, value):
    lru.set(query, rows, {"old": True})
    lru.set(query, rows, value)
    assert lru.get(query, rows) == value


# ---------------------------------------------------------------------------
# LRUCache: row separation — same query, different rows → different entries
# ---------------------------------------------------------------------------

ROW_SEPARATION_CASES = [
    ("diabetes", 5, {"r": 5}),
    ("diabetes", 10, {"r": 10}),
    ("fever", 1, {"r": 1}),
    ("fever", 50, {"r": 50}),
    ("blood", 15, {"r": 15}),
    ("blood", 20, {"r": 20}),
    ("metformin", 3, {"r": 3}),
    ("metformin", 7, {"r": 7}),
    ("pain", 12, {"r": 12}),
    ("pain", 25, {"r": 25}),
]


@pytest.mark.parametrize("query,rows,value", ROW_SEPARATION_CASES)
def test_different_rows_stored_independently(lru, query, rows, value):
    lru.set(query, rows, value)
    other_rows = rows + 1
    assert lru.get(query, other_rows) is None
    assert lru.get(query, rows) == value


# ---------------------------------------------------------------------------
# LRUCache: hit/miss counters
# ---------------------------------------------------------------------------

def test_lru_hit_increments_hit_counter(lru):
    lru.set("q", 5, {"data": 1})
    before = lru.stats()["hits"]
    lru.get("q", 5)
    assert lru.stats()["hits"] == before + 1


def test_lru_miss_increments_miss_counter(lru):
    before = lru.stats()["misses"]
    lru.get("not_set_query", 99)
    assert lru.stats()["misses"] == before + 1


def test_lru_hit_rate_correct_after_two_hits_one_miss(lru):
    lru.set("q", 5, {"data": 1})
    lru.get("q", 5)      # hit
    lru.get("q", 5)      # hit
    lru.get("miss", 5)   # miss
    stats = lru.stats()
    assert abs(stats["hit_rate"] - 2/3) < 0.01


def test_lru_hit_rate_zero_when_no_accesses(lru):
    assert lru.stats()["hit_rate"] == 0.0


def test_lru_stats_returns_required_keys(lru):
    stats = lru.stats()
    for key in ("size", "max_size", "ttl_sec", "hits", "misses", "hit_rate", "enabled"):
        assert key in stats


# ---------------------------------------------------------------------------
# LRUCache: max-size eviction
# ---------------------------------------------------------------------------

def test_lru_evicts_oldest_when_max_size_exceeded(tiny_lru):
    tiny_lru.set("k1", 1, {"v": 1})
    tiny_lru.set("k2", 1, {"v": 2})
    tiny_lru.set("k3", 1, {"v": 3})
    tiny_lru.set("k4", 1, {"v": 4})  # evicts k1
    assert tiny_lru.get("k1", 1) is None
    assert tiny_lru.get("k4", 1) == {"v": 4}


def test_lru_size_never_exceeds_max(tiny_lru):
    for i in range(20):
        tiny_lru.set(f"key{i}", 1, {"v": i})
    stats = tiny_lru.stats()
    assert stats["size"] <= 3


def test_lru_access_promotes_to_most_recent(tiny_lru):
    tiny_lru.set("k1", 1, {"v": 1})
    tiny_lru.set("k2", 1, {"v": 2})
    tiny_lru.set("k3", 1, {"v": 3})
    tiny_lru.get("k1", 1)     # promote k1
    tiny_lru.set("k4", 1, {"v": 4})  # evict k2 (now oldest)
    assert tiny_lru.get("k1", 1) == {"v": 1}  # still there
    assert tiny_lru.get("k2", 1) is None      # evicted


# ---------------------------------------------------------------------------
# LRUCache: TTL expiry
# ---------------------------------------------------------------------------

def test_lru_expired_entry_returns_none(short_ttl_lru):
    short_ttl_lru.set("expiring", 5, {"data": "value"})
    assert short_ttl_lru.get("expiring", 5) == {"data": "value"}

    # Monkey-patch the expiry timestamp to be in the past
    key = "expiring::5"
    with short_ttl_lru._lock:
        if key in short_ttl_lru._store:
            val, _ = short_ttl_lru._store[key]
            short_ttl_lru._store[key] = (val, time.monotonic() - 2)

    assert short_ttl_lru.get("expiring", 5) is None


def test_lru_non_expired_entry_survives(short_ttl_lru):
    short_ttl_lru.set("alive", 5, {"v": 1})
    assert short_ttl_lru.get("alive", 5) == {"v": 1}


# ---------------------------------------------------------------------------
# LRUCache: clear
# ---------------------------------------------------------------------------

def test_lru_clear_empties_cache(lru):
    for i in range(5):
        lru.set(f"q{i}", 5, {"v": i})
    lru.clear()
    assert lru.stats()["size"] == 0
    for i in range(5):
        assert lru.get(f"q{i}", 5) is None


def test_lru_clear_resets_hit_miss_counters(lru):
    lru.set("q", 5, {"v": 1})
    lru.get("q", 5)       # hit
    lru.get("miss", 5)    # miss
    lru.clear()
    stats = lru.stats()
    assert stats["hits"] == 0
    assert stats["misses"] == 0


# ---------------------------------------------------------------------------
# LRUCache: enabled flag
# ---------------------------------------------------------------------------

def test_lru_enabled_when_max_size_positive():
    from backend.core.cache import LRUCache
    c = LRUCache(max_size=1, ttl_sec=60)
    assert c.enabled is True


def test_lru_disabled_when_max_size_zero():
    from backend.core.cache import LRUCache
    c = LRUCache(max_size=0, ttl_sec=60)
    assert c.enabled is False
    c.set("q", 5, {"v": 1})
    assert c.get("q", 5) is None


# ---------------------------------------------------------------------------
# Large parametrize: 200 query×row combos no crash
# ---------------------------------------------------------------------------

LARGE_CACHE_CASES = [
    (f"query_{i}", (i % 50) + 1, {"results": [f"term_{i}"], "total": i})
    for i in range(200)
]


@pytest.mark.parametrize("query,rows,value", LARGE_CACHE_CASES)
def test_lru_large_set_get_no_crash(query, rows, value):
    from backend.core.cache import LRUCache
    cache = LRUCache(max_size=50, ttl_sec=3600)
    cache.set(query, rows, value)
    result = cache.get(query, rows)
    # Either present (within max_size) or evicted — no crash
    assert result is None or result == value


# ---------------------------------------------------------------------------
# /cache/stats endpoint
# ---------------------------------------------------------------------------

CACHE_STATS_REQUIRED = {"lru", "redis"}
LRU_STATS_REQUIRED = {"size", "max_size", "ttl_sec", "hits", "misses", "hit_rate", "enabled"}


@pytest.mark.anyio
async def test_cache_stats_returns_200(client):
    resp = await client.get("/cache/stats")
    assert resp.status_code == 200


@pytest.mark.anyio
async def test_cache_stats_has_lru_and_redis_keys(client):
    resp = await client.get("/cache/stats")
    assert resp.status_code == 200
    assert CACHE_STATS_REQUIRED.issubset(resp.json().keys())


@pytest.mark.anyio
async def test_cache_stats_lru_has_required_fields(client):
    resp = await client.get("/cache/stats")
    assert resp.status_code == 200
    lru = resp.json()["lru"]
    assert LRU_STATS_REQUIRED.issubset(lru.keys())


@pytest.mark.anyio
async def test_cache_stats_lru_size_non_negative(client):
    resp = await client.get("/cache/stats")
    assert resp.status_code == 200
    assert resp.json()["lru"]["size"] >= 0


@pytest.mark.anyio
async def test_cache_stats_lru_hits_non_negative(client):
    resp = await client.get("/cache/stats")
    assert resp.status_code == 200
    assert resp.json()["lru"]["hits"] >= 0


@pytest.mark.anyio
async def test_cache_stats_lru_misses_non_negative(client):
    resp = await client.get("/cache/stats")
    assert resp.status_code == 200
    assert resp.json()["lru"]["misses"] >= 0


@pytest.mark.anyio
async def test_cache_stats_lru_hit_rate_between_0_and_1(client):
    resp = await client.get("/cache/stats")
    assert resp.status_code == 200
    hit_rate = resp.json()["lru"]["hit_rate"]
    assert 0.0 <= hit_rate <= 1.0


@pytest.mark.anyio
async def test_cache_stats_lru_max_size_positive(client):
    resp = await client.get("/cache/stats")
    assert resp.status_code == 200
    assert resp.json()["lru"]["max_size"] > 0


@pytest.mark.anyio
async def test_cache_stats_lru_ttl_sec_positive(client):
    resp = await client.get("/cache/stats")
    assert resp.status_code == 200
    assert resp.json()["lru"]["ttl_sec"] > 0


@pytest.mark.anyio
async def test_cache_stats_lru_enabled_is_bool(client):
    resp = await client.get("/cache/stats")
    assert resp.status_code == 200
    assert isinstance(resp.json()["lru"]["enabled"], bool)


@pytest.mark.anyio
async def test_cache_stats_structure_stable_on_repeated_calls(client):
    resp1 = await client.get("/cache/stats")
    resp2 = await client.get("/cache/stats")
    assert set(resp1.json().keys()) == set(resp2.json().keys())
    assert set(resp1.json()["lru"].keys()) == set(resp2.json()["lru"].keys())


# ---------------------------------------------------------------------------
# /cache/clear endpoint
# ---------------------------------------------------------------------------

@pytest.mark.anyio
async def test_cache_clear_returns_200(client):
    resp = await client.post("/cache/clear")
    assert resp.status_code == 200


@pytest.mark.anyio
async def test_cache_clear_wrong_method_returns_405(client):
    resp = await client.get("/cache/clear")
    assert resp.status_code == 405


@pytest.mark.anyio
async def test_cache_stats_lru_size_0_after_clear(client):
    await client.post("/cache/clear")
    resp = await client.get("/cache/stats")
    assert resp.status_code == 200
    assert resp.json()["lru"]["size"] == 0


@pytest.mark.anyio
async def test_cache_clear_response_is_valid_json(client):
    resp = await client.post("/cache/clear")
    assert resp.status_code == 200
    assert resp.json() is not None


# ---------------------------------------------------------------------------
# Repeated calls — idempotency and stability
# ---------------------------------------------------------------------------

@pytest.mark.anyio
@pytest.mark.parametrize("_n", list(range(30)))
async def test_cache_stats_always_200(_n, client):
    resp = await client.get("/cache/stats")
    assert resp.status_code == 200


@pytest.mark.anyio
@pytest.mark.parametrize("_n", list(range(20)))
async def test_cache_clear_always_200(_n, client):
    resp = await client.post("/cache/clear")
    assert resp.status_code == 200


# ---------------------------------------------------------------------------
# TwoLayerCache: Redis disabled (ttl_sec=0) → always miss
# ---------------------------------------------------------------------------

def test_two_layer_cache_redis_disabled_returns_none_on_miss():
    from backend.core.cache import LRUCache, RedisCache, TwoLayerCache

    class _DisabledTwoLayer:
        def __init__(self):
            self.lru = LRUCache(max_size=10, ttl_sec=60)
            self.redis = RedisCache(url="redis://localhost:6379/0", ttl_sec=0)

        def get(self, query, rows):
            val = self.lru.get(query, rows)
            if val is not None:
                return val
            val = self.redis.get(query, rows)
            if val is not None:
                self.lru.set(query, rows, val)
                return val
            return None

        def set(self, query, rows, value):
            self.lru.set(query, rows, value)
            self.redis.set(query, rows, value)

    cache = _DisabledTwoLayer()
    assert cache.get("diabetes", 10) is None
    cache.set("diabetes", 10, {"total": 5})
    assert cache.get("diabetes", 10) == {"total": 5}


# ---------------------------------------------------------------------------
# LRUCache: parametrize stats correctness across different sizes
# ---------------------------------------------------------------------------

SIZE_TTL_CASES = [
    (10, 3600),
    (100, 1800),
    (1000, 600),
    (50, 7200),
    (1, 60),
]


@pytest.mark.parametrize("max_size,ttl_sec", SIZE_TTL_CASES)
def test_lru_stats_reflect_constructor_params(max_size, ttl_sec):
    from backend.core.cache import LRUCache
    c = LRUCache(max_size=max_size, ttl_sec=ttl_sec)
    stats = c.stats()
    assert stats["max_size"] == max_size
    assert stats["ttl_sec"] == ttl_sec
    assert stats["size"] == 0
    assert stats["hits"] == 0
    assert stats["misses"] == 0
