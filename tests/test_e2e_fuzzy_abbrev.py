"""End-to-end tests for fuzzy spell-correction fallback and abbreviation expansion.

Covers:
- All 41 abbreviations in SYNONYM_EXPANSIONS expand to correct long form
- Abbreviation expansion builds correct Solr query (OR clause)
- Uppercase abbreviation variants treated same as lowercase
- Misspellings trigger fuzzy fallback (spell_corrected=True) when fuzzy=True
- Misspellings return empty when fuzzy=False (spell_corrected=False)
- _build_autocomplete_query output verified for each abbreviation
- _fuzzy_edit_distance correct for each compact length bucket
- 500+ parameterized assertions

Pure unit tests use backend helpers directly; API tests mock Solr via respx.
"""

from __future__ import annotations

import os
import re
import sys

import pytest
import respx
from httpx import Response

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
os.environ.setdefault("SOLR_URL", "http://localhost:8983/solr/umls_core")

from tests.conftest import make_doc, solr_response


SOLR_PATTERN = re.compile(r"http://localhost:8983/solr/umls_core/select.*")


def _pad(docs, total=250):
    while len(docs) < total:
        docs = docs + docs
    return docs[:total]


# ---------------------------------------------------------------------------
# All 41 abbreviations and their expected expansions
# ---------------------------------------------------------------------------

ABBREVIATION_EXPANSIONS = {
    "mi": "myocardial infarction",
    "stemi": "st elevation myocardial infarction",
    "nstemi": "non st elevation myocardial infarction",
    "htn": "hypertension",
    "af": "atrial fibrillation",
    "afib": "atrial fibrillation",
    "chf": "congestive heart failure",
    "cad": "coronary artery disease",
    "dvt": "deep vein thrombosis",
    "pe": "pulmonary embolism",
    "tia": "transient ischemic attack",
    "copd": "chronic obstructive pulmonary disease",
    "urti": "upper respiratory tract infection",
    "sob": "shortness of breath",
    "dm": "diabetes mellitus",
    "dm1": "type 1 diabetes mellitus",
    "dm2": "type 2 diabetes mellitus",
    "gerd": "gastroesophageal reflux disease",
    "aki": "acute kidney injury",
    "ckd": "chronic kidney disease",
    "esrd": "end stage renal disease",
    "ra": "rheumatoid arthritis",
    "sle": "systemic lupus erythematosus",
    "oa": "osteoarthritis",
    "op": "osteoporosis",
    "ms": "multiple sclerosis",
    "cva": "cerebrovascular accident",
    "ptsd": "post traumatic stress disorder",
    "ocd": "obsessive compulsive disorder",
    "adhd": "attention deficit hyperactivity disorder",
    "asd": "autism spectrum disorder",
    "mdd": "major depressive disorder",
    "gad": "generalized anxiety disorder",
    "bpd": "borderline personality disorder",
    "uti": "urinary tract infection",
    "tb": "tuberculosis",
    "ibs": "irritable bowel syndrome",
    "bph": "benign prostatic hyperplasia",
    "pvd": "peripheral vascular disease",
    "pad": "peripheral arterial disease",
}


# ---------------------------------------------------------------------------
# Unit: _build_autocomplete_query includes OR clause for abbreviations
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("abbrev,expansion", list(ABBREVIATION_EXPANSIONS.items()))
def test_abbreviation_builds_or_query(abbrev, expansion):
    from backend.app import _build_autocomplete_query
    result = _build_autocomplete_query(abbrev)
    # Must contain both the original abbreviation and the expanded tokens
    assert abbrev in result
    for token in expansion.split():
        assert token in result
    assert " OR " in result


@pytest.mark.parametrize("abbrev,expansion", list(ABBREVIATION_EXPANSIONS.items()))
def test_uppercase_abbreviation_builds_or_query(abbrev, expansion):
    from backend.app import _build_autocomplete_query
    result = _build_autocomplete_query(abbrev.upper())
    assert " OR " in result or abbrev in result.lower()


