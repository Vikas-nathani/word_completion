"""Unit tests for the autocompleter backend — no live Solr required.

Coverage:
  - backend/app.py helper functions
  - backend/services/section_config.py
  - backend/services/search.py (Solr mocked via httpx)
  - backend/api/router.py (FastAPI test client with Solr mocked)
  - router.py _merge_context_and_umls_results helper
"""

from __future__ import annotations

import json
import os
import sys
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from httpx import ASGITransport, AsyncClient, Response

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

os.environ.setdefault("SOLR_URL", "http://localhost:8983/solr/umls_core")


# ─────────────────────────────────────────────────────────────────────────────
# Helpers — fake Solr document factory
# ─────────────────────────────────────────────────────────────────────────────

def _make_doc(
    term="Diabetes Mellitus",
    tty="PT",
    semantic_type="Disease or Syndrome",
    source="SNOMEDCT_US",
    concept_id="C0011849",
    code="73211009",
    tty_priority=1,
    source_priority=1,
    term_word_count=2,
    term_length=16,
    is_abbreviation=False,
):
    return {
        "id": f"{concept_id}_{source}",
        "term": term,
        "tty": tty,
        "semantic_type": semantic_type,
        "source": source,
        "concept_id": concept_id,
        "code": code,
        "tty_priority": tty_priority,
        "source_priority": source_priority,
        "term_word_count": term_word_count,
        "term_length": term_length,
        "is_abbreviation": is_abbreviation,
    }


def _solr_response(docs: list[dict], num_found: int | None = None) -> dict:
    return {
        "response": {
            "numFound": num_found if num_found is not None else len(docs),
            "start": 0,
            "docs": docs,
        }
    }


# ─────────────────────────────────────────────────────────────────────────────
# app.py — pure helper function tests
# ─────────────────────────────────────────────────────────────────────────────

class TestGetScalar:
    def setup_method(self):
        from backend.app import _get_scalar
        self.fn = _get_scalar

    def test_plain_string_field(self):
        assert self.fn({"term": "Fever"}, "term") == "Fever"

    def test_list_field_returns_first_element(self):
        assert self.fn({"term": ["Fever", "Pyrexia"]}, "term") == "Fever"

    def test_empty_list_returns_default(self):
        assert self.fn({"term": []}, "term", "MISSING") == "MISSING"

    def test_missing_key_returns_default(self):
        assert self.fn({}, "term", "DEFAULT") == "DEFAULT"


class TestWordCount:
    def setup_method(self):
        from backend.app import _word_count
        self.fn = _word_count

    def test_single_word(self):
        assert self.fn("Fever") == 1

    def test_two_words(self):
        assert self.fn("Diabetes Mellitus") == 2

    def test_hyphenated_counts_as_one_word(self):
        assert self.fn("type-2") == 1

    def test_empty_string(self):
        assert self.fn("") == 0


class TestBuildAutocompleteQuery:
    def setup_method(self):
        from backend.app import _build_autocomplete_query
        self.fn = _build_autocomplete_query

    def test_single_prefix(self):
        assert self.fn("diab") == "term_lower:diab"

    def test_multi_word_prefix(self):
        result = self.fn("diabetes mellitus")
        assert "term_lower:diabetes" in result
        assert "term_lower:mellitus" in result
        assert "AND" in result

    def test_known_abbreviation_expands(self):
        result = self.fn("htn")
        assert "term_lower:htn" in result
        assert "term_lower:hypertension" in result
        assert "OR" in result

    def test_empty_returns_wildcard(self):
        assert self.fn("") == "*:*"

    def test_dm_expands_to_diabetes_mellitus(self):
        result = self.fn("dm")
        assert "term_lower:diabetes" in result
        assert "term_lower:mellitus" in result


