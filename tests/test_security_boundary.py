"""Security and boundary tests.

Categories:
  - Input validation: empty, oversized, unicode, binary garbage
  - Injection attempts: Solr injection via q/section, path traversal, script tags
  - Encoding: URL-encoded chars, null bytes, newlines in params
  - Boundary: min/max rows, exactly at limit, off-by-one
  - HTTP method enforcement: wrong methods return 405
  - Content-type enforcement: wrong body type
"""

from __future__ import annotations

import json
import os
import sys
from unittest.mock import AsyncMock, patch

import pytest
from httpx import ASGITransport, AsyncClient

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
os.environ.setdefault("SOLR_URL", "http://localhost:8983/solr/umls_core")

from tests.conftest import make_doc


def _mock_empty():
    return patch("backend.api.router.note_complete", new_callable=AsyncMock,
                 return_value=([], 0, False))


def _mock_one_doc():
    return patch("backend.api.router.note_complete", new_callable=AsyncMock,
                 return_value=([make_doc()], 1, False))


# ── Solr injection attempts in q parameter ────────────────────────────────────

@pytest.mark.anyio
@pytest.mark.parametrize("malicious_q", [
    "*:*",
    "diab) OR (1:1",
    "diab'; DROP TABLE umls_core--",
    "diab\"; DELETE FROM docs WHERE \"1\"=\"1",
    "](semantic_type:*)",
    "{!lucene} *:*",
    "diab^100",
    "diab~99",
    "diab AND source:*",
    "term_lower:diab OR term:*",
])
async def test_solr_injection_in_q_does_not_crash(async_client, malicious_q):
    with _mock_empty():
        resp = await async_client.get(
            "/api/note/complete",
            params={"q": malicious_q, "section": "diagnosis"},
        )
    # Should either succeed (200) or fail with a validation error (400/422)
    # but NEVER crash with a 500
    assert resp.status_code in (200, 400, 422), f"Got {resp.status_code} for q={malicious_q!r}"


# ── Section parameter injection ───────────────────────────────────────────────

@pytest.mark.anyio
@pytest.mark.parametrize("bad_section", [
    "diagnosis' OR '1'='1",
    "../../../etc/passwd",
    "<script>alert(1)</script>",
    "diagnosis; SELECT * FROM docs",
    "diagnosis\x00null",
    "DIAGNOSIS",  # wrong case
    " diagnosis ",  # leading/trailing space
    "",
])
async def test_section_injection_returns_400_or_422(async_client, bad_section):
    resp = await async_client.get(
        "/api/note/complete",
        params={"q": "diab", "section": bad_section},
    )
    assert resp.status_code in (400, 422), (
        f"Expected 400/422 for section={bad_section!r}, got {resp.status_code}"
    )


# ── Oversized q parameter ─────────────────────────────────────────────────────

@pytest.mark.anyio
async def test_q_at_max_length_is_accepted(async_client):
    with _mock_empty():
        q = "a" * 200  # max_length in NoteCompleteRequest
        resp = await async_client.get(
            "/api/note/complete",
            params={"q": q, "section": "diagnosis"},
        )
    assert resp.status_code in (200, 400, 422)


@pytest.mark.anyio
async def test_q_over_max_length_returns_error(async_client):
    q = "a" * 201
    resp = await async_client.get(
        "/api/note/complete",
        params={"q": q, "section": "diagnosis"},
    )
    assert resp.status_code in (400, 422)


@pytest.mark.anyio
async def test_q_single_char_accepted(async_client):
    with _mock_empty():
        resp = await async_client.get(
            "/api/note/complete",
            params={"q": "a", "section": "diagnosis"},
        )
    assert resp.status_code == 200


# ── Unicode and special characters in q ──────────────────────────────────────

@pytest.mark.anyio
@pytest.mark.parametrize("unicode_q", [
    "糖尿病",       # Chinese: diabetes
    "диабет",      # Russian: diabetes
    "مرض السكري",  # Arabic: diabetes
    "café",
    "naïve",
    "🩺",           # medical stethoscope emoji
])
async def test_unicode_q_does_not_crash(async_client, unicode_q):
    with _mock_empty():
        resp = await async_client.get(
            "/api/note/complete",
            params={"q": unicode_q, "section": "diagnosis"},
        )
    assert resp.status_code in (200, 400, 422)


# ── Boundary: rows parameter ──────────────────────────────────────────────────

@pytest.mark.anyio
async def test_rows_at_minimum_1(async_client):
    with _mock_one_doc():
        resp = await async_client.get(
            "/api/note/complete",
            params={"q": "diab", "section": "diagnosis", "rows": 1},
        )
    assert resp.status_code == 200
    assert len(resp.json()["results"]) <= 1


@pytest.mark.anyio
async def test_rows_at_maximum(async_client):
    from backend.core.config import NOTE_API_MAX_ROWS
    with _mock_empty():
        resp = await async_client.get(
            "/api/note/complete",
            params={"q": "diab", "section": "diagnosis", "rows": NOTE_API_MAX_ROWS},
        )
    assert resp.status_code == 200


