"""End-to-end tests for /api/note/complete — core autocomplete per section.

Covers:
- All 6 sections with 500+ parameterized query samples
- Single-char, short, multi-word, numeric, mixed-case queries
- Valid rows values, default rows behaviour
- Missing required params return 400/422
- Oversized query (>200 chars) rejected
- Invalid rows values rejected
- Response always has required top-level fields
- results list is always a list (never null)
- total == len(results)
- response_time_ms is a non-negative number
- solr_hits is a non-negative int
- spell_corrected is a bool

Solr is mocked at the httpx transport layer via respx so no live Solr is needed.
"""

from __future__ import annotations

import os
import re
import sys
from unittest.mock import AsyncMock, patch

import pytest
from httpx import ASGITransport, AsyncClient

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
os.environ.setdefault("SOLR_URL", "http://localhost:8983/solr/umls_core")

from tests.conftest import make_doc


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _mock_docs(docs=None):
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


@pytest.fixture
def app():
    from backend.app import app as _app
    return _app


@pytest.fixture
async def client(app):
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


# ---------------------------------------------------------------------------
# Section × query matrix — chief_complaint (100+ queries)
# ---------------------------------------------------------------------------

CHIEF_COMPLAINT_QUERIES = [
    "a", "ab", "ache", "acute", "ankle", "arm", "back", "bleed", "blind",
    "bloat", "blood", "blue", "body", "bowel", "breath", "burn", "chest",
    "chil", "chron", "cold", "coma", "cough", "cramp", "cry", "cut",
    "dark", "deaf", "dehy", "depre", "diz", "drain", "drow", "dry",
    "dysp", "ear", "edema", "eye", "face", "fall", "fatigue", "fev",
    "fever", "foot", "frac", "gas", "giddy", "groin", "gum", "head",
    "hear", "heart", "heat", "hemat", "hemi", "hip", "hoar", "hunger",
    "hurt", "itch", "jaun", "jaw", "joint", "knee", "leg", "lip", "loss",
    "low", "lumb", "lung", "mal", "migr", "mouth", "musc", "naus", "neck",
    "nerve", "night", "nose", "numb", "obes", "odem", "pain", "palp",
    "pani", "para", "pelvic", "peri", "phle", "poor", "prur", "pull",
    "rash", "rectal", "red", "reflu", "rig", "runny", "seiz", "sharp",
    "shin", "short", "sick", "sinus", "skin", "sleep", "slur", "sneez",
    "sore", "spasm", "sput", "stiff", "sting", "stom", "stuffy", "sweat",
    "swell", "syncop", "tender", "thirst", "throat", "tired", "trem",
    "ulcer", "urin", "vertigo", "vision", "vomit", "weak", "weight",
    "wheeze", "wrist",
]

DIAGNOSIS_QUERIES = [
    "abdom", "abscess", "acid", "acne", "adenom", "adren", "alco", "alle",
    "alop", "alz", "amyl", "anemia", "angina", "anki", "anxi", "aort",
    "arrhyth", "arth", "asthma", "ather", "atri", "autism", "behcet",
    "bili", "brain", "bronch", "cancer", "cardia", "cardio", "caries",
    "celiac", "cerv", "cirr", "clot", "coeli", "colon", "coronary",
    "crohn", "cyst", "degen", "dem", "depress", "derma", "diab", "diarrh",
    "diver", "down", "dys", "eczem", "embol", "emphy", "enceph", "endo",
    "enter", "epilep", "erythr", "fibro", "frac", "gall", "gastr",
    "glau", "gout", "grave", "hashim", "heart", "hemat", "hepat", "hern",
    "hiv", "hodgk", "hyper", "hypo", "ibd", "ibs", "infect", "inflamm",
    "insuf", "isch", "kidney", "leuk", "lipid", "liver", "lupus", "lymph",
    "malig", "mani", "measl", "melano", "mening", "migr", "mult", "myel",
    "myop", "narco", "nephro", "neuro", "nod", "obes", "occlus", "osteo",
    "ovari", "panc", "parkin", "peptic", "peri", "phob", "pleur", "pneum",
    "polyc", "psori", "pulm", "renal", "rheum", "sarco", "schiz", "sepsis",
    "sickle", "sinusit", "skin", "sleep", "steno", "stroke", "thrombo",
    "thyroid", "tumor", "ulcer", "urin", "vasc", "vascu", "virus", "vitil",
]