class TestTtyPriorityValue:
    def setup_method(self):
        from backend.app import _tty_priority_value
        self.fn = _tty_priority_value

    def test_pt_is_1(self):
        assert self.fn({"tty": "PT"}) == 1

    def test_pn_is_2(self):
        assert self.fn({"tty": "PN"}) == 2

    def test_sy_is_3(self):
        assert self.fn({"tty": "SY"}) == 3

    def test_list_field_handled(self):
        assert self.fn({"tty": ["PT"]}) == 1

    def test_unknown_tty_falls_back_to_tty_priority_field(self):
        assert self.fn({"tty": "XX", "tty_priority": 7}) == 7

    def test_unknown_tty_no_field_defaults_to_6(self):
        assert self.fn({"tty": "ZZ"}) == 6


class TestSourcePriorityValue:
    def setup_method(self):
        from backend.app import _source_priority_value
        self.fn = _source_priority_value

    def test_snomedct_us_is_1(self):
        assert self.fn({"source": "SNOMEDCT_US"}) == 1

    def test_rxnorm_is_4(self):
        assert self.fn({"source": "RXNORM"}) == 4

    def test_unknown_source_defaults_to_16(self):
        assert self.fn({"source": "UNKNOWN_SRC"}) == 16

    def test_case_insensitive(self):
        assert self.fn({"source": "snomedct_us"}) == 1


class TestFilterDoc:
    def setup_method(self):
        from backend.app import _filter_doc
        self.fn = _filter_doc

    def test_pt_allowed(self):
        assert self.fn({"tty": "PT"}) is True

    def test_sy_allowed(self):
        assert self.fn({"tty": "SY"}) is True

    def test_ab_allowed(self):
        assert self.fn({"tty": "AB"}) is True

    def test_unknown_tty_blocked(self):
        assert self.fn({"tty": "XY"}) is False

    def test_list_tty_field(self):
        assert self.fn({"tty": ["PN"]}) is True


class TestPreferredTierValue:
    def setup_method(self):
        from backend.app import _preferred_tier_value
        self.fn = _preferred_tier_value

    def test_pt_is_tier_0(self):
        assert self.fn({"tty": "PT"}) == 0

    def test_pn_is_tier_0(self):
        assert self.fn({"tty": "PN"}) == 0

    def test_sy_is_tier_1(self):
        assert self.fn({"tty": "SY"}) == 1

    def test_ab_is_tier_1(self):
        assert self.fn({"tty": "AB"}) == 1


class TestBuildBlockedSemanticFq:
    def setup_method(self):
        from backend.app import _build_blocked_semantic_fq
        self.fn = _build_blocked_semantic_fq

    def test_single_word_type_not_quoted(self):
        result = self.fn({"Food"})
        assert '"Food"' not in result
        assert "Food" in result

    def test_multi_word_type_is_quoted(self):
        result = self.fn({"Disease or Syndrome"})
        assert '"Disease or Syndrome"' in result

    def test_output_starts_with_negation(self):
        result = self.fn({"Food"})
        assert result.startswith("-semantic_type:(")

    def test_output_is_deterministic(self):
        types = {"Food", "Language", "Substance"}
        assert self.fn(types) == self.fn(types)


class TestDeduplicateByConceptId:
    def setup_method(self):
        from backend.app import _deduplicate_by_concept_id
        self.fn = _deduplicate_by_concept_id

    def test_same_concept_id_deduped(self):
        docs = [
            _make_doc(concept_id="C0011849", source="SNOMEDCT_US", tty_priority=1),
            _make_doc(concept_id="C0011849", source="ICD10CM", tty_priority=2),
        ]
        result = self.fn(docs)
        assert len(result) == 1
        assert result[0]["source"] == "SNOMEDCT_US"

    def test_different_concept_ids_both_kept(self):
        docs = [
            _make_doc(term="Diabetes", concept_id="C0011849"),
            _make_doc(term="Hypertension", concept_id="C0020538"),
        ]
        result = self.fn(docs)
        assert len(result) == 2

    def test_empty_list_returns_empty(self):
        assert self.fn([]) == []


# ─────────────────────────────────────────────────────────────────────────────
# section_config.py
# ─────────────────────────────────────────────────────────────────────────────

