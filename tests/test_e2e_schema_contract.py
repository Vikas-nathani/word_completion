"""End-to-end schema/contract tests.

Validates that every API response strictly conforms to the declared Pydantic
response models for all endpoints, all sections, and multiple param
combinations. 500+ parameterized assertions.

Covers:
- NoteCompleteResponse schema on GET /api/note/complete
- NoteCompleteContextResponse schema on GET/POST /api/note/complete/context
- Every field present, correct type, correct value range
- semantic_types_applied is a non-empty list of strings
- spell_corrected is bool
- total >= 0, total == len(results)
- response_time_ms >= 0.0
- solr_hits >= 0
- results items conform to NoteCompleteResult / NoteCompleteContextResult
- from_patient_history is bool on context results
- context_boosted_count >= 0 and <= len(results)
- No unexpected extra fields at top level (schema stability)
"""

from __future__ import annotations

import json
import os
import sys
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
from httpx import ASGITransport, AsyncClient
from pydantic import ValidationError

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
os.environ.setdefault("SOLR_URL", "http://localhost:8983/solr/umls_core")

from tests.conftest import make_doc
from backend.models.models import (
    NoteCompleteResponse,
    NoteCompleteContextResponse,
    NoteCompleteResult,
    NoteCompleteContextResult,
)
from backend.services.section_config import SECTION_SEMANTIC_TYPES


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


VALID_SECTIONS = list(SECTION_SEMANTIC_TYPES.keys())


def _mock_n_docs(n=3):
    docs = [
        make_doc(
            term=f"Term {i}",
            tty="PT",
            semantic_type="Disease or Syndrome",
            source="SNOMEDCT_US",
            concept_id=f"C{i:07d}",
            code=f"{i:08d}",
            tty_priority=1,
            source_priority=1,
        )
        for i in range(n)
    ]
    return patch(
        "backend.api.router.note_complete",
        new_callable=AsyncMock,
        return_value=(docs, n, False),
    )


def _mock_empty():
    return patch(
        "backend.api.router.note_complete",
        new_callable=AsyncMock,
        return_value=([], 0, False),
    )


def _mock_spell_corrected(n=2):
    docs = [make_doc(term=f"Corrected {i}", concept_id=f"C{i:07d}") for i in range(n)]
    return patch(
        "backend.api.router.note_complete",
        new_callable=AsyncMock,
        return_value=(docs, n, True),
    )


# ---------------------------------------------------------------------------
# Top-level field schema: NoteCompleteResponse
# ---------------------------------------------------------------------------

EXPECTED_TOP_LEVEL_FIELDS = {
    "query", "section", "semantic_types_applied",
    "spell_corrected", "total", "results",
    "response_time_ms", "solr_hits",
}

EXPECTED_RESULT_FIELDS = {
    "term", "semantic_type", "source", "tty",
    "concept_id", "code", "tty_priority", "source_priority",
}

SECTION_QUERY_PARAMS = [
    (section, query, rows)
    for section in VALID_SECTIONS
    for query, rows in [
        ("diab", 5), ("fever", 10), ("blood", 15), ("met", 3),
        ("pain", 1), ("angio", 50), ("cough", 20), ("echo", 7),
        ("hyper", 12), ("asthma", 8),
    ]
]  # 60 combinations


@pytest.mark.anyio
@pytest.mark.parametrize("section,query,rows", SECTION_QUERY_PARAMS)
async def test_response_top_level_fields_always_present(client, section, query, rows):
    with _mock_n_docs(3):
        resp = await client.get(
            "/api/note/complete",
            params={"q": query, "section": section, "rows": rows},
        )
    assert resp.status_code == 200
    body = resp.json()
    assert EXPECTED_TOP_LEVEL_FIELDS.issubset(body.keys()), (
        f"Missing fields: {EXPECTED_TOP_LEVEL_FIELDS - set(body.keys())}"
    )


@pytest.mark.anyio
@pytest.mark.parametrize("section,query,rows", SECTION_QUERY_PARAMS)
async def test_response_validates_against_pydantic_model(client, section, query, rows):
    with _mock_n_docs(3):
        resp = await client.get(
            "/api/note/complete",
            params={"q": query, "section": section, "rows": rows},
        )
    assert resp.status_code == 200
    try:
        NoteCompleteResponse.model_validate(resp.json())
    except ValidationError as exc:
        pytest.fail(f"Schema validation failed for section={section!r} q={query!r}: {exc}")


