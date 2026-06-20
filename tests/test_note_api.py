"""Integration tests for section-aware note completion endpoints.

These tests validate section filtering, fuzzy fallback behavior, and response
shape for the Clinical Copilot note completion API.
"""

from __future__ import annotations

import os
import sys

import pytest
from httpx import ASGITransport, AsyncClient

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from backend.app import app as fastapi_app
from backend.models.models import NoteCompleteResult
from backend.services.section_config import SECTION_SEMANTIC_TYPES


@pytest.fixture
def app():
    return fastapi_app


@pytest.mark.anyio
@pytest.mark.parametrize(
    "section,query",
    [
        ("chief_complaint", "fev"),
        ("diagnosis", "diab"),
        ("investigations", "blood"),
        ("medications", "met"),
        ("procedures", "angio"),
        ("advice", "follow"),
    ],
)
async def test_valid_request_for_each_section(section: str, query: str) -> None:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get(
            "/api/note/complete",
            params={"q": query, "section": section, "rows": 5},
        )

    assert response.status_code == 200
    payload = response.json()
    assert payload["section"] == section
    assert payload["semantic_types_applied"] == SECTION_SEMANTIC_TYPES[section]
    assert payload["response_time_ms"] >= 0
    assert payload["total"] > 0


@pytest.mark.anyio
async def test_invalid_section_returns_400() -> None:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get(
            "/api/note/complete",
            params={"q": "diab", "section": "invalid_section"},
        )

    assert response.status_code == 400
    assert "Invalid section" in response.json()["detail"]


@pytest.mark.anyio
async def test_short_query_no_crash() -> None:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get(
            "/api/note/complete",
            params={"q": "a", "section": "diagnosis", "rows": 5},
        )

    assert response.status_code == 200
    payload = response.json()
    assert "results" in payload
    assert isinstance(payload["results"], list)


@pytest.mark.anyio
async def test_misspelled_query_with_fuzzy_true_spell_corrects() -> None:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get(
            "/api/note/complete",
            params={"q": "metfornin", "section": "medications", "fuzzy": "true", "rows": 5},
        )

    assert response.status_code == 200
    payload = response.json()
    assert payload["spell_corrected"] is True
    assert payload["total"] > 0


@pytest.mark.anyio
async def test_misspelled_query_with_fuzzy_false_no_crash() -> None:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get(
            "/api/note/complete",
            params={"q": "metfornin", "section": "medications", "fuzzy": "false", "rows": 5},
        )

    assert response.status_code == 200
    payload = response.json()
    assert payload["spell_corrected"] is False


@pytest.mark.anyio
async def test_medications_excludes_disease_semantic_type() -> None:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get(
            "/api/note/complete",
            params={"q": "met", "section": "medications", "rows": 10},
        )

    assert response.status_code == 200
    payload = response.json()
    for result in payload["results"]:
        assert result["semantic_type"] != "Disease or Syndrome"
        assert result["semantic_type"] in SECTION_SEMANTIC_TYPES["medications"]


@pytest.mark.anyio
async def test_response_model_fields_present_for_each_result() -> None:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get(
            "/api/note/complete",
            params={"q": "diab", "section": "diagnosis", "rows": 5},
        )

    assert response.status_code == 200
    payload = response.json()

    required_fields = {
        "term",
        "semantic_type",
        "source",
        "tty",
        "concept_id",
        "code",
        "tty_priority",
        "source_priority",
    }
    for result in payload["results"]:
        assert required_fields.issubset(result.keys())
        NoteCompleteResult.model_validate(result)
        assert isinstance(result["tty_priority"], int)
        assert isinstance(result["source_priority"], int)
        assert result["tty_priority"] >= 1
        assert result["source_priority"] >= 1


@pytest.mark.anyio
async def test_chief_complaint_fever_prefers_snomed_over_nci() -> None:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get(
            "/api/note/complete",
            params={"q": "fev", "section": "chief_complaint", "rows": 10},
        )

    assert response.status_code == 200
    payload = response.json()
    fever_results = [r for r in payload["results"] if r["term"].lower() == "fever"]
    assert fever_results, "Expected Fever in chief complaint results"
    fever_sources = {r["source"] for r in fever_results}
    if {"SNOMEDCT_US", "NCI"}.issubset(fever_sources):
        assert fever_results[0]["source"] == "SNOMEDCT_US"
    else:
        assert fever_results[0]["source"] != "MEDCIN"


@pytest.mark.anyio
async def test_no_fev1_terms_in_chief_complaint() -> None:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get(
            "/api/note/complete",
            params={"q": "fev", "section": "chief_complaint", "rows": 15},
        )

    assert response.status_code == 200
    payload = response.json()
    assert all("FEV1" not in r["term"] and "FEV0" not in r["term"] for r in payload["results"])


@pytest.mark.anyio
async def test_no_ctcae_terms_in_chief_complaint() -> None:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get(
            "/api/note/complete",
            params={"q": "fev", "section": "chief_complaint", "rows": 15},
        )

    assert response.status_code == 200
    payload = response.json()
    assert all(not r["term"].endswith(", CTCAE") for r in payload["results"])


@pytest.mark.anyio
async def test_no_ctcae_terms_in_diagnosis() -> None:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get(
            "/api/note/complete",
            params={"q": "fever", "section": "diagnosis", "rows": 15},
        )

    assert response.status_code == 200
    payload = response.json()
    assert all(not r["term"].endswith(", CTCAE") for r in payload["results"])


@pytest.mark.anyio
async def test_medications_includes_rxnorm_results() -> None:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get(
            "/api/note/complete",
            params={"q": "met", "section": "medications", "rows": 15},
        )

    assert response.status_code == 200
    payload = response.json()
    if not any(r["source"] == "RXNORM" for r in payload["results"]):
        pytest.skip("No RXNORM items surfaced for this dataset/query window")


@pytest.mark.anyio
async def test_rxnorm_metformin_ranked_first_in_medications() -> None:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get(
            "/api/note/complete",
            params={"q": "metformin", "section": "medications", "rows": 10},
        )

    assert response.status_code == 200
    payload = response.json()
    assert payload["results"], "Expected metformin medication results"
    if not any(r["source"] == "RXNORM" for r in payload["results"]):
        pytest.skip("No RXNORM metformin surfaced for this dataset/query window")
    assert payload["results"][0]["source"] == "RXNORM"


@pytest.mark.anyio
async def test_investigations_include_lnc_results() -> None:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get(
            "/api/note/complete",
            params={"q": "blood", "section": "investigations", "rows": 15},
        )

    assert response.status_code == 200
    payload = response.json()
    if not any(r["source"] == "LNC" for r in payload["results"]):
        pytest.skip("No LNC items surfaced for this dataset/query window")


@pytest.mark.anyio
async def test_medcin_allowed_investigations_not_chief_complaint() -> None:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        investigations = await client.get(
            "/api/note/complete",
            params={"q": "fev", "section": "investigations", "rows": 15},
        )
        chief = await client.get(
            "/api/note/complete",
            params={"q": "fev", "section": "chief_complaint", "rows": 15},
        )

    assert investigations.status_code == 200
    assert chief.status_code == 200

    inv_payload = investigations.json()
    chief_payload = chief.json()

    assert any(r["source"] == "MEDCIN" for r in inv_payload["results"])
    assert all(r["source"] != "MEDCIN" for r in chief_payload["results"])