class TestSectionConfig:
    def test_all_default_sections_present(self):
        from backend.services.section_config import SECTION_SEMANTIC_TYPES
        expected = {"chief_complaint", "diagnosis", "investigations", "medications", "procedures", "advice"}
        assert expected == set(SECTION_SEMANTIC_TYPES.keys())

    def test_medications_contains_pharmacologic_substance(self):
        from backend.services.section_config import SECTION_SEMANTIC_TYPES
        assert "Pharmacologic Substance" in SECTION_SEMANTIC_TYPES["medications"]

    def test_diagnosis_contains_disease_or_syndrome(self):
        from backend.services.section_config import SECTION_SEMANTIC_TYPES
        assert "Disease or Syndrome" in SECTION_SEMANTIC_TYPES["diagnosis"]

    def test_chv_excluded_sections(self):
        from backend.services.section_config import CHV_EXCLUDED_SECTIONS
        assert "chief_complaint" in CHV_EXCLUDED_SECTIONS
        assert "diagnosis" in CHV_EXCLUDED_SECTIONS
        assert "investigations" in CHV_EXCLUDED_SECTIONS
        assert "medications" not in CHV_EXCLUDED_SECTIONS

    def test_medication_trusted_sources_includes_rxnorm(self):
        from backend.services.section_config import MEDICATION_TRUSTED_SOURCES
        assert "RXNORM" in MEDICATION_TRUSTED_SOURCES
        assert "SNOMEDCT_US" in MEDICATION_TRUSTED_SOURCES


class TestGetSectionFq:
    def setup_method(self):
        from backend.services.section_config import get_section_fq
        self.fn = get_section_fq

    def test_valid_section_returns_fq_string(self):
        fq = self.fn("diagnosis")
        assert fq.startswith("semantic_type:(")
        assert "Disease or Syndrome" in fq

    def test_unknown_section_raises_value_error(self):
        with pytest.raises(ValueError, match="Unknown section"):
            self.fn("nonexistent_section")

    def test_multi_word_types_are_quoted(self):
        fq = self.fn("chief_complaint")
        assert '"Sign or Symptom"' in fq or '"Disease or Syndrome"' in fq

    def test_medications_fq_contains_pharmacologic_substance(self):
        fq = self.fn("medications")
        assert "Pharmacologic Substance" in fq


class TestQuoteSemanticType:
    def setup_method(self):
        from backend.services.section_config import _quote_semantic_type
        self.fn = _quote_semantic_type

    def test_single_word_not_quoted(self):
        assert self.fn("Finding") == "Finding"

    def test_multi_word_quoted(self):
        assert self.fn("Disease or Syndrome") == '"Disease or Syndrome"'

    def test_term_with_comma_quoted(self):
        result = self.fn("Amino Acid, Peptide, or Protein")
        assert result.startswith('"')
        assert result.endswith('"')


# ─────────────────────────────────────────────────────────────────────────────
# router.py — _merge_context_and_umls_results
# ─────────────────────────────────────────────────────────────────────────────

class TestMergeContextAndUmlsResults:
    def setup_method(self):
        from backend.api.router import _merge_context_and_umls_results
        self.fn = _merge_context_and_umls_results

    def _umls_doc(self, term, concept_id="C0000001", source="SNOMEDCT_US", tty="PT"):
        return _make_doc(term=term, concept_id=concept_id, source=source, tty=tty)

    def test_no_context_matches_returns_umls_only(self):
        docs = [self._umls_doc("Diabetes Mellitus")]
        results, boosted = self.fn(docs, [], rows=10)
        assert len(results) == 1
        assert boosted == 0
        assert results[0].from_patient_history is False

    def test_context_match_boosted_to_top(self):
        docs = [
            self._umls_doc("Hypertension", concept_id="C0020538"),
            self._umls_doc("Diabetes Mellitus", concept_id="C0011849"),
        ]
        context_matches = [{"term": "Diabetes Mellitus"}]
        results, boosted = self.fn(docs, context_matches, rows=10)
        assert results[0].term == "Diabetes Mellitus"
        assert results[0].from_patient_history is True
        assert boosted == 1

    def test_context_only_term_added_as_patient_history(self):
        docs = []
        context_matches = [{"term": "Fracture of bone"}]
        results, boosted = self.fn(docs, context_matches, rows=10)
        assert len(results) == 1
        assert results[0].term == "Fracture of bone"
        assert results[0].from_patient_history is True
        assert results[0].source == "PATIENT_HISTORY"

    def test_duplicate_context_terms_deduplicated(self):
        docs = []
        context_matches = [{"term": "Fever"}, {"term": "fever"}]
        results, boosted = self.fn(docs, context_matches, rows=10)
        assert len(results) == 1

    def test_rows_limit_respected(self):
        docs = [self._umls_doc(f"Term {i}", concept_id=f"C000{i:04d}") for i in range(10)]
        results, _ = self.fn(docs, [], rows=3)
        assert len(results) == 3

    def test_empty_term_in_context_skipped(self):
        docs = []
        context_matches = [{"term": ""}, {"term": "  "}]
        results, boosted = self.fn(docs, context_matches, rows=10)
        assert len(results) == 0
        assert boosted == 0


