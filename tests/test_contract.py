"""Contract / schema tests — verify every API response field matches its declared type.

These tests treat the API as a black box: they make a request (with Solr mocked)
and assert the JSON response is structurally valid against the Pydantic models and
against explicit type expectations — so a backend change that silently drops a field
or changes a type will be caught here.
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


# ── Expected field contracts ──────────────────────────────────────────────────

NOTE_COMPLETE_RESPONSE_FIELDS = {
    "query": str,
    "section": str,
    "semantic_types_applied": list,
    "spell_corrected": bool,
    "total": int,
    "results": list,
    "response_time_ms": float,
    "solr_hits": int,
}

NOTE_COMPLETE_RESULT_FIELDS = {
    "term": str,
    "semantic_type": str,
    "source": str,
    "tty": str,
    "concept_id": str,
    "code": str,
    "tty_priority": int,
    "source_priority": int,
}

CONTEXT_RESPONSE_EXTRA_FIELDS = {
    "context_boosted_count": int,
}

CONTEXT_RESULT_EXTRA_FIELDS = {
    "from_patient_history": bool,
}

SECTIONS_RESPONSE_FIELDS = {
    "sections": list,
    "total": int,
}


# ── Helpers ───────────────────────────────────────────────────────────────────

def _assert_schema(payload: dict, expected: dict, path: str = ""):
    for field, expected_type in expected.items():
        full_path = f"{path}.{field}" if path else field
        assert field in payload, f"Missing field: {full_path!r}"
        actual = payload[field]
        assert isinstance(actual, expected_type), (
            f"Field {full_path!r}: expected {expected_type.__name__}, got {type(actual).__name__} = {actual!r}"
        )


def _make_mock_docs(n=3):
    return [
        make_doc(
            term=f"Term {i}",
            concept_id=f"C{i:07d}",
            tty="PT",
            semantic_type="Disease or Syndrome",
            source="SNOMEDCT_US",
        )
        for i in range(n)
    ]


# ── /api/note/complete contract ───────────────────────────────────────────────

@pytest.mark.anyio
async def test_note_complete_response_top_level_schema(async_client):
    with patch("backend.api.router.note_complete", new_callable=AsyncMock) as mock:
        mock.return_value = (_make_mock_docs(3), 100, False)
        resp = await async_client.get(
            "/api/note/complete",
            params={"q": "diab", "section": "diagnosis", "rows": 3},
        )

    assert resp.status_code == 200
    payload = resp.json()
    _assert_schema(payload, NOTE_COMPLETE_RESPONSE_FIELDS)


@pytest.mark.anyio
async def test_note_complete_result_item_schema(async_client):
    with patch("backend.api.router.note_complete", new_callable=AsyncMock) as mock:
        mock.return_value = (_make_mock_docs(2), 2, False)
        resp = await async_client.get(
            "/api/note/complete",
            params={"q": "diab", "section": "diagnosis", "rows": 5},
        )

    payload = resp.json()
    assert len(payload["results"]) > 0
    for item in payload["results"]:
        _assert_schema(item, NOTE_COMPLETE_RESULT_FIELDS, path="results[*]")


@pytest.mark.anyio
async def test_note_complete_total_matches_results_length(async_client):
    docs = _make_mock_docs(4)
    with patch("backend.api.router.note_complete", new_callable=AsyncMock) as mock:
        mock.return_value = (docs, 4, False)
        resp = await async_client.get(
            "/api/note/complete",
            params={"q": "diab", "section": "diagnosis", "rows": 10},
        )

    payload = resp.json()
    assert payload["total"] == len(payload["results"])


@pytest.mark.anyio
async def test_note_complete_response_time_ms_non_negative(async_client):
    with patch("backend.api.router.note_complete", new_callable=AsyncMock) as mock:
        mock.return_value = ([], 0, False)
        resp = await async_client.get(
            "/api/note/complete",
            params={"q": "xyz", "section": "diagnosis"},
        )

    payload = resp.json()
    assert payload["response_time_ms"] >= 0.0


@pytest.mark.anyio
async def test_note_complete_query_echoed_back(async_client):
    with patch("backend.api.router.note_complete", new_callable=AsyncMock) as mock:
        mock.return_value = ([], 0, False)
        resp = await async_client.get(
            "/api/note/complete",
            params={"q": "diabetez", "section": "diagnosis"},
        )

    payload = resp.json()
    assert payload["query"] == "diabetez"


@pytest.mark.anyio
async def test_note_complete_section_echoed_back(async_client):
    with patch("backend.api.router.note_complete", new_callable=AsyncMock) as mock:
        mock.return_value = ([], 0, False)
        resp = await async_client.get(
            "/api/note/complete",
            params={"q": "heart", "section": "chief_complaint"},
        )

    payload = resp.json()
    assert payload["section"] == "chief_complaint"


@pytest.mark.anyio
async def test_note_complete_tty_priority_is_int_gte_1(async_client):
    with patch("backend.api.router.note_complete", new_callable=AsyncMock) as mock:
        mock.return_value = (_make_mock_docs(5), 5, False)
        resp = await async_client.get(
            "/api/note/complete",
            params={"q": "diab", "section": "diagnosis", "rows": 5},
        )

    payload = resp.json()
    for item in payload["results"]:
        assert isinstance(item["tty_priority"], int)
        assert item["tty_priority"] >= 1


@pytest.mark.anyio
async def test_note_complete_source_priority_is_int_gte_1(async_client):
    with patch("backend.api.router.note_complete", new_callable=AsyncMock) as mock:
        mock.return_value = (_make_mock_docs(5), 5, False)
        resp = await async_client.get(
            "/api/note/complete",
            params={"q": "diab", "section": "diagnosis", "rows": 5},
        )

    payload = resp.json()
    for item in payload["results"]:
        assert isinstance(item["source_priority"], int)
        assert item["source_priority"] >= 1


@pytest.mark.anyio
async def test_note_complete_spell_corrected_is_bool(async_client):
    with patch("backend.api.router.note_complete", new_callable=AsyncMock) as mock:
        mock.return_value = (_make_mock_docs(1), 1, True)
        resp = await async_client.get(
            "/api/note/complete",
            params={"q": "diabetez", "section": "diagnosis"},
        )

    payload = resp.json()
    assert isinstance(payload["spell_corrected"], bool)


@pytest.mark.anyio
async def test_note_complete_semantic_types_applied_is_list_of_strings(async_client):
    with patch("backend.api.router.note_complete", new_callable=AsyncMock) as mock:
        mock.return_value = ([], 0, False)
        resp = await async_client.get(
            "/api/note/complete",
            params={"q": "diab", "section": "diagnosis"},
        )

    payload = resp.json()
    types = payload["semantic_types_applied"]
    assert isinstance(types, list)
    assert all(isinstance(t, str) for t in types)
    assert len(types) > 0


# ── /api/note/complete/context (POST) contract ────────────────────────────────

@pytest.mark.anyio
async def test_context_post_response_top_level_schema(async_client):
    with patch("backend.api.router.note_complete", new_callable=AsyncMock) as mock:
        mock.return_value = (_make_mock_docs(2), 2, False)
        resp = await async_client.post(
            "/api/note/complete/context",
            json={
                "q": "diab",
                "section": "diagnosis",
                "patient_context": "Patient has diabetes mellitus.",
            },
        )

    assert resp.status_code == 200
    payload = resp.json()
    _assert_schema(payload, {**NOTE_COMPLETE_RESPONSE_FIELDS, **CONTEXT_RESPONSE_EXTRA_FIELDS})


@pytest.mark.anyio
async def test_context_post_result_item_has_from_patient_history(async_client):
    with patch("backend.api.router.note_complete", new_callable=AsyncMock) as mock:
        mock.return_value = (_make_mock_docs(2), 2, False)
        resp = await async_client.post(
            "/api/note/complete/context",
            json={
                "q": "diab",
                "section": "diagnosis",
                "patient_context": "Patient has diabetes mellitus.",
            },
        )

    payload = resp.json()
    for item in payload["results"]:
        _assert_schema(item, {**NOTE_COMPLETE_RESULT_FIELDS, **CONTEXT_RESULT_EXTRA_FIELDS}, path="results[*]")


@pytest.mark.anyio
async def test_context_post_context_boosted_count_is_non_negative(async_client):
    with patch("backend.api.router.note_complete", new_callable=AsyncMock) as mock:
        mock.return_value = (_make_mock_docs(2), 2, False)
        resp = await async_client.post(
            "/api/note/complete/context",
            json={
                "q": "diab",
                "section": "diagnosis",
                "patient_context_json": {
                    "conditions": [{"term": "Diabetes Mellitus", "status": "active"}]
                },
            },
        )

    payload = resp.json()
    assert payload["context_boosted_count"] >= 0


# ── /api/note/sections contract ───────────────────────────────────────────────

@pytest.mark.anyio
async def test_sections_response_top_level_schema(async_client):
    resp = await async_client.get("/api/note/sections")
    assert resp.status_code == 200
    payload = resp.json()
    _assert_schema(payload, SECTIONS_RESPONSE_FIELDS)


@pytest.mark.anyio
async def test_sections_each_item_has_name_and_semantic_types(async_client):
    resp = await async_client.get("/api/note/sections")
    payload = resp.json()
    for section in payload["sections"]:
        assert "name" in section
        assert "semantic_types" in section
        assert isinstance(section["name"], str)
        assert isinstance(section["semantic_types"], list)
        assert len(section["semantic_types"]) > 0


@pytest.mark.anyio
async def test_sections_total_matches_sections_length(async_client):
    resp = await async_client.get("/api/note/sections")
    payload = resp.json()
    assert payload["total"] == len(payload["sections"])


@pytest.mark.anyio
async def test_sections_all_default_sections_present(async_client):
    from backend.core.config import VALID_SECTIONS
    resp = await async_client.get("/api/note/sections")
    payload = resp.json()
    section_names = {s["name"] for s in payload["sections"]}
    for expected in VALID_SECTIONS:
        assert expected in section_names


# ── Error response contracts ──────────────────────────────────────────────────

@pytest.mark.anyio
async def test_invalid_section_error_has_detail_field(async_client):
    resp = await async_client.get(
        "/api/note/complete",
        params={"q": "diab", "section": "FAKE_SECTION"},
    )
    assert resp.status_code == 400
    payload = resp.json()
    assert "detail" in payload
    assert isinstance(payload["detail"], str)


@pytest.mark.anyio
async def test_missing_params_error_response_is_json(async_client):
    resp = await async_client.get("/api/note/complete")
    assert resp.status_code == 422
    payload = resp.json()
    assert "detail" in payload


@pytest.mark.anyio
async def test_pydantic_model_validates_note_complete_result():
    from backend.models.models import NoteCompleteResult
    item = {
        "term": "Hypertension",
        "semantic_type": "Disease or Syndrome",
        "source": "SNOMEDCT_US",
        "tty": "PT",
        "concept_id": "C0020538",
        "code": "38341003",
        "tty_priority": 1,
        "source_priority": 1,
    }
    model = NoteCompleteResult.model_validate(item)
    assert model.term == "Hypertension"
    assert model.tty_priority == 1


@pytest.mark.anyio
async def test_pydantic_model_validates_context_result():
    from backend.models.models import NoteCompleteContextResult
    item = {
        "term": "Diabetes Mellitus",
        "semantic_type": "Disease or Syndrome",
        "source": "SNOMEDCT_US",
        "tty": "PT",
        "concept_id": "C0011849",
        "code": "73211009",
        "tty_priority": 1,
        "source_priority": 1,
        "from_patient_history": True,
    }
    model = NoteCompleteContextResult.model_validate(item)
    assert model.from_patient_history is True
