"""Extended context parser tests — edge cases, JSON path, date parsing, boost scoring.

Covers the parse_patient_context (plain text), parse_patient_context_json,
calculate_boost_score, and find_matching_context_terms functions with cases
beyond the happy-path tests in test_context_parser.py.
"""

from __future__ import annotations

import os
import sys
from datetime import date

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
os.environ.setdefault("SOLR_URL", "http://localhost:8983/solr/umls_core")

from backend.services.context_parser import (
    calculate_boost_score,
    find_matching_context_terms,
    parse_patient_context,
    parse_patient_context_json,
)


# ── parse_patient_context: defensive input handling ───────────────────────────

def test_parser_handles_none_gracefully():
    result = parse_patient_context(None)
    assert isinstance(result, dict)
    assert result["conditions"] == []


def test_parser_handles_empty_string():
    result = parse_patient_context("")
    assert result["conditions"] == []


def test_parser_handles_whitespace_only():
    result = parse_patient_context("   \n\t  ")
    assert result["conditions"] == []


def test_parser_handles_non_string_input():
    result = parse_patient_context(12345)
    assert isinstance(result, dict)
    assert result["conditions"] == []


def test_parser_handles_no_known_headings():
    result = parse_patient_context("Just some random text without any headings.")
    assert isinstance(result, dict)
    assert result["conditions"] == []


def test_parser_result_has_all_required_keys():
    result = parse_patient_context("")
    required = {"patient_name", "conditions", "medications", "procedures", "investigations", "care_plan", "allergies", "immunizations"}
    assert required.issubset(result.keys())


# ── parse_patient_context: condition parsing ──────────────────────────────────

CONDITION_TEXT = """\
Patient Summary
Alice Test is a 40 years, female patient.

Encounter 1
Encounter Type: Office Visit
Date: 15 March 2025
Status: finished

Conditions
Hypertension (onset: 1 Jan 2020)
Type 2 Diabetes Mellitus (onset: 15 Mar 2025, resolved: 1 Jun 2025)
Fracture of bone (disorder)
"""


def test_parser_extracts_active_condition():
    result = parse_patient_context(CONDITION_TEXT)
    terms = [c["term_lower"] for c in result["conditions"]]
    assert "hypertension" in terms


def test_parser_extracts_resolved_condition_with_dates():
    result = parse_patient_context(CONDITION_TEXT)
    dm = next((c for c in result["conditions"] if "diabetes" in c["term_lower"]), None)
    assert dm is not None
    assert dm["onset"] == "2025-03-15"
    assert dm["resolved"] == "2025-06-01"
    assert dm["status"] == "resolved"


def test_parser_strips_snomed_disorder_suffix():
    result = parse_patient_context(CONDITION_TEXT)
    terms = [c["term_lower"] for c in result["conditions"]]
    # "Fracture of bone (disorder)" should become "fracture of bone"
    assert any("fracture of bone" in t for t in terms)
    assert not any("disorder" in t for t in terms)


def test_parser_active_condition_has_no_resolved_date():
    result = parse_patient_context(CONDITION_TEXT)
    hyp = next((c for c in result["conditions"] if c["term_lower"] == "hypertension"), None)
    assert hyp is not None
    assert hyp["resolved"] is None
    assert hyp["status"] == "active"


# ── parse_patient_context: medications ───────────────────────────────────────

MED_TEXT = """\
Encounter 1
Encounter Type: Office Visit
Date: 1 Jan 2026
Status: finished

Medications
Metformin 500 MG Oral Tablet -- twice daily
Lisinopril 10 MG Oral Tablet -- once daily
For: hypertension management
"""


def test_parser_extracts_medications():
    result = parse_patient_context(MED_TEXT)
    med_terms = [m["term_lower"] for m in result["medications"]]
    assert any("metformin" in t for t in med_terms)
    assert any("lisinopril" in t for t in med_terms)


def test_parser_strips_dosing_schedule_from_medication():
    result = parse_patient_context(MED_TEXT)
    for med in result["medications"]:
        assert "twice daily" not in med["term_lower"]
        assert "once daily" not in med["term_lower"]


def test_parser_excludes_for_reason_lines_from_medications():
    result = parse_patient_context(MED_TEXT)
    med_terms = [m["term_lower"] for m in result["medications"]]
    assert not any("for:" in t for t in med_terms)
    assert not any("hypertension management" in t for t in med_terms)


# ── parse_patient_context: procedures and investigations ─────────────────────

PROC_TEXT = """\
Encounter 1
Encounter Type: Visit
Date: 10 Feb 2026
Status: finished

Procedures
Plain X-ray of wrist region
Urine culture (reason: routine)
Blood pressure measurement
"""