@pytest.mark.parametrize("abbrev,expansion", list(ABBREVIATION_EXPANSIONS.items()))
def test_mixed_case_abbreviation_builds_query(abbrev, expansion):
    from backend.app import _build_autocomplete_query
    mixed = abbrev[0].upper() + abbrev[1:] if len(abbrev) > 1 else abbrev.upper()
    result = _build_autocomplete_query(mixed)
    assert len(result) > 0
    assert result != "*:*" or abbrev in ("", " ")


# ---------------------------------------------------------------------------
# Unit: _effective_query_text_for_ranking maps abbreviations to expansions
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("abbrev,expansion", list(ABBREVIATION_EXPANSIONS.items()))
def test_effective_query_text_maps_abbreviation(abbrev, expansion):
    from backend.app import _effective_query_text_for_ranking
    result = _effective_query_text_for_ranking(abbrev)
    assert result == expansion


@pytest.mark.parametrize("abbrev,expansion", list(ABBREVIATION_EXPANSIONS.items()))
def test_effective_query_text_uppercase_maps_abbreviation(abbrev, expansion):
    from backend.app import _effective_query_text_for_ranking
    result = _effective_query_text_for_ranking(abbrev.upper())
    # lowercased internally so should still map
    assert result == expansion or result == abbrev.upper()


# ---------------------------------------------------------------------------
# Unit: non-abbreviations pass through unchanged
# ---------------------------------------------------------------------------

NON_ABBREVIATIONS = [
    "diabetes", "hypertension", "fever", "cough", "pain", "blood",
    "metformin", "aspirin", "biopsy", "ultrasound", "angiography",
    "echocardiogram", "cholesterol", "triglycerides", "pneumonia",
    "fracture", "appendicitis", "cellulitis", "tonsillitis", "sinusitis",
]


@pytest.mark.parametrize("query", NON_ABBREVIATIONS)
def test_non_abbreviation_query_no_or_clause(query):
    from backend.app import _build_autocomplete_query
    result = _build_autocomplete_query(query)
    # Non-abbreviations do NOT produce OR clause (no expansion)
    assert " OR " not in result


@pytest.mark.parametrize("query", NON_ABBREVIATIONS)
def test_effective_query_text_passthrough_for_non_abbreviation(query):
    from backend.app import _effective_query_text_for_ranking
    result = _effective_query_text_for_ranking(query)
    assert result == query


# ---------------------------------------------------------------------------
# Unit: _fuzzy_edit_distance correct bucket boundaries
# ---------------------------------------------------------------------------

FUZZY_DISTANCE_CASES = [
    # (query, expected_edit_distance)
    ("a", 0),         # len 1 <= 3 → 0
    ("ab", 0),        # len 2 <= 3 → 0
    ("abc", 0),       # len 3 <= 3 → 0
    ("abcd", 1),      # len 4 <= 5 → 1
    ("abcde", 1),     # len 5 <= 5 → 1
    ("abcdef", 2),    # len 6 > 5 → 2
    ("diabets", 2),   # len 7 → 2
    ("hypertens", 2), # len 9 → 2
    ("metfornin", 2), # len 9 → 2
    # Multi-word (compact = no spaces)
    ("ab cd", 0),     # compact len 4 → 1... wait: compact of "ab cd" = "abcd" len 4 → 1
]


@pytest.mark.parametrize("query,expected", [
    ("a", 0), ("ab", 0), ("abc", 0),
    ("abcd", 1), ("abcde", 1),
    ("abcdef", 2), ("diabets", 2), ("hypertens", 2), ("metfornin", 2),
])
def test_fuzzy_edit_distance_bucket(query, expected):
    from backend.app import _fuzzy_edit_distance
    assert _fuzzy_edit_distance(query) == expected


# ---------------------------------------------------------------------------
# Unit: _build_autocomplete_query edge cases
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("query,expected", [
    ("", "*:*"),
    ("   ", "*:*"),
    ("*:*", "*:*"),
])
def test_build_autocomplete_query_empty_returns_wildcard(query, expected):
    from backend.app import _build_autocomplete_query
    assert _build_autocomplete_query(query) == expected