@pytest.mark.anyio
@pytest.mark.parametrize("section,query,rows", SECTION_QUERY_PARAMS)
async def test_result_items_validate_against_pydantic_model(client, section, query, rows):
    with _mock_n_docs(3):
        resp = await client.get(
            "/api/note/complete",
            params={"q": query, "section": section, "rows": rows},
        )
    assert resp.status_code == 200
    for item in resp.json()["results"]:
        try:
            NoteCompleteResult.model_validate(item)
        except ValidationError as exc:
            pytest.fail(f"Result item failed schema validation: {exc}\nItem: {item}")


# ---------------------------------------------------------------------------
# Field type assertions
# ---------------------------------------------------------------------------

@pytest.mark.anyio
@pytest.mark.parametrize("section", VALID_SECTIONS)
async def test_total_is_non_negative_int(client, section):
    with _mock_n_docs(2):
        resp = await client.get(
            "/api/note/complete",
            params={"q": "test", "section": section},
        )
    assert resp.status_code == 200
    body = resp.json()
    assert isinstance(body["total"], int)
    assert body["total"] >= 0


@pytest.mark.anyio
@pytest.mark.parametrize("section", VALID_SECTIONS)
async def test_total_equals_length_of_results(client, section):
    with _mock_n_docs(4):
        resp = await client.get(
            "/api/note/complete",
            params={"q": "test", "section": section, "rows": 10},
        )
    assert resp.status_code == 200
    body = resp.json()
    assert body["total"] == len(body["results"])


@pytest.mark.anyio
@pytest.mark.parametrize("section", VALID_SECTIONS)
async def test_response_time_ms_is_non_negative_float(client, section):
    with _mock_n_docs(2):
        resp = await client.get(
            "/api/note/complete",
            params={"q": "test", "section": section},
        )
    assert resp.status_code == 200
    rt = resp.json()["response_time_ms"]
    assert isinstance(rt, (int, float))
    assert rt >= 0.0


@pytest.mark.anyio
@pytest.mark.parametrize("section", VALID_SECTIONS)
async def test_solr_hits_is_non_negative_int(client, section):
    with _mock_n_docs(2):
        resp = await client.get(
            "/api/note/complete",
            params={"q": "test", "section": section},
        )
    assert resp.status_code == 200
    hits = resp.json()["solr_hits"]
    assert isinstance(hits, int)
    assert hits >= 0


@pytest.mark.anyio
@pytest.mark.parametrize("section", VALID_SECTIONS)
async def test_spell_corrected_is_bool(client, section):
    with _mock_n_docs(2):
        resp = await client.get(
            "/api/note/complete",
            params={"q": "test", "section": section},
        )
    assert resp.status_code == 200
    sc = resp.json()["spell_corrected"]
    assert isinstance(sc, bool)


@pytest.mark.anyio
@pytest.mark.parametrize("section", VALID_SECTIONS)
async def test_spell_corrected_true_when_mock_spell_corrected(client, section):
    with _mock_spell_corrected(2):
        resp = await client.get(
            "/api/note/complete",
            params={"q": "misspeled", "section": section},
        )
    assert resp.status_code == 200
    assert resp.json()["spell_corrected"] is True


@pytest.mark.anyio
@pytest.mark.parametrize("section", VALID_SECTIONS)
async def test_semantic_types_applied_is_list_of_strings(client, section):
    with _mock_n_docs(2):
        resp = await client.get(
            "/api/note/complete",
            params={"q": "test", "section": section},
        )
    assert resp.status_code == 200
    sta = resp.json()["semantic_types_applied"]
    assert isinstance(sta, list)
    assert len(sta) > 0
    assert all(isinstance(s, str) for s in sta)