def test_parser_classifies_xray_as_investigation():
    result = parse_patient_context(PROC_TEXT)
    inv_terms = [i["term_lower"] for i in result["investigations"]]
    assert any("x-ray" in t for t in inv_terms)


def test_parser_classifies_blood_pressure_measurement_as_investigation():
    # "measurement" is an investigation keyword, so this goes to investigations
    result = parse_patient_context(PROC_TEXT)
    inv_terms = [i["term_lower"] for i in result["investigations"]]
    assert any("blood pressure measurement" in t for t in inv_terms)


def test_parser_classifies_culture_as_investigation():
    result = parse_patient_context(PROC_TEXT)
    inv_terms = [i["term_lower"] for i in result["investigations"]]
    assert any("urine culture" in t for t in inv_terms)


# ── parse_patient_context: allergies ─────────────────────────────────────────

ALLERGY_TEXT = """\
Allergies
Penicillin (criticality: high)
Aspirin (criticality: low)
Sulfa drugs
"""


def test_parser_extracts_allergies():
    result = parse_patient_context(ALLERGY_TEXT)
    assert "Penicillin" in result["allergies"]
    assert "Aspirin" in result["allergies"]
    assert "Sulfa drugs" in result["allergies"]


def test_parser_strips_criticality_from_allergy():
    result = parse_patient_context(ALLERGY_TEXT)
    for allergy in result["allergies"]:
        assert "criticality" not in allergy.lower()


# ── parse_patient_context: encounter_count ───────────────────────────────────

MULTI_ENCOUNTER_TEXT = """\
Encounter 1
Encounter Type: Visit
Date: 1 Jan 2026
Status: finished

Conditions
Hypertension (onset: 1 Jan 2020)

Encounter 2
Encounter Type: Follow Up
Date: 15 Jan 2026
Status: finished

Conditions
Hypertension (onset: 1 Jan 2020)
"""


def test_parser_increments_encounter_count_for_same_condition():
    result = parse_patient_context(MULTI_ENCOUNTER_TEXT)
    hyp = next((c for c in result["conditions"] if c["term_lower"] == "hypertension"), None)
    assert hyp is not None
    assert hyp["encounter_count"] == 2


def test_parser_does_not_duplicate_entries_across_encounters():
    result = parse_patient_context(MULTI_ENCOUNTER_TEXT)
    hyp_entries = [c for c in result["conditions"] if c["term_lower"] == "hypertension"]
    assert len(hyp_entries) == 1


# ── parse_patient_context_json ────────────────────────────────────────────────

def test_json_parser_handles_empty_dict():
    result = parse_patient_context_json({})
    assert isinstance(result, dict)
    assert result["conditions"] == []


def test_json_parser_handles_none():
    result = parse_patient_context_json(None)
    assert isinstance(result, dict)


def test_json_parser_handles_invalid_json_string():
    result = parse_patient_context_json("not valid json")
    assert isinstance(result, dict)
    assert result["conditions"] == []


def test_json_parser_handles_list_root():
    result = parse_patient_context_json([1, 2, 3])
    assert isinstance(result, dict)
    assert result["conditions"] == []


def test_json_parser_extracts_patient_name(sample_patient_json):
    result = parse_patient_context_json(sample_patient_json)
    assert result["patient_name"] == "Jane Smith"


def test_json_parser_extracts_allergies(sample_patient_json):
    result = parse_patient_context_json(sample_patient_json)
    assert "Aspirin" in result["allergies"]


def test_json_parser_strips_snomed_suffix_from_allergy(sample_patient_json):
    result = parse_patient_context_json(sample_patient_json)
    for allergy in result["allergies"]:
        assert "(substance)" not in allergy.lower()


def test_json_parser_extracts_conditions(sample_patient_json):
    result = parse_patient_context_json(sample_patient_json)
    terms = [c["term_lower"] for c in result["conditions"]]
    assert any("pharyngitis" in t for t in terms)


def test_json_parser_extracts_medications(sample_patient_json):
    result = parse_patient_context_json(sample_patient_json)
    meds = [m["term_lower"] for m in result["medications"]]
    assert any("amoxicillin" in t for t in meds)


def test_json_parser_classifies_throat_culture_as_investigation(sample_patient_json):
    result = parse_patient_context_json(sample_patient_json)
    inv_terms = [i["term_lower"] for i in result["investigations"]]
    assert any("throat culture" in t for t in inv_terms)