INVESTIGATIONS_QUERIES = [
    "abdom", "acid", "acth", "albumin", "alk", "amyl", "ana", "anti",
    "aort", "arterial", "bact", "barium", "bili", "biopsy", "blood",
    "bone", "brain", "bronch", "calc", "cardiac", "cbc", "chest", "choles",
    "clot", "coag", "colon", "complet", "cortis", "crea", "crp", "ct",
    "culture", "cytol", "ddim", "echo", "ecg", "eeg", "electr", "emg",
    "enzyme", "esoph", "exam", "ferr", "fibrin", "folic", "fundi",
    "galac", "gastro", "gluc", "growth", "hemat", "hemo", "hepat",
    "high", "histol", "holter", "hormone", "igf", "immun", "inflam",
    "iodine", "iron", "kidney", "lab", "lft", "liver", "lumbar", "lyme",
    "lymph", "magn", "mammo", "mri", "neph", "neur", "nitro", "occult",
    "osteo", "panel", "pap", "parathy", "pcr", "pelvic", "petsc", "platelet",
    "potass", "progest", "prolact", "prostat", "protein", "proth", "pulm",
    "rand", "renal", "rheum", "ribo", "screen", "serum", "sodium", "spiro",
    "sput", "stool", "strep", "thyro", "tissue", "toxic", "trig", "tropon",
    "tsh", "tumor", "ultra", "uric", "urin", "urine", "vascu", "vitamin",
    "wbc", "wound", "xray",
]

MEDICATIONS_QUERIES = [
    "acet", "aceto", "acycl", "adalim", "adeno", "adrena", "albu", "allopur",
    "alp", "amlo", "amox", "ampi", "antig", "anti", "amlod", "arip",
    "aspir", "aten", "ator", "azith", "beclom", "benzo", "beta",
    "bisopr", "bromine", "bupro", "carba", "carved", "cefaz", "ceftr",
    "celecox", "chlor", "cipro", "citalop", "clarith", "clavul", "clind",
    "clopid", "clonaz", "clonid", "codein", "colch", "cyclo", "daltepar",
    "dapto", "dexam", "dextro", "digox", "dilti", "diuret", "doxo",
    "dulox", "emtr", "enalap", "etopos", "famo", "felod", "fenof",
    "ferr", "flucon", "fluo", "fluox", "fosino", "furo", "gabap",
    "gancic", "gemcit", "glipiz", "gluco", "glybe", "halop", "hydral",
    "hydro", "hydrox", "ibup", "insulin", "irbesar", "isoniz", "isosorbid",
    "ketoprof", "lamotr", "lanotop", "laris", "levo", "linos", "lisin",
    "lithi", "loraz", "losart", "lovast", "lumef", "metfor", "metho",
    "methylpr", "metop", "metronid", "mirtaz", "momelot", "monteluk",
    "morph", "nabi", "napro", "nifed", "nitr", "nystatin", "olanz",
    "omega", "omepraz", "ondanset", "oxacill", "oxyco", "pantopraz",
    "parox", "pencil", "perindo", "phenyt", "pioglitaz", "piperac",
    "potass", "pravastat", "pred", "proges", "propra", "quetiap",
    "ramip", "ranitidin", "rifamp", "ritonavir", "rivastig", "rosuvast",
    "salmeter", "sertra", "simvast", "sitaglip", "sodium", "spiro",
    "sulfam", "sumatript", "tamox", "temaz", "tenofov", "theoph",
    "tiotropi", "topiramat", "tramad", "trastuz", "trazod", "trimeth",
    "valsar", "valproat", "vancom", "venlaf", "verapam", "warfar", "zolpidem",
]