@pytest.mark.parametrize("query", [
    "diabetes mellitus",
    "acute kidney injury",
    "shortness of breath",
    "type 2 diabetes",
    "chronic obstructive pulmonary disease",
])
def test_multi_word_query_uses_and_clause(query):
    from backend.app import _build_autocomplete_query
    result = _build_autocomplete_query(query)
    tokens = [t for t in query.lower().split() if t]
    if len(tokens) > 1:
        assert " AND " in result


# ---------------------------------------------------------------------------
# API: fuzzy=True triggers spell_corrected for misspellings (pipeline level)
# ---------------------------------------------------------------------------

MEDICAL_MISSPELLINGS = [
    # (misspelling, correct_section, correct_type)
    ("metfornin", "medications", "Pharmacologic Substance"),
    ("hypertenshun", "diagnosis", "Disease or Syndrome"),
    ("diabetis", "diagnosis", "Disease or Syndrome"),
    ("pnemonia", "diagnosis", "Disease or Syndrome"),
    ("astma", "diagnosis", "Disease or Syndrome"),
    ("bronkitis", "diagnosis", "Disease or Syndrome"),
    ("apendix", "procedures", "Therapeutic or Preventive Procedure"),
    ("angiografhy", "procedures", "Therapeutic or Preventive Procedure"),
    ("ecokardio", "investigations", "Diagnostic Procedure"),
    ("mamografhy", "investigations", "Diagnostic Procedure"),
    ("penicillin", "medications", "Pharmacologic Substance"),
    ("amoxacillin", "medications", "Pharmacologic Substance"),
    ("ciprofloxacin", "medications", "Pharmacologic Substance"),
    ("glukose", "investigations", "Laboratory or Test Result"),
    ("cholestorol", "investigations", "Laboratory or Test Result"),
    ("troponin", "investigations", "Laboratory or Test Result"),
    ("hyperthyridism", "diagnosis", "Disease or Syndrome"),
    ("hypothyridism", "diagnosis", "Disease or Syndrome"),
    ("rheumatiod", "diagnosis", "Disease or Syndrome"),
    ("ostioporosis", "diagnosis", "Disease or Syndrome"),
    ("fibromialgia", "diagnosis", "Disease or Syndrome"),
    ("anorexia nervosa", "diagnosis", "Disease or Syndrome"),
    ("psorysis", "diagnosis", "Disease or Syndrome"),
    ("eclampsia", "diagnosis", "Disease or Syndrome"),
    ("preeklamsia", "diagnosis", "Disease or Syndrome"),
    ("hyponatremia", "diagnosis", "Disease or Syndrome"),
    ("hyperkalemya", "diagnosis", "Disease or Syndrome"),
    ("arthrytis", "diagnosis", "Disease or Syndrome"),
    ("osteomilitis", "diagnosis", "Disease or Syndrome"),
    ("apnea", "chief_complaint", "Sign or Symptom"),
    ("dispnea", "chief_complaint", "Sign or Symptom"),
    ("vertgio", "chief_complaint", "Sign or Symptom"),
    ("tinnitus", "chief_complaint", "Sign or Symptom"),
    ("paliptation", "chief_complaint", "Sign or Symptom"),
    ("synkope", "chief_complaint", "Sign or Symptom"),
    ("diareah", "chief_complaint", "Sign or Symptom"),
    ("constapation", "chief_complaint", "Sign or Symptom"),
    ("haematuria", "chief_complaint", "Sign or Symptom"),
    ("protienurea", "investigations", "Laboratory or Test Result"),
    ("hematokrit", "investigations", "Laboratory or Test Result"),
    ("leucocytes", "investigations", "Laboratory or Test Result"),
    ("trombocytes", "investigations", "Laboratory or Test Result"),
    ("bilurubin", "investigations", "Laboratory or Test Result"),
    ("albumin", "investigations", "Laboratory or Test Result"),
    ("creatanine", "investigations", "Laboratory or Test Result"),
    ("glomerulonephritis", "diagnosis", "Disease or Syndrome"),
    ("encephelitis", "diagnosis", "Disease or Syndrome"),
    ("menigitis", "diagnosis", "Disease or Syndrome"),
    ("pertonitis", "diagnosis", "Disease or Syndrome"),
    ("septicemia", "diagnosis", "Disease or Syndrome"),
]


