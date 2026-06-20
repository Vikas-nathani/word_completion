"""Shared pytest fixtures for the autocompleter test suite."""

from __future__ import annotations

import os
import sys

import pytest
from httpx import ASGITransport, AsyncClient

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
os.environ.setdefault("SOLR_URL", "http://localhost:8983/solr/umls_core")


# ── Shared document factory ───────────────────────────────────────────────────

def make_doc(
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
    stn_path=None,
    parent_stn=None,
    parent_stn_id=None,
    depth_level=None,
):
    doc = {
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
    if stn_path is not None:
        doc["stn_path"] = stn_path
    if parent_stn is not None:
        doc["parent_stn"] = parent_stn
    if parent_stn_id is not None:
        doc["parent_stn_id"] = parent_stn_id
    if depth_level is not None:
        doc["depth_level"] = depth_level
    return doc


def solr_response(docs: list[dict], num_found: int | None = None) -> dict:
    return {
        "response": {
            "numFound": num_found if num_found is not None else len(docs),
            "start": 0,
            "docs": docs,
        }
    }


# ── FastAPI app fixture ───────────────────────────────────────────────────────

@pytest.fixture(scope="session")
def fastapi_app():
    from backend.app import app
    return app


@pytest.fixture
async def async_client(fastapi_app):
    transport = ASGITransport(app=fastapi_app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        yield client


# ── Common patient context fixtures ──────────────────────────────────────────

@pytest.fixture
def sample_patient_text():
    return """\
Patient Summary
John Doe is a 55 years 2 months, male patient. Active conditions: Hypertension, Type 2 Diabetes Mellitus and 1 more. Resolved conditions: Stress. Recent medications: Metformin 500 MG Oral Tablet, Lisinopril 10 MG Oral Tablet.

Patient Information
Name: John Doe
Gender: male
Date of Birth: 1 Jan 1969

Allergies
Penicillin (criticality: high)

Encounter 1
Encounter Type: Office Visit
Date: 10 January 2026
Status: finished

Conditions
Hypertension (onset: 1 Jan 2020)
Type 2 Diabetes Mellitus (onset: 1 Mar 2018)

Medications
Metformin 500 MG Oral Tablet -- twice daily
Lisinopril 10 MG Oral Tablet -- once daily

Procedures
Blood pressure measurement
Urine culture (reason: routine check)
"""


@pytest.fixture
def sample_patient_json():
    return {
        "fullName": "Jane Smith",
        "allergies": [
            {"code": {"display": "Aspirin (substance)"}}
        ],
        "encounters": [
            {
                "id": "enc-001",
                "period": {"start": "2026-01-15"},
                "conditions": [
                    {
                        "code": {"display": "Acute viral pharyngitis (disorder)"},
                        "clinicalStatus": "active",
                        "onsetDateTime": "2026-01-15",
                        "abatementDateTime": None,
                    }
                ],
                "medications": [
                    {
                        "medication": {"display": "Amoxicillin 500 MG Oral Capsule"},
                        "status": "active",
                        "authoredOn": "2026-01-15",
                    }
                ],
                "procedures": [
                    {
                        "code": {"display": "Throat culture (procedure)"},
                        "performedPeriod": {"start": "2026-01-15"},
                    }
                ],
                "observations": [
                    {
                        "category": "laboratory",
                        "code": {"display": "White blood cell count"},
                        "interpretation": "H",
                    }
                ],
                "immunizations": [
                    {"vaccineCode": {"display": "Influenza vaccine"}}
                ],
                "carePlans": [],
            }
        ],
    }