PROCEDURES_QUERIES = [
    "abdom", "ablat", "amput", "angio", "appendec", "arth", "aspir",
    "biopsy", "blood", "bone", "bronch", "bypass", "cardiac", "cath",
    "cerv", "chemo", "cholecys", "closure", "colon", "crani", "cyst",
    "debrid", "dental", "dialys", "dila", "drain", "echo", "electro",
    "endosc", "epidur", "excis", "extract", "fibul", "fisst", "gall",
    "gastr", "heart", "hemod", "hernia", "hip", "holter", "hypno",
    "hyster", "implan", "incis", "infus", "inject", "joint", "kidney",
    "knee", "lapar", "laser", "litho", "lumbar", "lymph", "mammo",
    "mastec", "medic", "nerve", "open", "orchid", "osteo", "panc",
    "percutan", "pericardi", "physio", "place", "plast", "pleur",
    "proced", "prosta", "pulm", "radio", "recon", "rehabilit", "remov",
    "repair", "resect", "skin", "spine", "splint", "stent", "surg",
    "sutur", "symp", "thyroid", "trans", "tumor", "ultra", "vasc",
    "vasec", "venous", "vessel",
]

ADVICE_QUERIES = [
    "activ", "aerob", "alcohol", "ambul", "anti", "applic", "arth",
    "avoid", "bath", "bed", "blood", "breath", "calcium", "cardiac",
    "care", "cardio", "check", "chemo", "cold", "compre", "consult",
    "cont", "cope", "counsel", "daily", "dehy", "dental", "diet",
    "discharg", "diuret", "drink", "drug", "educa", "elev", "emerg",
    "encour", "exercise", "follow", "fluid", "gastr", "gluco", "hand",
    "health", "high", "home", "hospic", "hydrat", "hygien", "immun",
    "infect", "inject", "insul", "joint", "keep", "lifestyle", "light",
    "limit", "low", "medic", "mobil", "monitor", "motion", "nutrit",
    "occup", "oral", "pain", "pallia", "patient", "physio", "postur",
    "press", "prevent", "protect", "psycho", "pulm", "quit", "range",
    "reduc", "refer", "rehabilit", "relaxat", "rest", "restric", "salt",
    "screen", "skin", "sleep", "smoke", "social", "sport", "stress",
    "stretch", "sugar", "support", "swim", "take", "therapy", "track",
    "ultra", "vaccin", "vital", "walk", "water", "weight", "wound", "yoga",
]

# Build combined list of (section, query) tuples — 600+ total
SECTION_QUERY_PAIRS = (
    [("chief_complaint", q) for q in CHIEF_COMPLAINT_QUERIES]
    + [("diagnosis", q) for q in DIAGNOSIS_QUERIES]
    + [("investigations", q) for q in INVESTIGATIONS_QUERIES]
    + [("medications", q) for q in MEDICATIONS_QUERIES]
    + [("procedures", q) for q in PROCEDURES_QUERIES]
    + [("advice", q) for q in ADVICE_QUERIES]
)


@pytest.mark.anyio
@pytest.mark.parametrize("section,query", SECTION_QUERY_PAIRS)
async def test_complete_returns_200_for_all_section_queries(client, section, query):
    doc = make_doc(term=query.capitalize(), semantic_type="Disease or Syndrome")
    with _mock_docs([doc]):
        resp = await client.get(
            "/api/note/complete",
            params={"q": query, "section": section, "rows": 5},
        )
    assert resp.status_code == 200
    body = resp.json()
    assert body["section"] == section
    assert isinstance(body["results"], list)
    assert isinstance(body["total"], int)
    assert body["total"] >= 0
    assert body["response_time_ms"] >= 0
    assert isinstance(body["spell_corrected"], bool)
    assert isinstance(body["solr_hits"], int)