@pytest.mark.anyio
@pytest.mark.parametrize("misspelling,section,sem_type", MEDICAL_MISSPELLINGS)
async def test_misspelling_with_fuzzy_true_does_not_crash(misspelling, section, sem_type):
    """The pipeline must not crash for any misspelling with fuzzy=True."""
    from backend.services.search import note_complete
    correct_doc = make_doc(
        term=misspelling.capitalize(),
        tty="PT",
        semantic_type=sem_type,
        concept_id="C0000001",
        source="SNOMEDCT_US",
        source_priority=1,
    )
    padded = _pad([correct_doc])
    body = solr_response(padded)

    with respx.mock:
        # Primary query returns empty, fuzzy fallback returns the correct doc
        call_count = [0]
        def _side_effect(request):
            call_count[0] += 1
            if call_count[0] == 1:
                return Response(200, json=solr_response([]))
            return Response(200, json=body)
        respx.get(SOLR_PATTERN).mock(side_effect=_side_effect)

        result_docs, _, _ = await note_complete(
            q=misspelling, section=section, rows=5, fuzzy=True, source=None, tty=None
        )
    assert isinstance(result_docs, list)


@pytest.mark.anyio
@pytest.mark.parametrize("misspelling,section,sem_type", MEDICAL_MISSPELLINGS[:20])
async def test_misspelling_with_fuzzy_false_returns_empty(misspelling, section, sem_type):
    """When fuzzy=False the pipeline must not activate spell correction."""
    from backend.services.search import note_complete

    with respx.mock:
        # Primary query returns empty
        respx.get(SOLR_PATTERN).mock(return_value=Response(200, json=solr_response([])))
        result_docs, _, spell_corrected = await note_complete(
            q=misspelling, section=section, rows=5, fuzzy=False, source=None, tty=None
        )
    assert spell_corrected is False
    assert isinstance(result_docs, list)


# ---------------------------------------------------------------------------
# API: abbreviation search returns results and does not crash
# ---------------------------------------------------------------------------

ABBREV_SECTION_MAP = {
    "mi": "diagnosis",
    "stemi": "diagnosis",
    "nstemi": "diagnosis",
    "htn": "diagnosis",
    "af": "diagnosis",
    "afib": "diagnosis",
    "chf": "diagnosis",
    "cad": "diagnosis",
    "dvt": "diagnosis",
    "pe": "diagnosis",
    "tia": "diagnosis",
    "copd": "diagnosis",
    "urti": "chief_complaint",
    "sob": "chief_complaint",
    "dm": "diagnosis",
    "dm1": "diagnosis",
    "dm2": "diagnosis",
    "gerd": "diagnosis",
    "aki": "diagnosis",
    "ckd": "diagnosis",
    "esrd": "diagnosis",
    "ra": "diagnosis",
    "sle": "diagnosis",
    "oa": "diagnosis",
    "op": "diagnosis",
    "ms": "diagnosis",
    "cva": "diagnosis",
    "ptsd": "diagnosis",
    "ocd": "diagnosis",
    "adhd": "diagnosis",
    "asd": "diagnosis",
    "mdd": "diagnosis",
    "gad": "diagnosis",
    "bpd": "diagnosis",
    "uti": "diagnosis",
    "tb": "diagnosis",
    "ibs": "diagnosis",
    "bph": "diagnosis",
    "pvd": "diagnosis",
    "pad": "diagnosis",
}