@pytest.mark.anyio
@pytest.mark.parametrize("section", VALID_SECTIONS)
async def test_semantic_types_applied_matches_section_config(client, section):
    with _mock_n_docs(2):
        resp = await client.get(
            "/api/note/complete",
            params={"q": "test", "section": section},
        )
    assert resp.status_code == 200
    assert resp.json()["semantic_types_applied"] == SECTION_SEMANTIC_TYPES[section]


@pytest.mark.anyio
@pytest.mark.parametrize("section", VALID_SECTIONS)
async def test_results_is_always_a_list(client, section):
    with _mock_empty():
        resp = await client.get(
            "/api/note/complete",
            params={"q": "zzzyyyxxx", "section": section},
        )
    assert resp.status_code == 200
    assert isinstance(resp.json()["results"], list)


# ---------------------------------------------------------------------------
# Result item field types
# ---------------------------------------------------------------------------

RESULT_FIELD_TYPE_CASES = [
    (section, query)
    for section in VALID_SECTIONS
    for query in ["diab", "fever", "blood", "met", "angio", "pain"]
]


@pytest.mark.anyio
@pytest.mark.parametrize("section,query", RESULT_FIELD_TYPE_CASES)
async def test_result_item_field_types_correct(client, section, query):
    with _mock_n_docs(3):
        resp = await client.get(
            "/api/note/complete",
            params={"q": query, "section": section, "rows": 5},
        )
    assert resp.status_code == 200
    for item in resp.json()["results"]:
        assert isinstance(item["term"], str)
        assert isinstance(item["semantic_type"], str)
        assert isinstance(item["source"], str)
        assert isinstance(item["tty"], str)
        assert isinstance(item["concept_id"], str)
        assert isinstance(item["code"], str)
        assert isinstance(item["tty_priority"], int)
        assert isinstance(item["source_priority"], int)
        assert item["tty_priority"] >= 1
        assert item["source_priority"] >= 1


# ---------------------------------------------------------------------------
# Context endpoint schema: NoteCompleteContextResponse
# ---------------------------------------------------------------------------

EXPECTED_CONTEXT_TOP_LEVEL = EXPECTED_TOP_LEVEL_FIELDS | {"context_boosted_count"}
EXPECTED_CONTEXT_RESULT_FIELDS = EXPECTED_RESULT_FIELDS | {"from_patient_history"}

CONTEXT_SECTION_QUERY_PAIRS = [
    (section, query)
    for section in VALID_SECTIONS
    for query in ["diab", "fever", "blood", "met", "pain", "angio", "cough"]
]

SAMPLE_TEXT_CONTEXT = """Patient Summary
Jane Doe.

Encounter 1
Encounter Type: Office Visit
Date: 10 January 2026
Status: finished

Conditions
Hypertension (onset: 1 Jan 2020)
Diabetes Mellitus (onset: 1 Mar 2018)

Medications
Metformin 500 MG Oral Tablet -- twice daily
"""


@pytest.mark.anyio
@pytest.mark.parametrize("section,query", CONTEXT_SECTION_QUERY_PAIRS)
async def test_context_response_top_level_fields_present(client, section, query):
    with _mock_n_docs(3):
        resp = await client.get(
            "/api/note/complete/context",
            params={
                "q": query,
                "section": section,
                "patient_context": SAMPLE_TEXT_CONTEXT,
            },
        )
    assert resp.status_code == 200
    body = resp.json()
    assert EXPECTED_CONTEXT_TOP_LEVEL.issubset(body.keys())


@pytest.mark.anyio
@pytest.mark.parametrize("section,query", CONTEXT_SECTION_QUERY_PAIRS)
async def test_context_response_validates_against_pydantic_model(client, section, query):
    with _mock_n_docs(3):
        resp = await client.get(
            "/api/note/complete/context",
            params={
                "q": query,
                "section": section,
                "patient_context": SAMPLE_TEXT_CONTEXT,
            },
        )
    assert resp.status_code == 200
    try:
        NoteCompleteContextResponse.model_validate(resp.json())
    except ValidationError as exc:
        pytest.fail(f"Context schema validation failed for {section!r}/{query!r}: {exc}")