# ---------------------------------------------------------------------------
# Response shape — required fields always present
# ---------------------------------------------------------------------------

REQUIRED_RESPONSE_FIELDS = {
    "query", "section", "semantic_types_applied",
    "spell_corrected", "total", "results",
    "response_time_ms", "solr_hits",
}

REQUIRED_RESULT_FIELDS = {
    "term", "semantic_type", "source", "tty",
    "concept_id", "code", "tty_priority", "source_priority",
}


@pytest.mark.anyio
@pytest.mark.parametrize("section,query", SECTION_QUERY_PAIRS[:100])
async def test_response_has_all_required_top_level_fields(client, section, query):
    with _mock_docs():
        resp = await client.get(
            "/api/note/complete",
            params={"q": query, "section": section},
        )
    assert resp.status_code == 200
    body = resp.json()
    assert REQUIRED_RESPONSE_FIELDS.issubset(body.keys())


@pytest.mark.anyio
@pytest.mark.parametrize("section,query", SECTION_QUERY_PAIRS[:100])
async def test_each_result_has_required_fields(client, section, query):
    doc = make_doc(term="Test Term")
    with _mock_docs([doc]):
        resp = await client.get(
            "/api/note/complete",
            params={"q": query, "section": section},
        )
    assert resp.status_code == 200
    body = resp.json()
    for result in body["results"]:
        assert REQUIRED_RESULT_FIELDS.issubset(result.keys())


@pytest.mark.anyio
@pytest.mark.parametrize("section,query", SECTION_QUERY_PAIRS[:100])
async def test_total_equals_len_results(client, section, query):
    docs = [make_doc(term=f"Term {i}", concept_id=f"C{i:07d}") for i in range(3)]
    with _mock_docs(docs):
        resp = await client.get(
            "/api/note/complete",
            params={"q": query, "section": section, "rows": 5},
        )
    assert resp.status_code == 200
    body = resp.json()
    assert body["total"] == len(body["results"])


# ---------------------------------------------------------------------------
# Rows parameter — valid range 1..50
# ---------------------------------------------------------------------------

VALID_ROWS = [1, 2, 3, 5, 10, 15, 20, 25, 30, 40, 50]


@pytest.mark.anyio
@pytest.mark.parametrize("rows", VALID_ROWS)
async def test_valid_rows_values_accepted(client, rows):
    with _mock_docs():
        resp = await client.get(
            "/api/note/complete",
            params={"q": "diab", "section": "diagnosis", "rows": rows},
        )
    assert resp.status_code == 200


INVALID_ROWS = [0, -1, -100, 51, 52, 100, 1000, 99999]


@pytest.mark.anyio
@pytest.mark.parametrize("rows", INVALID_ROWS)
async def test_invalid_rows_rejected(client, rows):
    with _mock_empty():
        resp = await client.get(
            "/api/note/complete",
            params={"q": "diab", "section": "diagnosis", "rows": rows},
        )
    assert resp.status_code in (400, 422)


# ---------------------------------------------------------------------------
# Section validation
# ---------------------------------------------------------------------------

VALID_SECTIONS = [
    "chief_complaint", "diagnosis", "investigations",
    "medications", "procedures", "advice",
]

INVALID_SECTIONS = [
    "invalid", "CHIEF_COMPLAINT", "Chief_Complaint", "cc", "dx",
    "meds", "labs", "rx", "orders", "note", "soap", "hpi", "ros",
    "pmh", "fh", "sh", "exam", "assessment", "plan", "followup",
    "", "null", "undefined", "none", "0", "1", "true", "false",
    "chief complaint", "chief-complaint", "chief_complaint ",
    " diagnosis", "investigations ", "medications\n",
    "procedures\t", "advice;", "advice'", 'advice"',
    "chief_complaint OR 1=1",
    "diagnosis; DROP TABLE terms",
    "../etc/passwd",
    "<script>alert(1)</script>",
    "admin", "root", "system", "test", "demo", "api",
    "chief_complaint\x00", "diagnosis\xff",
]