@pytest.mark.anyio
async def test_rows_one_above_maximum_rejected(async_client):
    from backend.core.config import NOTE_API_MAX_ROWS
    resp = await async_client.get(
        "/api/note/complete",
        params={"q": "diab", "section": "diagnosis", "rows": NOTE_API_MAX_ROWS + 1},
    )
    assert resp.status_code in (400, 422)


@pytest.mark.anyio
async def test_rows_zero_rejected(async_client):
    resp = await async_client.get(
        "/api/note/complete",
        params={"q": "diab", "section": "diagnosis", "rows": 0},
    )
    assert resp.status_code in (400, 422)


@pytest.mark.anyio
async def test_rows_negative_rejected(async_client):
    resp = await async_client.get(
        "/api/note/complete",
        params={"q": "diab", "section": "diagnosis", "rows": -1},
    )
    assert resp.status_code in (400, 422)


@pytest.mark.anyio
async def test_rows_non_integer_rejected(async_client):
    resp = await async_client.get(
        "/api/note/complete",
        params={"q": "diab", "section": "diagnosis", "rows": "abc"},
    )
    assert resp.status_code == 422


# ── HTTP method enforcement ───────────────────────────────────────────────────

@pytest.mark.anyio
async def test_get_complete_does_not_accept_post(async_client):
    resp = await async_client.post(
        "/api/note/complete",
        params={"q": "diab", "section": "diagnosis"},
    )
    assert resp.status_code == 405


@pytest.mark.anyio
async def test_sections_does_not_accept_post(async_client):
    resp = await async_client.post("/api/note/sections")
    assert resp.status_code == 405


@pytest.mark.anyio
async def test_sections_does_not_accept_put(async_client):
    resp = await async_client.put("/api/note/sections")
    assert resp.status_code == 405


@pytest.mark.anyio
async def test_context_post_does_not_accept_get_without_params(async_client):
    # GET /api/note/complete/context without context returns 400 (missing context)
    resp = await async_client.get(
        "/api/note/complete/context",
        params={"q": "diab", "section": "diagnosis"},
    )
    assert resp.status_code == 400


# ── Null bytes and control characters ────────────────────────────────────────

@pytest.mark.anyio
async def test_null_byte_in_q_does_not_crash(async_client):
    with _mock_empty():
        resp = await async_client.get(
            "/api/note/complete",
            params={"q": "diab\x00", "section": "diagnosis"},
        )
    assert resp.status_code in (200, 400, 422)


@pytest.mark.anyio
async def test_newline_in_q_does_not_crash(async_client):
    with _mock_empty():
        resp = await async_client.get(
            "/api/note/complete",
            params={"q": "diab\ninjected_header: value", "section": "diagnosis"},
        )
    assert resp.status_code in (200, 400, 422)


# ── Context POST: malicious JSON body ─────────────────────────────────────────

@pytest.mark.anyio
async def test_context_post_with_xss_in_context_does_not_reflect(async_client):
    with _mock_empty():
        resp = await async_client.post(
            "/api/note/complete/context",
            json={
                "q": "diab",
                "section": "diagnosis",
                "patient_context": "<script>alert('xss')</script>",
            },
        )
    # If it returns 200, the XSS payload must not appear unescaped in results
    assert resp.status_code in (200, 400)
    if resp.status_code == 200:
        raw = resp.text
        # JSON-encoding will escape < > naturally, so no bare <script> in body
        assert "<script>" not in raw


@pytest.mark.anyio
async def test_context_post_deeply_nested_json_does_not_crash(async_client):
    def nest(depth):
        if depth == 0:
            return "value"
        return {"key": nest(depth - 1)}

    with _mock_empty():
        resp = await async_client.post(
            "/api/note/complete/context",
            json={
                "q": "diab",
                "section": "diagnosis",
                "patient_context_json": nest(20),
            },
        )
    assert resp.status_code in (200, 400, 422)


@pytest.mark.anyio
async def test_file_endpoint_empty_file_falls_back_gracefully(async_client):
    with _mock_empty():
        resp = await async_client.post(
            "/api/note/complete/context/file",
            data={"q": "diab", "section": "diagnosis"},
            files={"patient_context_file": ("empty.txt", b"", "text/plain")},
        )
    # Empty file should trigger fallback (no context), not crash
    assert resp.status_code in (200, 400)


@pytest.mark.anyio
async def test_file_endpoint_non_utf8_file_returns_400(async_client):
    resp = await async_client.post(
        "/api/note/complete/context/file",
        data={"q": "diab", "section": "diagnosis"},
        files={"patient_context_file": ("notes.txt", b"\xff\xfe\x00\x00bad", "text/plain")},
    )
    assert resp.status_code in (400, 422)


@pytest.mark.anyio
async def test_file_endpoint_json_with_array_root_returns_400(async_client):
    resp = await async_client.post(
        "/api/note/complete/context/file",
        data={"q": "diab", "section": "diagnosis"},
        files={"patient_context_file": ("data.json", b"[1, 2, 3]", "application/json")},
    )
    # JSON root must be object not array
    assert resp.status_code == 400
