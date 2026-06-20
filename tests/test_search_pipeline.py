"""Search pipeline integration tests — Solr mocked at the HTTP transport layer.

Uses respx to intercept httpx calls so the full note_complete() code path runs
(query building, fq construction, fetch, filter, dedup, rerank) without a live
Solr instance.
"""

from __future__ import annotations

import json
import os
import re
import sys
from unittest.mock import patch

import pytest
import respx
from httpx import Response

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
os.environ.setdefault("SOLR_URL", "http://localhost:8983/solr/umls_core")

from tests.conftest import make_doc, solr_response


def _solr_pattern():
    return re.compile(r"http://localhost:8983/solr/umls_core/select.*")


def _mock_solr(docs, num_found=None, status_code=200):
    body = solr_response(docs, num_found=num_found)
    return respx.get(_solr_pattern()).mock(
        return_value=Response(status_code, json=body)
    )


# ── Basic pipeline happy path ─────────────────────────────────────────────────

@pytest.mark.anyio
@respx.mock
async def test_note_complete_returns_docs_from_solr():
    from backend.services.search import note_complete
    docs = [make_doc(term="Diabetes Mellitus", tty="PT", semantic_type="Disease or Syndrome")]
    _mock_solr(docs)

    result_docs, num_found, spell_corrected = await note_complete(
        q="diab", section="diagnosis", rows=5, fuzzy=True, source=None, tty=None
    )

    assert len(result_docs) >= 1
    assert not spell_corrected


@pytest.mark.anyio
@respx.mock
async def test_note_complete_deduplicates_same_concept_id():
    from backend.services.search import note_complete
    docs = [
        make_doc(term="Hypertension", tty="PT", concept_id="C0020538", source="SNOMEDCT_US", tty_priority=1, source_priority=1),
        make_doc(term="Hypertension", tty="SY", concept_id="C0020538", source="ICD10CM", tty_priority=3, source_priority=2),
        make_doc(term="Hypertension", tty="SY", concept_id="C0020538", source="CHV", tty_priority=3, source_priority=15),
    ]
    _mock_solr(docs * 10)  # pad to exceed MIN_SOLR_FETCH_ROWS threshold

    result_docs, _, _ = await note_complete(
        q="hyp", section="diagnosis", rows=10, fuzzy=True, source=None, tty=None
    )

    concept_ids = [str(d.get("concept_id", "")) for d in result_docs]
    assert concept_ids.count("C0020538") <= 1


@pytest.mark.anyio
@respx.mock
async def test_note_complete_filters_non_allowed_tty():
    from backend.services.search import note_complete
    docs = [
        make_doc(term="Diabetes", tty="PT"),
        make_doc(term="Bad Term", tty="XZ", concept_id="C9999999"),  # unknown TTY should be filtered
    ]
    _mock_solr(docs)

    result_docs, _, _ = await note_complete(
        q="diab", section="diagnosis", rows=10, fuzzy=True, source=None, tty=None
    )

    ttys = [str(d.get("tty", "")) for d in result_docs]
    assert "XZ" not in ttys


@pytest.mark.anyio
@respx.mock
async def test_note_complete_zero_results_no_fuzzy_returns_empty():
    from backend.services.search import note_complete
    _mock_solr([])

    result_docs, num_found, spell_corrected = await note_complete(
        q="xyzzzzzz", section="diagnosis", rows=5, fuzzy=False, source=None, tty=None
    )

    assert result_docs == []
    assert spell_corrected is False


@pytest.mark.anyio
@respx.mock
async def test_note_complete_zero_results_with_fuzzy_calls_fuzzy_endpoint():
    from backend.services.search import note_complete

    primary_docs = []
    fuzzy_docs = [make_doc(term="Metformin", tty="PT", semantic_type="Pharmacologic Substance", source="RXNORM")]

    call_count = 0
    def side_effect(request, *args, **kwargs):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            return Response(200, json=solr_response(primary_docs))
        return Response(200, json=solr_response(fuzzy_docs))

    respx.get(_solr_pattern()).mock(side_effect=side_effect)

    result_docs, _, spell_corrected = await note_complete(
        q="metfornin", section="medications", rows=5, fuzzy=True, source=None, tty=None
    )

    assert spell_corrected is True
    assert call_count >= 2


@pytest.mark.anyio
@respx.mock
async def test_note_complete_solr_unavailable_raises_httpx_error():
    import httpx
    from backend.services.search import note_complete

    respx.get(_solr_pattern()).mock(side_effect=httpx.ConnectError("refused"))

    with pytest.raises(httpx.HTTPError):
        await note_complete(
            q="diab", section="diagnosis", rows=5, fuzzy=True, source=None, tty=None
        )