@pytest.mark.anyio
@pytest.mark.parametrize("section", VALID_SECTIONS)
async def test_valid_sections_accepted(client, section):
    with _mock_docs():
        resp = await client.get(
            "/api/note/complete",
            params={"q": "test", "section": section},
        )
    assert resp.status_code == 200


@pytest.mark.anyio
@pytest.mark.parametrize("bad_section", INVALID_SECTIONS)
async def test_invalid_sections_rejected(client, bad_section):
    with _mock_empty():
        resp = await client.get(
            "/api/note/complete",
            params={"q": "test", "section": bad_section},
        )
    assert resp.status_code in (400, 422)


# ---------------------------------------------------------------------------
# Query length boundary
# ---------------------------------------------------------------------------

@pytest.mark.anyio
async def test_single_char_query_accepted(client):
    with _mock_docs():
        resp = await client.get(
            "/api/note/complete",
            params={"q": "a", "section": "diagnosis"},
        )
    assert resp.status_code == 200


@pytest.mark.anyio
async def test_200_char_query_accepted(client):
    q = "a" * 200
    with _mock_docs():
        resp = await client.get(
            "/api/note/complete",
            params={"q": q, "section": "diagnosis"},
        )
    assert resp.status_code == 200


@pytest.mark.anyio
async def test_201_char_query_rejected(client):
    q = "a" * 201
    with _mock_empty():
        resp = await client.get(
            "/api/note/complete",
            params={"q": q, "section": "diagnosis"},
        )
    assert resp.status_code in (400, 422)


@pytest.mark.anyio
@pytest.mark.parametrize("length", [202, 250, 300, 500, 1000, 5000])
async def test_oversized_queries_rejected(client, length):
    q = "x" * length
    with _mock_empty():
        resp = await client.get(
            "/api/note/complete",
            params={"q": q, "section": "diagnosis"},
        )
    assert resp.status_code in (400, 422)


# ---------------------------------------------------------------------------
# Missing required params
# ---------------------------------------------------------------------------

@pytest.mark.anyio
async def test_missing_q_param_returns_error(client):
    with _mock_empty():
        resp = await client.get(
            "/api/note/complete",
            params={"section": "diagnosis"},
        )
    assert resp.status_code in (400, 422)


@pytest.mark.anyio
async def test_missing_section_param_returns_error(client):
    with _mock_empty():
        resp = await client.get(
            "/api/note/complete",
            params={"q": "diab"},
        )
    assert resp.status_code in (400, 422)


@pytest.mark.anyio
async def test_missing_both_required_params_returns_error(client):
    with _mock_empty():
        resp = await client.get("/api/note/complete")
    assert resp.status_code in (400, 422)


# ---------------------------------------------------------------------------
# Case insensitivity — queries should not crash regardless of case
# ---------------------------------------------------------------------------

CASE_VARIANTS = [
    ("diagnosis", "Diabetes"),
    ("diagnosis", "DIABETES"),
    ("diagnosis", "DiAbEtEs"),
    ("chief_complaint", "FEVER"),
    ("chief_complaint", "Fever"),
    ("medications", "METFORMIN"),
    ("medications", "Metformin"),
    ("investigations", "BLOOD"),
    ("procedures", "BIOPSY"),
    ("advice", "EXERCISE"),
]


@pytest.mark.anyio
@pytest.mark.parametrize("section,query", CASE_VARIANTS)
async def test_case_insensitive_queries_do_not_crash(client, section, query):
    with _mock_docs():
        resp = await client.get(
            "/api/note/complete",
            params={"q": query, "section": section},
        )
    assert resp.status_code == 200


# ---------------------------------------------------------------------------
# Multi-word queries
# ---------------------------------------------------------------------------

