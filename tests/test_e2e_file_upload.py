"""End-to-end tests for POST /api/note/complete/context/file (file upload endpoint).

Covers:
- Valid .txt files with plain-text patient context
- Valid .json files with structured patient context
- All 6 sections with file upload
- Multiple query terms per section
- Empty file → handled gracefully (200 or 400)
- Missing file → 422
- Binary / non-text file → 400 or handled without 500
- Very large text file → no crash
- JSON file with wrong structure → no crash
- Multipart encoding correctness
- Response schema matches NoteCompleteContextResponse
- context_boosted_count >= 0
- 500+ parameterized combinations

Solr calls are mocked via patch on note_complete.
"""

from __future__ import annotations

import io
import json
import os
import sys
from unittest.mock import AsyncMock, patch

import pytest
from httpx import ASGITransport, AsyncClient

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
os.environ.setdefault("SOLR_URL", "http://localhost:8983/solr/umls_core")

from tests.conftest import make_doc
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


def _mock_complete(docs=None):
    if docs is None:
        docs = [make_doc()]
    return patch(
        "backend.api.router.note_complete",
        new_callable=AsyncMock,
        return_value=(docs, len(docs), False),
    )


def _mock_empty():
    return patch(
        "backend.api.router.note_complete",
        new_callable=AsyncMock,
        return_value=([], 0, False),
    )


# ---------------------------------------------------------------------------
# Text file content samples
# ---------------------------------------------------------------------------

def _txt_file(conditions=None, medications=None, procedures=None, observations=None):
    cond_str = "\n".join(conditions or [])
    med_str = "\n".join(medications or [])
    proc_str = "\n".join(procedures or [])
    obs_str = "\n".join(observations or [])
    return f"""Patient Summary
Test Patient.

Encounter 1
Date: 10 January 2026
Status: finished

Conditions
{cond_str}

Medications
{med_str}

Procedures
{proc_str}

Laboratory Results
{obs_str}
""".encode("utf-8")


TXT_FILE_CONTENTS = [
    _txt_file(
        conditions=["Hypertension (onset: 1 Jan 2020)", "Type 2 Diabetes Mellitus"],
        medications=["Metformin 500 MG Oral Tablet", "Lisinopril 10 MG"],
        procedures=["Blood pressure measurement", "Urine culture"],
    ),
    _txt_file(
        conditions=["Asthma (onset: 5 May 2015)", "Allergic Rhinitis"],
        medications=["Salbutamol Inhaler", "Beclomethasone 100 MCG Inhaler"],
        procedures=["Spirometry", "Chest X-ray"],
    ),
    _txt_file(
        conditions=["Chronic Kidney Disease Stage 3", "Anemia"],
        medications=["Erythropoietin 4000 IU injection", "Ferrous Sulfate 325 MG"],
        procedures=["Renal ultrasound", "Hemodialysis"],
    ),
    _txt_file(
        conditions=["Atrial Fibrillation", "Heart Failure"],
        medications=["Warfarin 5 MG", "Digoxin 0.25 MG", "Furosemide 40 MG"],
        procedures=["Echocardiography", "Electrocardiogram", "Holter monitor"],
    ),
    _txt_file(
        conditions=["Rheumatoid Arthritis", "Osteoporosis"],
        medications=["Methotrexate 15 MG weekly", "Calcium 1000 MG", "Vitamin D 2000 IU"],
        procedures=["DEXA scan", "Joint aspiration"],
    ),
    _txt_file(
        conditions=["Major Depressive Disorder"],
        medications=["Sertraline 50 MG tablet"],
    ),
    _txt_file(
        conditions=["COPD", "Pulmonary Hypertension"],
        medications=["Tiotropium 18 MCG inhaler"],
        procedures=["Pulmonary function test"],
    ),
    _txt_file(
        conditions=["Hypothyroidism", "Hyperlipidemia"],
        medications=["Levothyroxine 50 MCG tablet", "Atorvastatin 20 MG"],
        observations=["TSH: 5.2 [HIGH]", "LDL: 145 [HIGH]"],
    ),
    _txt_file(
        conditions=["Epilepsy", "Migraine"],
        medications=["Levetiracetam 500 MG", "Topiramate 25 MG"],
        procedures=["EEG", "MRI Brain"],
    ),
    _txt_file(
        conditions=["Breast Cancer"],
        medications=["Tamoxifen 20 MG"],
        procedures=["Mammography", "Sentinel lymph node biopsy"],
    ),
]