def test_json_parser_extracts_lab_observation(sample_patient_json):
    result = parse_patient_context_json(sample_patient_json)
    inv_terms = [i["term_lower"] for i in result["investigations"]]
    assert any("white blood cell count" in t for t in inv_terms)


def test_json_parser_flags_high_lab_result(sample_patient_json):
    result = parse_patient_context_json(sample_patient_json)
    wbc = next(
        (i for i in result["investigations"] if "white blood cell" in i["term_lower"]),
        None,
    )
    assert wbc is not None
    assert wbc["status"] == "flagged"


def test_json_parser_extracts_immunizations(sample_patient_json):
    result = parse_patient_context_json(sample_patient_json)
    assert "Influenza vaccine" in result["immunizations"]


def test_json_parser_handles_null_fields_in_encounter():
    data = {
        "fullName": "Test Patient",
        "encounters": [
            {
                "id": "enc-1",
                "period": None,
                "conditions": None,
                "medications": None,
                "procedures": None,
                "observations": None,
                "immunizations": None,
                "carePlans": None,
            }
        ],
    }
    result = parse_patient_context_json(data)
    assert isinstance(result, dict)


def test_json_parser_handles_null_entries_in_list():
    data = {
        "encounters": [
            {
                "id": "enc-1",
                "conditions": [None, {"code": {"display": "Hypertension (disorder)"}}],
            }
        ]
    }
    result = parse_patient_context_json(data)
    assert isinstance(result, dict)


# ── calculate_boost_score ─────────────────────────────────────────────────────

def test_boost_score_zero_for_zero_encounter_count():
    entry = {"encounter_count": 0, "status": "active", "onset": None, "resolved": None}
    assert calculate_boost_score(entry, date(2026, 1, 1)) == 0.0


def test_boost_score_active_higher_than_resolved():
    today = date(2026, 2, 1)
    active = {"encounter_count": 1, "status": "active", "onset": "2025-01-01", "resolved": None}
    resolved = {"encounter_count": 1, "status": "resolved", "onset": "2025-01-01", "resolved": "2025-01-10"}
    assert calculate_boost_score(active, today) > calculate_boost_score(resolved, today)


def test_boost_score_flagged_same_as_active():
    today = date(2026, 2, 1)
    active = {"encounter_count": 1, "status": "active", "resolved": None, "onset": None}
    flagged = {"encounter_count": 1, "status": "flagged", "resolved": None, "onset": None}
    assert calculate_boost_score(active, today) == calculate_boost_score(flagged, today)


def test_boost_score_recently_resolved_higher_than_old():
    today = date(2026, 6, 1)
    recent = {"encounter_count": 1, "status": "resolved", "resolved": "2026-05-01", "onset": None}
    old = {"encounter_count": 1, "status": "resolved", "resolved": "2023-01-01", "onset": None}
    assert calculate_boost_score(recent, today) > calculate_boost_score(old, today)


def test_boost_score_higher_encounter_count_higher_score():
    today = date(2026, 2, 1)
    low = {"encounter_count": 1, "status": "active", "resolved": None, "onset": None}
    high = {"encounter_count": 5, "status": "active", "resolved": None, "onset": None}
    assert calculate_boost_score(high, today) > calculate_boost_score(low, today)


def test_boost_score_resolved_90_days_is_tier_2x():
    today = date(2026, 4, 1)
    entry = {"encounter_count": 2, "status": "resolved", "resolved": "2026-02-10", "onset": None}
    score = calculate_boost_score(entry, today)
    # Within 90 days: encounter_count * 2.0
    assert score == pytest.approx(4.0)


def test_boost_score_resolved_6_months_is_tier_1_5x():
    today = date(2026, 6, 1)
    entry = {"encounter_count": 2, "status": "resolved", "resolved": "2026-01-01", "onset": None}
    score = calculate_boost_score(entry, today)
    # 91-180 days: encounter_count * 1.5
    assert score == pytest.approx(3.0)


def test_boost_score_resolved_over_a_year_is_half_x():
    today = date(2026, 6, 1)
    entry = {"encounter_count": 2, "status": "resolved", "resolved": "2024-01-01", "onset": None}
    score = calculate_boost_score(entry, today)
    # > 365 days: encounter_count * 0.5
    assert score == pytest.approx(1.0)


def test_boost_score_handles_invalid_resolved_date():
    entry = {"encounter_count": 1, "status": "resolved", "resolved": "not-a-date", "onset": None}
    score = calculate_boost_score(entry, date(2026, 1, 1))
    assert isinstance(score, float)


# ── find_matching_context_terms ───────────────────────────────────────────────