# ─────────────────────────────────────────────────────────────────────────────
# router.py — FastAPI endpoint tests (Solr mocked)
# ─────────────────────────────────────────────────────────────────────────────

def _make_note_complete_mock(docs=None, num_found=5, spell_corrected=False):
    """Return an async mock for backend.services.search.note_complete."""
    if docs is None:
        docs = [_make_doc()]
    mock = AsyncMock(return_value=(docs, num_found, spell_corrected))
    return mock


@pytest.fixture
def app():
    from backend.app import app as fastapi_app
    return fastapi_app


@pytest.mark.anyio
async def test_note_complete_valid_request(app):
    mock_docs = [_make_doc(term="Diabetes Mellitus")]
    with patch("backend.api.router.note_complete", new_callable=AsyncMock) as mock_nc:
        mock_nc.return_value = (mock_docs, 1, False)
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.get(
                "/api/note/complete",
                params={"q": "diab", "section": "diagnosis", "rows": 5},
            )

    assert resp.status_code == 200
    payload = resp.json()
    assert payload["query"] == "diab"
    assert payload["section"] == "diagnosis"
    assert payload["spell_corrected"] is False
    assert payload["total"] == 1
    assert payload["results"][0]["term"] == "Diabetes Mellitus"


@pytest.mark.anyio
async def test_note_complete_invalid_section_returns_400(app):
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get(
            "/api/note/complete",
            params={"q": "diab", "section": "nonsense_section"},
        )

    assert resp.status_code == 400
    assert "Invalid section" in resp.json()["detail"]


@pytest.mark.anyio
async def test_note_complete_missing_q_returns_422(app):
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get(
            "/api/note/complete",
            params={"section": "diagnosis"},
        )

    assert resp.status_code == 422


@pytest.mark.anyio
async def test_note_complete_missing_section_returns_422(app):
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get(
            "/api/note/complete",
            params={"q": "diab"},
        )

    assert resp.status_code == 422


@pytest.mark.anyio
async def test_note_complete_response_includes_semantic_types_applied(app):
    from backend.services.section_config import SECTION_SEMANTIC_TYPES
    mock_docs = [_make_doc()]
    with patch("backend.api.router.note_complete", new_callable=AsyncMock) as mock_nc:
        mock_nc.return_value = (mock_docs, 1, False)
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.get(
                "/api/note/complete",
                params={"q": "diab", "section": "diagnosis"},
            )

    payload = resp.json()
    assert payload["semantic_types_applied"] == SECTION_SEMANTIC_TYPES["diagnosis"]


@pytest.mark.anyio
async def test_note_complete_spell_corrected_flag_propagated(app):
    mock_docs = [_make_doc(term="Metformin")]
    with patch("backend.api.router.note_complete", new_callable=AsyncMock) as mock_nc:
        mock_nc.return_value = (mock_docs, 1, True)
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.get(
                "/api/note/complete",
                params={"q": "metfornin", "section": "medications", "fuzzy": "true"},
            )

    payload = resp.json()
    assert payload["spell_corrected"] is True