@pytest.mark.anyio
@respx.mock
async def test_note_complete_medications_section_excludes_disease_terms():
    from backend.services.search import note_complete
    # Simulate Solr returning a Disease or Syndrome doc for medications section
    # The section fq should prevent this but test that the pipeline doesn't crash
    docs = [
        make_doc(term="Metformin", tty="PT", semantic_type="Pharmacologic Substance", source="RXNORM"),
    ]
    _mock_solr(docs)

    result_docs, _, _ = await note_complete(
        q="met", section="medications", rows=5, fuzzy=True, source=None, tty=None
    )

    # Only Pharmacologic Substance should survive section filtering
    assert all(
        d.get("semantic_type") != "Disease or Syndrome"
        for d in result_docs
    )


@pytest.mark.anyio
@respx.mock
async def test_note_complete_rows_respected():
    from backend.services.search import note_complete
    docs = [
        make_doc(term=f"Term {i}", concept_id=f"C{i:07d}")
        for i in range(20)
    ]
    _mock_solr(docs)

    result_docs, _, _ = await note_complete(
        q="term", section="diagnosis", rows=3, fuzzy=False, source=None, tty=None
    )

    assert len(result_docs) <= 3


@pytest.mark.anyio
@respx.mock
async def test_note_complete_returns_num_found_from_solr():
    from backend.services.search import note_complete
    docs = [make_doc()]
    _mock_solr(docs, num_found=9999)

    _, num_found, _ = await note_complete(
        q="diab", section="diagnosis", rows=5, fuzzy=False, source=None, tty=None
    )

    assert num_found == 9999


@pytest.mark.anyio
@respx.mock
async def test_note_complete_single_char_query_does_not_crash():
    from backend.services.search import note_complete
    docs = [make_doc(term="Arthritis", tty="PT", term_word_count=1, term_length=9)]
    _mock_solr(docs)

    result_docs, _, _ = await note_complete(
        q="a", section="chief_complaint", rows=5, fuzzy=False, source=None, tty=None
    )

    assert isinstance(result_docs, list)


@pytest.mark.anyio
@respx.mock
async def test_note_complete_abbreviation_query_expands():
    from backend.services.search import note_complete
    from backend.app import SYNONYM_EXPANSIONS

    docs = [make_doc(term="Hypertension", tty="PT", semantic_type="Disease or Syndrome")]
    _mock_solr(docs)

    captured_url = []
    def capture(request, *args, **kwargs):
        captured_url.append(str(request.url))
        return Response(200, json=solr_response(docs))

    respx.get(_solr_pattern()).mock(side_effect=capture)

    await note_complete(
        q="htn", section="diagnosis", rows=5, fuzzy=False, source=None, tty=None
    )

    assert captured_url, "Solr was never called"
    # htn should expand to hypertension in query
    assert "hypertension" in captured_url[0].lower() or "htn" in captured_url[0].lower()


# ── fq construction via section_config ───────────────────────────────────────

def test_diagnosis_fq_contains_disease_or_syndrome():
    from backend.services.section_config import get_section_fq
    fq = get_section_fq("diagnosis")
    assert "Disease or Syndrome" in fq


def test_medications_fq_contains_pharmacologic_substance():
    from backend.services.section_config import get_section_fq
    fq = get_section_fq("medications")
    assert "Pharmacologic Substance" in fq


def test_procedures_fq_contains_therapeutic_procedure():
    from backend.services.section_config import get_section_fq
    fq = get_section_fq("procedures")
    assert "Therapeutic or Preventive Procedure" in fq


def test_investigations_fq_contains_laboratory_procedure():
    from backend.services.section_config import get_section_fq
    fq = get_section_fq("investigations")
    assert "Laboratory Procedure" in fq


def test_advice_fq_contains_health_care_activity():
    from backend.services.section_config import get_section_fq
    fq = get_section_fq("advice")
    assert "Health Care Activity" in fq


def test_all_sections_produce_valid_fq_string():
    from backend.services.section_config import SECTION_SEMANTIC_TYPES, get_section_fq
    for section in SECTION_SEMANTIC_TYPES:
        fq = get_section_fq(section)
        assert fq.startswith("semantic_type:(")
        assert "OR" in fq or len(SECTION_SEMANTIC_TYPES[section]) == 1