MULTI_WORD_QUERIES = [
    ("diagnosis", "type 2"),
    ("diagnosis", "type 2 diabetes"),
    ("diagnosis", "acute kidney"),
    ("chief_complaint", "chest pain"),
    ("chief_complaint", "shortness of"),
    ("medications", "metformin 500"),
    ("procedures", "blood pressure"),
    ("investigations", "complete blood"),
    ("advice", "low salt"),
]


@pytest.mark.anyio
@pytest.mark.parametrize("section,query", MULTI_WORD_QUERIES)
async def test_multi_word_queries_do_not_crash(client, section, query):
    with _mock_docs():
        resp = await client.get(
            "/api/note/complete",
            params={"q": query, "section": section},
        )
    assert resp.status_code == 200


# ---------------------------------------------------------------------------
# fuzzy parameter
# ---------------------------------------------------------------------------

@pytest.mark.anyio
@pytest.mark.parametrize("fuzzy_val", ["true", "false", "True", "False", "1", "0"])
async def test_fuzzy_param_accepted(client, fuzzy_val):
    with _mock_docs():
        resp = await client.get(
            "/api/note/complete",
            params={"q": "diab", "section": "diagnosis", "fuzzy": fuzzy_val},
        )
    assert resp.status_code == 200


# ---------------------------------------------------------------------------
# Empty result set — not an error
# ---------------------------------------------------------------------------

@pytest.mark.anyio
@pytest.mark.parametrize("section", VALID_SECTIONS)
async def test_empty_solr_result_returns_200_with_empty_list(client, section):
    with _mock_empty():
        resp = await client.get(
            "/api/note/complete",
            params={"q": "zzzyyyxxx", "section": section},
        )
    assert resp.status_code == 200
    body = resp.json()
    assert body["results"] == []
    assert body["total"] == 0


# ---------------------------------------------------------------------------
# semantic_types_applied reflects the section
# ---------------------------------------------------------------------------

from backend.services.section_config import SECTION_SEMANTIC_TYPES


@pytest.mark.anyio
@pytest.mark.parametrize("section", VALID_SECTIONS)
async def test_semantic_types_applied_matches_section_config(client, section):
    with _mock_docs():
        resp = await client.get(
            "/api/note/complete",
            params={"q": "test", "section": section},
        )
    assert resp.status_code == 200
    body = resp.json()
    assert body["semantic_types_applied"] == SECTION_SEMANTIC_TYPES[section]


# ---------------------------------------------------------------------------
# tty_priority and source_priority are positive integers
# ---------------------------------------------------------------------------

@pytest.mark.anyio
@pytest.mark.parametrize("section,query", SECTION_QUERY_PAIRS[:60])
async def test_priority_fields_are_positive_ints(client, section, query):
    doc = make_doc(tty_priority=1, source_priority=1)
    with _mock_docs([doc]):
        resp = await client.get(
            "/api/note/complete",
            params={"q": query, "section": section},
        )
    assert resp.status_code == 200
    for r in resp.json()["results"]:
        assert isinstance(r["tty_priority"], int)
        assert isinstance(r["source_priority"], int)
        assert r["tty_priority"] >= 1
        assert r["source_priority"] >= 1


# ---------------------------------------------------------------------------
# query echoed back correctly
# ---------------------------------------------------------------------------

ECHO_QUERIES = [
    ("diagnosis", "diabetes"),
    ("chief_complaint", "fever"),
    ("medications", "metformin"),
    ("investigations", "blood"),
    ("procedures", "angiography"),
    ("advice", "exercise"),
]


@pytest.mark.anyio
@pytest.mark.parametrize("section,query", ECHO_QUERIES)
async def test_query_field_echoes_input(client, section, query):
    with _mock_docs():
        resp = await client.get(
            "/api/note/complete",
            params={"q": query, "section": section},
        )
    assert resp.status_code == 200
    assert resp.json()["query"] == query