@pytest.mark.anyio
async def test_note_complete_rows_out_of_range_returns_422(app):
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get(
            "/api/note/complete",
            params={"q": "diab", "section": "diagnosis", "rows": 9999},
        )

    assert resp.status_code in (400, 422)


@pytest.mark.anyio
async def test_note_complete_zero_results_returns_empty_list(app):
    with patch("backend.api.router.note_complete", new_callable=AsyncMock) as mock_nc:
        mock_nc.return_value = ([], 0, False)
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.get(
                "/api/note/complete",
                params={"q": "xyzzznotaword", "section": "diagnosis"},
            )

    assert resp.status_code == 200
    payload = resp.json()
    assert payload["total"] == 0
    assert payload["results"] == []


@pytest.mark.anyio
async def test_list_sections_endpoint(app):
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/api/note/sections")

    assert resp.status_code == 200
    payload = resp.json()
    assert "sections" in payload
    section_names = {s["name"] for s in payload["sections"]}
    assert "diagnosis" in section_names
    assert "medications" in section_names
    assert payload["total"] == len(payload["sections"])


@pytest.mark.anyio
async def test_list_sections_each_has_semantic_types(app):
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/api/note/sections")

    payload = resp.json()
    for section in payload["sections"]:
        assert isinstance(section["semantic_types"], list)
        assert len(section["semantic_types"]) > 0


# ─────────────────────────────────────────────────────────────────────────────
# router.py — context endpoint tests (Solr mocked)
# ─────────────────────────────────────────────────────────────────────────────

@pytest.mark.anyio
async def test_context_get_endpoint_missing_context_returns_400(app):
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get(
            "/api/note/complete/context",
            params={"q": "diab", "section": "diagnosis"},
        )

    assert resp.status_code == 400
    assert "patient_context" in resp.json()["detail"].lower()


@pytest.mark.anyio
async def test_context_get_endpoint_with_plain_text_context(app):
    mock_docs = [_make_doc(term="Diabetes Mellitus")]
    with patch("backend.api.router.note_complete", new_callable=AsyncMock) as mock_nc:
        mock_nc.return_value = (mock_docs, 1, False)
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.get(
                "/api/note/complete/context",
                params={
                    "q": "diab",
                    "section": "diagnosis",
                    "patient_context": "Patient has diabetes and hypertension.",
                },
            )

    assert resp.status_code == 200
    payload = resp.json()
    assert "context_boosted_count" in payload


@pytest.mark.anyio
async def test_context_post_endpoint_valid_json_context(app):
    mock_docs = [_make_doc(term="Fever")]
    ctx_json = {
        "conditions": [{"term": "Fever", "status": "active", "onset": "2026-01-01"}]
    }
    with patch("backend.api.router.note_complete", new_callable=AsyncMock) as mock_nc:
        mock_nc.return_value = (mock_docs, 1, False)
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.post(
                "/api/note/complete/context",
                json={
                    "q": "fev",
                    "section": "chief_complaint",
                    "patient_context_json": ctx_json,
                },
            )

    assert resp.status_code == 200
    payload = resp.json()
    assert payload["query"] == "fev"
    assert payload["section"] == "chief_complaint"


@pytest.mark.anyio
async def test_context_post_endpoint_missing_both_contexts_returns_400(app):
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            "/api/note/complete/context",
            json={"q": "diab", "section": "diagnosis"},
        )

    assert resp.status_code == 400


@pytest.mark.anyio
async def test_context_post_invalid_section_returns_400(app):
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            "/api/note/complete/context",
            json={
                "q": "diab",
                "section": "not_a_section",
                "patient_context": "some context",
            },
        )

    assert resp.status_code == 400


# ─────────────────────────────────────────────────────────────────────────────
# router.py — file upload endpoint
# ─────────────────────────────────────────────────────────────────────────────

@pytest.mark.anyio
async def test_context_file_endpoint_no_file_returns_results(app):
    mock_docs = [_make_doc(term="Angina")]
    with patch("backend.api.router.note_complete", new_callable=AsyncMock) as mock_nc:
        mock_nc.return_value = (mock_docs, 1, False)
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.post(
                "/api/note/complete/context/file",
                data={"q": "angio", "section": "procedures"},
            )

    assert resp.status_code == 200
    payload = resp.json()
    assert payload["query"] == "angio"