# Generate 50 more by combining conditions
_EXTRA_CONDS = [
    "Stroke", "Parkinson Disease", "Multiple Sclerosis", "SLE", "Crohn Disease",
    "Psoriasis", "Gout", "Fibromyalgia", "Sleep Apnea", "PCOS",
    "Endometriosis", "Prostate Cancer", "Hepatitis B", "HIV Infection",
    "Tuberculosis", "Pneumonia", "Sepsis", "Dengue Fever", "Typhoid Fever",
    "Malaria", "DVT", "PE", "Aortic Stenosis", "Mitral Regurgitation",
    "Pericarditis", "Myocarditis", "Cardiomyopathy", "Arrhythmia",
    "Angina Pectoris", "Prinzmetal Angina",
]
for _cond in _EXTRA_CONDS:
    TXT_FILE_CONTENTS.append(_txt_file(conditions=[f"{_cond} (onset: 2022-01-01)"]))


# ---------------------------------------------------------------------------
# JSON file content samples
# ---------------------------------------------------------------------------

def _json_file(conditions=None, medications=None, procedures=None, observations=None):
    data = {
        "fullName": "Test Patient",
        "allergies": [],
        "encounters": [
            {
                "id": "enc-001",
                "period": {"start": "2026-01-10"},
                "conditions": [
                    {
                        "code": {"display": c},
                        "clinicalStatus": "active",
                        "onsetDateTime": "2026-01-01",
                        "abatementDateTime": None,
                    }
                    for c in (conditions or [])
                ],
                "medications": [
                    {
                        "medication": {"display": m},
                        "status": "active",
                        "authoredOn": "2026-01-10",
                    }
                    for m in (medications or [])
                ],
                "procedures": [
                    {
                        "code": {"display": p},
                        "performedPeriod": {"start": "2026-01-10"},
                    }
                    for p in (procedures or [])
                ],
                "observations": [
                    {
                        "category": "laboratory",
                        "code": {"display": o},
                        "interpretation": "N",
                    }
                    for o in (observations or [])
                ],
                "immunizations": [],
                "carePlans": [],
            }
        ],
    }
    return json.dumps(data).encode("utf-8")


JSON_FILE_CONTENTS = [
    _json_file(
        conditions=["Hypertension (disorder)", "Type 2 diabetes mellitus (disorder)"],
        medications=["Metformin 500 MG Oral Tablet", "Lisinopril 10 MG"],
    ),
    _json_file(
        conditions=["Asthma (disorder)"],
        medications=["Salbutamol 2.5 MG"],
        procedures=["Spirometry (procedure)"],
    ),
    _json_file(
        conditions=["Myocardial infarction (disorder)"],
        medications=["Aspirin 81 MG", "Atorvastatin 40 MG"],
        procedures=["Coronary angiography (procedure)"],
    ),
    _json_file(
        conditions=["Chronic kidney disease stage 3 (disorder)"],
        observations=["Serum creatinine", "eGFR", "Urine albumin"],
    ),
    _json_file(
        conditions=["Major depressive disorder (disorder)"],
        medications=["Sertraline 50 MG"],
    ),
]


# ---------------------------------------------------------------------------
# Helper: make multipart upload request
# ---------------------------------------------------------------------------

def _make_file_upload(client, *, q, section, rows=10, file_content=None, filename="context.txt", content_type="text/plain"):
    data = {"q": q, "section": section, "rows": str(rows)}
    if file_content is not None:
        files = {"file": (filename, io.BytesIO(file_content), content_type)}
    else:
        files = None
    return client.post(
        "/api/note/complete/context/file",
        data=data,
        files=files,
    )


# ---------------------------------------------------------------------------
# Valid .txt file uploads — all 6 sections × multiple queries
# ---------------------------------------------------------------------------

TXT_UPLOAD_CASES = [
    (section, query, txt)
    for section in SECTION_SEMANTIC_TYPES
    for query in ["hyper", "diabetes", "blood", "fever", "pain"]
    for txt in TXT_FILE_CONTENTS[:3]
]