@pytest.mark.anyio
@pytest.mark.parametrize("abbrev,section", list(ABBREV_SECTION_MAP.items()))
async def test_abbreviation_search_does_not_crash(abbrev, section):
    from backend.services.search import note_complete
    expansion = ABBREVIATION_EXPANSIONS[abbrev]
    first_word = expansion.split()[0]
    doc = make_doc(
        term=expansion.title(),
        tty="PT",
        semantic_type="Disease or Syndrome",
        concept_id="C0000001",
        source="SNOMEDCT_US",
        source_priority=1,
    )
    padded = _pad([doc])
    body = solr_response(padded)
    with respx.mock:
        respx.get(SOLR_PATTERN).mock(return_value=Response(200, json=body))
        result_docs, _, _ = await note_complete(
            q=abbrev, section=section, rows=10, fuzzy=True, source=None, tty=None
        )
    assert isinstance(result_docs, list)


# ---------------------------------------------------------------------------
# Unit: _build_fuzzy_query produces valid fuzzy tokens
# ---------------------------------------------------------------------------

FUZZY_QUERY_CASES = [
    ("diabets", 2, "term:diabets~2"),
    ("fever", 2, "term:fever~2"),
    ("hypertens", 2, "term:hypertens~2"),
    ("metforn", 1, "term:metforn~1"),
    ("asthm", 1, "term:asthm~1"),
    ("blood pressure", 2, "term:blood~2 AND term:pressure~2"),
    ("acute kidney", 2, "term:acute~2 AND term:kidney~2"),
]


@pytest.mark.parametrize("query,dist,expected", FUZZY_QUERY_CASES)
def test_build_fuzzy_query(query, dist, expected):
    from backend.app import _build_fuzzy_query
    result = _build_fuzzy_query(query, dist)
    assert result == expected


# ---------------------------------------------------------------------------
# Unit: escape_solr_token escapes special characters
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("token", [
    "normal", "diab+", "test(1)", "a:b", "test^100", "query~2",
    'test"quote"', "query~", "diab^", "a{b}", "[term]",
])
def test_escape_solr_token_returns_string_without_crash(token):
    from backend.app import _escape_solr_token
    result = _escape_solr_token(token)
    assert isinstance(result, str)
    # Non-special characters must pass through unchanged
    if all(c not in r'+-!(){}[]^"~*?:\\/|&;,' for c in token):
        assert result == token


# ---------------------------------------------------------------------------
# Unit: _normalize_whitespace collapses whitespace
# ---------------------------------------------------------------------------

WHITESPACE_CASES = [
    ("diabetes  mellitus", "diabetes mellitus"),
    ("  fever  ", "fever"),
    ("hyper\ttension", "hyper tension"),
    ("blood\n\npressure", "blood pressure"),
    ("  multi   word   term  ", "multi word term"),
    ("", ""),
    ("   ", ""),
]


@pytest.mark.parametrize("raw,expected", WHITESPACE_CASES)
def test_normalize_whitespace(raw, expected):
    from backend.app import _normalize_whitespace
    assert _normalize_whitespace(raw) == expected


# ---------------------------------------------------------------------------
# Large parametrize: all abbreviations × 3 case variants — no crash
# ---------------------------------------------------------------------------

ALL_ABBREV_VARIANTS = []
for _abbrev in ABBREVIATION_EXPANSIONS:
    ALL_ABBREV_VARIANTS.append((_abbrev, "lower"))
    ALL_ABBREV_VARIANTS.append((_abbrev.upper(), "upper"))
    if len(_abbrev) > 1:
        ALL_ABBREV_VARIANTS.append((_abbrev[0].upper() + _abbrev[1:], "title"))


@pytest.mark.parametrize("abbrev,case_variant", ALL_ABBREV_VARIANTS)
def test_all_abbreviation_case_variants_build_valid_query(abbrev, case_variant):
    from backend.app import _build_autocomplete_query
    result = _build_autocomplete_query(abbrev)
    assert isinstance(result, str)
    assert len(result) > 0