@pytest.mark.anyio
async def test_context_file_endpoint_invalid_section_returns_400(app):
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            "/api/note/complete/context/file",
            data={"q": "diab", "section": "INVALID"},
        )

    assert resp.status_code == 400


@pytest.mark.anyio
async def test_context_file_endpoint_with_json_file(app):
    mock_docs = [_make_doc(term="Fracture")]
    ctx = {"conditions": [{"term": "Fracture", "status": "resolved"}]}
    with patch("backend.api.router.note_complete", new_callable=AsyncMock) as mock_nc:
        mock_nc.return_value = (mock_docs, 1, False)
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.post(
                "/api/note/complete/context/file",
                data={"q": "frac", "section": "diagnosis"},
                files={"patient_context_file": ("context.json", json.dumps(ctx).encode(), "application/json")},
            )

    assert resp.status_code == 200


@pytest.mark.anyio
async def test_context_file_endpoint_with_invalid_json_file_returns_400(app):
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            "/api/note/complete/context/file",
            data={"q": "frac", "section": "diagnosis"},
            files={"patient_context_file": ("context.json", b"not valid json", "application/json")},
        )

    assert resp.status_code == 400


@pytest.mark.anyio
async def test_context_file_endpoint_with_text_file(app):
    mock_docs = [_make_doc(term="Naproxen")]
    with patch("backend.api.router.note_complete", new_callable=AsyncMock) as mock_nc:
        mock_nc.return_value = (mock_docs, 1, False)
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.post(
                "/api/note/complete/context/file",
                data={"q": "napro", "section": "medications"},
                files={"patient_context_file": ("notes.txt", b"Patient takes naproxen daily.", "text/plain")},
            )

    assert resp.status_code == 200


# ─────────────────────────────────────────────────────────────────────────────
# models.py — Pydantic validation
# ─────────────────────────────────────────────────────────────────────────────

class TestNoteCompleteRequest:
    def test_valid_request(self):
        from backend.models.models import NoteCompleteRequest
        req = NoteCompleteRequest(q="diab", section="diagnosis")
        assert req.q == "diab"
        assert req.section == "diagnosis"

    def test_invalid_section_raises(self):
        from backend.models.models import NoteCompleteRequest
        from pydantic import ValidationError
        with pytest.raises(ValidationError):
            NoteCompleteRequest(q="diab", section="invalid_section")

    def test_empty_q_raises(self):
        from backend.models.models import NoteCompleteRequest
        from pydantic import ValidationError
        with pytest.raises(ValidationError):
            NoteCompleteRequest(q="", section="diagnosis")

    def test_rows_defaults(self):
        from backend.models.models import NoteCompleteRequest
        from backend.core.config import NOTE_API_DEFAULT_ROWS
        req = NoteCompleteRequest(q="diab", section="diagnosis")
        assert req.rows == NOTE_API_DEFAULT_ROWS

    def test_rows_too_large_raises(self):
        from backend.models.models import NoteCompleteRequest
        from backend.core.config import NOTE_API_MAX_ROWS
        from pydantic import ValidationError
        with pytest.raises(ValidationError):
            NoteCompleteRequest(q="diab", section="diagnosis", rows=NOTE_API_MAX_ROWS + 1)

    def test_rows_zero_raises(self):
        from backend.models.models import NoteCompleteRequest
        from pydantic import ValidationError
        with pytest.raises(ValidationError):
            NoteCompleteRequest(q="diab", section="diagnosis", rows=0)


class TestNoteCompleteResult:
    def test_valid_result(self):
        from backend.models.models import NoteCompleteResult
        result = NoteCompleteResult(
            term="Diabetes Mellitus",
            semantic_type="Disease or Syndrome",
            source="SNOMEDCT_US",
            tty="PT",
            concept_id="C0011849",
            code="73211009",
            tty_priority=1,
            source_priority=1,
        )
        assert result.term == "Diabetes Mellitus"
        assert result.tty_priority == 1