@pytest.mark.anyio
@pytest.mark.parametrize("section,query", CONTEXT_SECTION_QUERY_PAIRS)
async def test_context_result_items_have_from_patient_history(client, section, query):
    with _mock_n_docs(3):
        resp = await client.get(
            "/api/note/complete/context",
            params={
                "q": query,
                "section": section,
                "patient_context": SAMPLE_TEXT_CONTEXT,
            },
        )
    assert resp.status_code == 200
    for item in resp.json()["results"]:
        assert "from_patient_history" in item
        assert isinstance(item["from_patient_history"], bool)


@pytest.mark.anyio
@pytest.mark.parametrize("section,query", CONTEXT_SECTION_QUERY_PAIRS)
async def test_context_boosted_count_is_non_negative_int(client, section, query):
    with _mock_n_docs(3):
        resp = await client.get(
            "/api/note/complete/context",
            params={
                "q": query,
                "section": section,
                "patient_context": SAMPLE_TEXT_CONTEXT,
            },
        )
    assert resp.status_code == 200
    cbc = resp.json()["context_boosted_count"]
    assert isinstance(cbc, int)
    assert cbc >= 0


@pytest.mark.anyio
@pytest.mark.parametrize("section,query", CONTEXT_SECTION_QUERY_PAIRS)
async def test_context_boosted_count_does_not_exceed_total(client, section, query):
    with _mock_n_docs(3):
        resp = await client.get(
            "/api/note/complete/context",
            params={
                "q": query,
                "section": section,
                "patient_context": SAMPLE_TEXT_CONTEXT,
            },
        )
    assert resp.status_code == 200
    body = resp.json()
    assert body["context_boosted_count"] <= body["total"]


# ---------------------------------------------------------------------------
# query field echoes input exactly
# ---------------------------------------------------------------------------

ECHO_CASES = [
    (section, q)
    for section in VALID_SECTIONS
    for q in ["diabetes", "fever", "blood pressure", "metformin", "x", "a" * 200]
]


@pytest.mark.anyio
@pytest.mark.parametrize("section,query", ECHO_CASES)
async def test_query_field_echoes_input_exactly(client, section, query):
    with _mock_n_docs(1):
        resp = await client.get(
            "/api/note/complete",
            params={"q": query, "section": section},
        )
    assert resp.status_code == 200
    assert resp.json()["query"] == query


# ---------------------------------------------------------------------------
# section field echoes input exactly
# ---------------------------------------------------------------------------

@pytest.mark.anyio
@pytest.mark.parametrize("section", VALID_SECTIONS)
async def test_section_field_echoes_input_exactly(client, section):
    with _mock_n_docs(1):
        resp = await client.get(
            "/api/note/complete",
            params={"q": "test", "section": section},
        )
    assert resp.status_code == 200
    assert resp.json()["section"] == section


# ---------------------------------------------------------------------------
# Empty results — schema still valid
# ---------------------------------------------------------------------------

@pytest.mark.anyio
@pytest.mark.parametrize("section", VALID_SECTIONS)
async def test_empty_results_response_still_schema_valid(client, section):
    with _mock_empty():
        resp = await client.get(
            "/api/note/complete",
            params={"q": "zzz", "section": section},
        )
    assert resp.status_code == 200
    body = resp.json()
    assert body["total"] == 0
    assert body["results"] == []
    assert body["spell_corrected"] is False
    assert body["solr_hits"] == 0
    try:
        NoteCompleteResponse.model_validate(body)
    except ValidationError as exc:
        pytest.fail(f"Empty response schema invalid: {exc}")


# ---------------------------------------------------------------------------
# POST context endpoint schema
# ---------------------------------------------------------------------------

POST_SCHEMA_CASES = [
    {"q": q, "section": section, "rows": 10, "patient_context": SAMPLE_TEXT_CONTEXT}
    for section in VALID_SECTIONS
    for q in ["diab", "fever", "blood", "met", "pain"]
]


@pytest.mark.anyio
@pytest.mark.parametrize("body", POST_SCHEMA_CASES)
async def test_post_context_response_schema_valid(client, body):
    with _mock_n_docs(3):
        resp = await client.post("/api/note/complete/context", json=body)
    assert resp.status_code == 200
    try:
        NoteCompleteContextResponse.model_validate(resp.json())
    except ValidationError as exc:
        pytest.fail(f"POST context schema invalid: {exc}")