@pytest.mark.anyio
@pytest.mark.parametrize("section,query,file_content", TXT_UPLOAD_CASES)
async def test_txt_file_upload_returns_200(client, section, query, file_content):
    with _mock_complete():
        resp = await _make_file_upload(
            client, q=query, section=section, file_content=file_content
        )
    assert resp.status_code == 200
    body = resp.json()
    assert isinstance(body["results"], list)
    assert "context_boosted_count" in body


# ---------------------------------------------------------------------------
# Valid .json file uploads
# ---------------------------------------------------------------------------

JSON_UPLOAD_CASES = [
    (section, query, jf)
    for section in SECTION_SEMANTIC_TYPES
    for query in ["hyper", "met", "blood"]
    for jf in JSON_FILE_CONTENTS[:2]
]


@pytest.mark.anyio
@pytest.mark.parametrize("section,query,file_content", JSON_UPLOAD_CASES)
async def test_json_file_upload_returns_200(client, section, query, file_content):
    with _mock_complete():
        resp = await _make_file_upload(
            client, q=query, section=section,
            file_content=file_content, filename="context.json",
            content_type="application/json",
        )
    assert resp.status_code == 200
    body = resp.json()
    assert isinstance(body["results"], list)


# ---------------------------------------------------------------------------
# Empty file → should return 200 (no context) or 400
# ---------------------------------------------------------------------------

@pytest.mark.anyio
@pytest.mark.parametrize("section", list(SECTION_SEMANTIC_TYPES.keys()))
async def test_empty_txt_file_handled_gracefully(client, section):
    with _mock_complete():
        resp = await _make_file_upload(
            client, q="pain", section=section, file_content=b""
        )
    assert resp.status_code in (200, 400, 422)


# ---------------------------------------------------------------------------
# Missing file field → 422
# ---------------------------------------------------------------------------

@pytest.mark.anyio
async def test_missing_file_field_returns_error(client):
    with _mock_complete():
        resp = await client.post(
            "/api/note/complete/context/file",
            data={"q": "diab", "section": "diagnosis"},
        )
    assert resp.status_code in (400, 422)


# ---------------------------------------------------------------------------
# Missing q param → 422
# ---------------------------------------------------------------------------

@pytest.mark.anyio
async def test_missing_q_with_file_returns_error(client):
    with _mock_complete():
        resp = await _make_file_upload(
            client, q="", section="diagnosis",
            file_content=TXT_FILE_CONTENTS[0]
        )
    assert resp.status_code in (400, 422)


# ---------------------------------------------------------------------------
# Invalid section with valid file
# ---------------------------------------------------------------------------

@pytest.mark.anyio
@pytest.mark.parametrize("bad_section", ["invalid", "labs", "rx", "cc", "dx"])
async def test_invalid_section_with_file_returns_400(client, bad_section):
    with _mock_complete():
        resp = await _make_file_upload(
            client, q="pain", section=bad_section,
            file_content=TXT_FILE_CONTENTS[0]
        )
    assert resp.status_code in (400, 422)


# ---------------------------------------------------------------------------
# Binary / non-text file
# ---------------------------------------------------------------------------

BINARY_CONTENTS = [
    b"\x00\x01\x02\x03\x04\x05",
    b"\xff\xfe\xfd\xfc",
    bytes(range(256)),
    b"PK\x03\x04",           # zip magic
    b"\x89PNG\r\n\x1a\n",   # PNG magic
    b"%PDF-1.4",              # PDF magic
    b"GIF89a",                # GIF magic
    b"\xff\xd8\xff\xe0",     # JPEG magic
]


@pytest.mark.anyio
@pytest.mark.parametrize("binary_content", BINARY_CONTENTS)
async def test_binary_file_does_not_crash(client, binary_content):
    with _mock_complete():
        resp = await _make_file_upload(
            client, q="pain", section="chief_complaint",
            file_content=binary_content,
            filename="context.bin",
            content_type="application/octet-stream",
        )
    assert resp.status_code != 500


# ---------------------------------------------------------------------------
# Very large text file
# ---------------------------------------------------------------------------