def test_find_returns_empty_for_unknown_section():
    parsed = {"conditions": [{"term": "Hypertension", "term_lower": "hypertension", "status": "active", "encounter_count": 1}]}
    matches = find_matching_context_terms("hyp", "unknown_section", parsed, date(2026, 1, 1))
    assert matches == []


def test_find_returns_empty_for_empty_query():
    parsed = {"conditions": [{"term": "Hypertension", "term_lower": "hypertension", "status": "active", "encounter_count": 1}]}
    matches = find_matching_context_terms("", "diagnosis", parsed, date(2026, 1, 1))
    assert matches == []


def test_find_prefix_match_works():
    parsed = {
        "conditions": [
            {"term": "Hypertension", "term_lower": "hypertension", "status": "active", "encounter_count": 2, "onset": None, "resolved": None}
        ]
    }
    matches = find_matching_context_terms("hyp", "diagnosis", parsed, date(2026, 1, 1))
    assert any(m["term_lower"] == "hypertension" for m in matches)


def test_find_token_prefix_match_works():
    parsed = {
        "conditions": [
            {"term": "Acute Viral Pharyngitis", "term_lower": "acute viral pharyngitis", "status": "active", "encounter_count": 1, "onset": None, "resolved": None}
        ]
    }
    matches = find_matching_context_terms("viral", "diagnosis", parsed, date(2026, 1, 1))
    assert any("pharyngitis" in m["term_lower"] for m in matches)


def test_find_does_not_cross_section_buckets():
    parsed = {
        "conditions": [
            {"term": "Diabetes", "term_lower": "diabetes", "status": "active", "encounter_count": 1, "onset": None, "resolved": None}
        ],
        "medications": [
            {"term": "Metformin", "term_lower": "metformin", "status": "active", "encounter_count": 1, "onset": None, "resolved": None}
        ],
    }
    # Querying diagnosis section — should only return conditions, not medications
    matches = find_matching_context_terms("met", "diagnosis", parsed, date(2026, 1, 1))
    assert all("metformin" not in m["term_lower"] for m in matches)


def test_find_returns_matches_sorted_by_boost_score_desc():
    today = date(2026, 2, 1)
    parsed = {
        "conditions": [
            {"term": "Old Disease", "term_lower": "old disease", "status": "resolved",
             "resolved": "2023-01-01", "encounter_count": 1, "onset": None},
            {"term": "Active Disease", "term_lower": "active disease", "status": "active",
             "resolved": None, "encounter_count": 3, "onset": None},
        ]
    }
    matches = find_matching_context_terms("dis", "diagnosis", parsed, today)
    if len(matches) >= 2:
        scores = [m["boost_score"] for m in matches]
        assert scores == sorted(scores, reverse=True)


def test_find_each_match_has_boost_score():
    parsed = {
        "conditions": [
            {"term": "Fever", "term_lower": "fever", "status": "active", "encounter_count": 1, "onset": None, "resolved": None}
        ]
    }
    matches = find_matching_context_terms("fev", "chief_complaint", parsed, date(2026, 1, 1))
    for m in matches:
        assert "boost_score" in m
        assert isinstance(m["boost_score"], float)


def test_find_medications_section_queries_medications_bucket():
    parsed = {
        "medications": [
            {"term": "Metformin", "term_lower": "metformin", "status": "active", "encounter_count": 2, "onset": None, "resolved": None}
        ]
    }
    matches = find_matching_context_terms("met", "medications", parsed, date(2026, 1, 1))
    assert any("metformin" in m["term_lower"] for m in matches)


def test_find_investigations_section_queries_investigations_bucket():
    parsed = {
        "investigations": [
            {"term": "Blood glucose test", "term_lower": "blood glucose test", "status": "active", "encounter_count": 1, "onset": None, "resolved": None}
        ]
    }
    matches = find_matching_context_terms("blood", "investigations", parsed, date(2026, 1, 1))
    assert any("blood glucose" in m["term_lower"] for m in matches)


def test_find_advice_section_queries_conditions_and_care_plan():
    parsed = {
        "conditions": [
            {"term": "Hypertension", "term_lower": "hypertension", "status": "active", "encounter_count": 1, "onset": None, "resolved": None}
        ],
        "care_plan": [
            {"term": "Low-salt diet", "term_lower": "low-salt diet", "status": "active", "encounter_count": 1, "onset": None, "resolved": None}
        ],
    }
    condition_matches = find_matching_context_terms("hyp", "advice", parsed, date(2026, 1, 1))
    care_plan_matches = find_matching_context_terms("low", "advice", parsed, date(2026, 1, 1))
    assert len(condition_matches) > 0
    assert len(care_plan_matches) > 0