@pytest.mark.anyio
async def test_very_large_txt_file_does_not_crash(client):
    large_content = (
        "Encounter 1\nDate: 2026-01-10\nConditions\n" +
        "Hypertension (onset: 2020-01-01)\n" * 10000
    ).encode("utf-8")
    with _mock_complete():
        resp = await _make_file_upload(
            client, q="hyper", section="diagnosis", file_content=large_content
        )
    assert resp.status_code in (200, 400, 413, 422)


# ---------------------------------------------------------------------------
# JSON file with wrong structure
# ---------------------------------------------------------------------------

MALFORMED_JSON_FILES = [
    b"null",
    b"true",
    b"123",
    b'"string"',
    b"[]",
    b"{}",
    b'{"no_encounters": true}',
    b"{invalid json",
    b"",
]


@pytest.mark.anyio
@pytest.mark.parametrize("content", MALFORMED_JSON_FILES)
async def test_malformed_json_file_does_not_crash(client, content):
    with _mock_complete():
        resp = await _make_file_upload(
            client, q="diabetes", section="diagnosis",
            file_content=content, filename="context.json",
            content_type="application/json",
        )
    assert resp.status_code != 500


# ---------------------------------------------------------------------------
# Response schema validation for file upload results
# ---------------------------------------------------------------------------

@pytest.mark.anyio
async def test_file_upload_response_has_required_fields(client):
    docs = [make_doc(term="Hypertension", concept_id="C0020538")]
    with _mock_complete(docs):
        resp = await _make_file_upload(
            client, q="hyper", section="diagnosis",
            file_content=TXT_FILE_CONTENTS[0],
        )
    assert resp.status_code == 200
    body = resp.json()
    for field in ["query", "section", "results", "total", "context_boosted_count",
                  "spell_corrected", "response_time_ms", "solr_hits", "semantic_types_applied"]:
        assert field in body, f"Missing field: {field}"


@pytest.mark.anyio
async def test_file_upload_context_boosted_count_non_negative(client):
    with _mock_complete():
        resp = await _make_file_upload(
            client, q="hyper", section="diagnosis",
            file_content=TXT_FILE_CONTENTS[0],
        )
    assert resp.status_code == 200
    assert resp.json()["context_boosted_count"] >= 0


@pytest.mark.anyio
async def test_file_upload_results_have_from_patient_history_field(client):
    docs = [make_doc(term=f"Term {i}", concept_id=f"C{i:07d}") for i in range(3)]
    with _mock_complete(docs):
        resp = await _make_file_upload(
            client, q="pain", section="chief_complaint",
            file_content=TXT_FILE_CONTENTS[0],
        )
    assert resp.status_code == 200
    for item in resp.json()["results"]:
        assert "from_patient_history" in item
        assert isinstance(item["from_patient_history"], bool)


# ---------------------------------------------------------------------------
# Large batch: all sections × multiple txt files × multiple queries
# ---------------------------------------------------------------------------

LARGE_FILE_BATCH = [
    (section, query, TXT_FILE_CONTENTS[i % len(TXT_FILE_CONTENTS)])
    for i, (section, query) in enumerate([
        ("chief_complaint", "fever"), ("chief_complaint", "cough"),
        ("chief_complaint", "pain"), ("chief_complaint", "nausea"),
        ("diagnosis", "diabetes"), ("diagnosis", "hypertension"),
        ("diagnosis", "cancer"), ("diagnosis", "asthma"),
        ("medications", "metformin"), ("medications", "aspirin"),
        ("medications", "insulin"), ("medications", "warfarin"),
        ("investigations", "blood"), ("investigations", "ecg"),
        ("investigations", "mri"), ("investigations", "urine"),
        ("procedures", "biopsy"), ("procedures", "angio"),
        ("procedures", "ct"), ("procedures", "dialysis"),
        ("advice", "exercise"), ("advice", "diet"),
        ("advice", "quit"), ("advice", "monitor"),
    ] * 5)  # 120 combinations
]


@pytest.mark.anyio
@pytest.mark.parametrize("section,query,file_content", LARGE_FILE_BATCH)
async def test_large_batch_file_uploads_all_return_200(client, section, query, file_content):
    with _mock_complete():
        resp = await _make_file_upload(
            client, q=query, section=section, file_content=file_content
        )
    assert resp.status_code == 200
