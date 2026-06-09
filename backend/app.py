from fastapi import FastAPI, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
import httpx
import urllib.parse
import re
import os
import json
import time
import logging
from logging.handlers import RotatingFileHandler
from typing import Optional
from dotenv import load_dotenv
load_dotenv()  # loads .env from current working directory
# ─────────────────────────────────────────
# FILTERS CONFIG
# ─────────────────────────────────────────
ALLOWED_TTY = ["PT", "PN", "SY", "FN", "AB"]
# Include docs missing term_word_count so base reindex remains searchable
# before post-processing scripts populate derived fields.
MAX_WORDS = 3
MIN_SOLR_FETCH_ROWS = 200
MAX_SOLR_FETCH_ROWS = 400
TERM_WORD_COUNT_FQ = f"(term_word_count:[1 TO {MAX_WORDS}] )"

BLOCKED_SEMANTIC_TYPES = {
    # Hormone-related types are intentionally not globally blocked here;
    # note_api/search.py now blocks them for non-medication sections only.
    "Food",
    "Manufactured Object",
    "Health Care Related Organization",
    "Professional or Occupational Group",
    "Health Care Activity",
    "Gene or Genome",
    "Intellectual Product",
    "Quantitative Concept",
    "Temporal Concept",
    "Functional Concept",
    "Spatial Concept",
    "Idea or Concept",
    "Language",
    "Occupation or Discipline",
    "Organization",
    "Geographic Area",
    "Substance",
    "Indicator, Reagent, or Diagnostic Aid",
    "Therapeutic or Preventive Procedure",
    "Pathologic Function",
}

TTY_PRIORITY_MAP = {
    "PT": 1,
    "PN": 2,
    "SY": 3,
    "FN": 4,
    "AB": 5,
}
SOLR_URL = os.getenv("SOLR_URL")
if not SOLR_URL:
    raise RuntimeError("SOLR_URL environment variable is not set!")
SOURCE_PRIORITY_MAP = {
    "SNOMEDCT_US": 1,
    "ICD10CM": 2,
    "NCI": 3,
    "RXNORM": 4,
    "MSH": 5,
    "LNC": 6,
    "MEDCIN": 7,
    "ICD10PCS": 8,
    "OMIM": 9,
    "PDQ": 10,
    "CPT": 11,
    "MDR": 12,
    "MTH": 13,
    "MMSL": 14,
    "CHV": 15,
}

SEMANTIC_TYPE_PRIORITY_MAP = {
    "Disease or Syndrome": 1,
    "Finding": 2,
    "Neoplastic Process": 3,
    "Sign or Symptom": 4,
    "Pathologic Function": 5,
    "Pharmacologic Substance": 6,
    "Clinical Drug": 7,
    "Hormone": 8,
    "Mental or Behavioral Dysfunction": 11,
    "Organic Chemical": 12,
    "Hazardous or Poisonous Substance": 13,
}

SYNONYM_EXPANSIONS = {
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

# ─────────────────────────────────────────
# CONFIG
# ─────────────────────────────────────────
import os

SOLR_URL = os.getenv("SOLR_URL")
SEARCH_LOG_PATH = "/var/log/clinical_copilot/search.log"


def _safe_int(value: object, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _get_scalar(doc: dict, field: str, default: str = ""):
    value = doc.get(field, default)
    if isinstance(value, list):
        return value[0] if value else default
    return value


def _word_count(term: str) -> int:
    return len(re.findall(r"[A-Za-z0-9]+(?:[-'][A-Za-z0-9]+)?", term or ""))


def _normalize_whitespace(text: str) -> str:
    return re.sub(r"\s+", " ", text or "").strip()


def _tokenize_words(text: str) -> list[str]:
    return re.findall(r"[a-z0-9]+(?:[-'][a-z0-9]+)?", (text or "").lower())


def _escape_solr_token(token: str) -> str:
    escaped = token
    for char in r'+-!(){}[]^"~*?:\\/':
        escaped = escaped.replace(char, f"\\{char}")
    return escaped


def _extract_query_text(raw_q: str) -> str:
    query = _normalize_whitespace(raw_q)
    if not query or query == "*:*":
        return ""

    query = re.sub(r"(?i)term_lower:", "", query)
    query = re.sub(r"(?i)\b(?:AND|OR)\b", " ", query)

    if query.startswith('"') and query.endswith('"') and len(query) > 1:
        query = query[1:-1].strip()

    return _normalize_whitespace(query)


def _should_rewrite_to_autocomplete_query(raw_q: str) -> bool:
    query = (raw_q or "").strip()
    if not query:
        return True
    if query == "*:*":
        return True
    if query.lower().startswith("term_lower:"):
        return True

    special_chars = [":", "(", ")", "[", "]", "{", "}", '"']
    return not any(ch in query for ch in special_chars)


def _build_autocomplete_query(raw_q: str) -> str:
    """
    Build prefix autocomplete query for term_lower, with optional abbreviation expansion.

    Examples:
    - diab -> term_lower:diab
    - diabetes mellitus -> term_lower:diabetes AND term_lower:mellitus
    - mi -> (term_lower:mi) OR (term_lower:myocardial AND term_lower:infarction)
    """
    text = _extract_query_text(raw_q).lower()
    if not text:
        return "*:*"

    tokens = [_escape_solr_token(token) for token in text.split(" ") if token]
    if not tokens:
        return "*:*"

    base_query = " AND ".join([f"term_lower:{token}" for token in tokens])

    expansion = SYNONYM_EXPANSIONS.get(text)
    if not expansion:
        return base_query

    expanded_tokens = [_escape_solr_token(token) for token in expansion.lower().split(" ") if token]
    if not expanded_tokens:
        return base_query

    expanded_query = " AND ".join([f"term_lower:{token}" for token in expanded_tokens])
    return f"({base_query}) OR ({expanded_query})"


def _effective_query_text_for_ranking(raw_q: str) -> str:
    normalized = _normalize_whitespace(raw_q).lower()
    return SYNONYM_EXPANSIONS.get(normalized, raw_q)


def _build_blocked_semantic_fq(blocked_types: set[str]) -> str:
    """
    Build deterministic Solr fq from BLOCKED_SEMANTIC_TYPES.

    Multi-word types are quoted for exact matching and the full list is sorted
    so the generated filter string remains stable across restarts.
    """
    sorted_types = sorted(blocked_types)
    parts = []
    for semantic_type in sorted_types:
        escaped = semantic_type.replace('"', '\\"')
        if " " in escaped or "," in escaped or "-" in escaped:
            parts.append(f'"{escaped}"')
        else:
            parts.append(escaped)
    return "-semantic_type:(" + " OR ".join(parts) + ")"


def _encode_query_params(parsed: dict) -> str:
    safe_chars = ':()* ,"-'
    new_params = []
    for key, values in parsed.items():
        for val in values:
            new_params.append(
                f"{urllib.parse.quote(str(key))}={urllib.parse.quote(str(val), safe=safe_chars)}"
            )
    return "&".join(new_params)


def _fuzzy_edit_distance(query_text: str) -> int:
    compact_len = len(re.sub(r"\s+", "", query_text or ""))
    if compact_len <= 3:
        return 0
    if compact_len <= 5:
        return 1
    return 2


def _build_fuzzy_query(query_text: str, edit_distance: int) -> str:
    tokens = [tok for tok in _tokenize_words(query_text) if tok]
    if not tokens or edit_distance <= 0:
        return ""
    fuzzy_parts = [f"term:{_escape_solr_token(token)}~{edit_distance}" for token in tokens]
    return " AND ".join(fuzzy_parts)


def _filter_doc(doc: dict) -> bool:
    """
    Lightweight safety filter.

    Word-count and semantic blocking are enforced in Solr filter queries for performance.
    This function intentionally keeps only a TTY safety gate.
    """
    tty = str(_get_scalar(doc, "tty", ""))
    return tty in ALLOWED_TTY


def _parse_fl_fields(fl_value: str) -> list[str]:
    if not fl_value:
        return []
    return [field.strip() for field in fl_value.split(",") if field.strip()]


def _ensure_ranking_fields(parsed: dict) -> list[str]:
    requested_fields = _parse_fl_fields((parsed.get("fl") or [""])[0])
    if "*" in requested_fields:
        return requested_fields

    required_fields = [
        "id",
        "term",
        "tty",
        "concept_id",
        "source",
        "tty_priority",
        "source_priority",
        "term_word_count",
        "term_length",
    ]
    final_fields = list(requested_fields)
    for field in required_fields:
        if field not in final_fields:
            final_fields.append(field)
    if final_fields:
        parsed["fl"] = [",".join(final_fields)]
    return requested_fields


def _project_docs_by_fl(docs: list[dict], requested_fields: list[str]) -> list[dict]:
    if not requested_fields or "*" in requested_fields:
        return docs
    projected = []
    for doc in docs:
        projected_doc = {}
        for field in requested_fields:
            if field in doc:
                projected_doc[field] = doc[field]
        projected.append(projected_doc)
    return projected


def _normalize_output_fields(doc: dict) -> dict:
    """Ensure response fields stay consistent even if derived Solr fields lag."""
    normalized = dict(doc)
    term_text = str(_get_scalar(doc, "term", ""))

    normalized["tty_priority"] = _tty_priority_value(doc)
    normalized["source_priority"] = _source_priority_value(doc)
    normalized["term_word_count"] = _safe_int(
        _get_scalar(doc, "term_word_count", _word_count(term_text)),
        _word_count(term_text),
    )
    normalized["term_length"] = _safe_int(
        _get_scalar(doc, "term_length", len(term_text)),
        len(term_text),
    )
    return normalized


def _tty_priority_value(doc: dict) -> int:
    tty = str(_get_scalar(doc, "tty", "")).upper()
    if tty in TTY_PRIORITY_MAP:
        return TTY_PRIORITY_MAP[tty]
    return _safe_int(_get_scalar(doc, "tty_priority", 6), 6)


def _source_priority_value(doc: dict) -> int:
    source = str(_get_scalar(doc, "source", "")).upper()
    if source in SOURCE_PRIORITY_MAP:
        return SOURCE_PRIORITY_MAP[source]
    return _safe_int(_get_scalar(doc, "source_priority", 16), 16)


def _semantic_type_priority_value(doc: dict) -> int:
    semantic_type = str(_get_scalar(doc, "semantic_type", ""))
    if semantic_type in SEMANTIC_TYPE_PRIORITY_MAP:
        return SEMANTIC_TYPE_PRIORITY_MAP[semantic_type]
    return 10


def _deduplicate_by_concept_id(docs: list[dict]) -> list[dict]:
    """
    Deduplicate by concept_id while intentionally using a different key than reranking.

    Deduplication picks the simplest representative for each concept first
    (fewest words), then applies clinical source/TTY priorities. This prevents
    verbose concept variants from replacing concise canonical terms.
    """

    def dedup_key(doc: dict):
        term = _normalize_whitespace(str(_get_scalar(doc, "term", ""))).lower()
        return (
            # Word count first — 1-word term always beats 2-word term for
            # same concept. This ensures bare "Hypertension" (MTH, 1 word)
            # beats "Hypertension resolved" (SNOMEDCT_US, 2 words) as the
            # concept representative shown to the doctor.
            _word_count(term),
            # TTY and source priority second — among equal word count terms,
            # best clinical source wins.
            _tty_priority_value(doc),
            _source_priority_value(doc),
            len(term),
            0 if term and term[0].isupper() else 1,
            term,
        )

    concept_best = {}
    passthrough = []
    for doc in docs:
        concept_id = str(_get_scalar(doc, "concept_id", "")).strip()
        if not concept_id or concept_id == "None":
            passthrough.append(doc)
            continue

        existing = concept_best.get(concept_id)
        if existing is None or dedup_key(doc) < dedup_key(existing):
            concept_best[concept_id] = doc

    result = list(passthrough)
    emitted = set()
    for doc in docs:
        concept_id = str(_get_scalar(doc, "concept_id", "")).strip()
        if not concept_id or concept_id == "None":
            continue
        if concept_id in emitted:
            continue
        best = concept_best.get(concept_id)
        if best is None:
            continue
        emitted.add(concept_id)
        result.append(best)

    seen_doc_ids = set()
    final = []
    for doc in result:
        doc_obj_id = id(doc)
        if doc_obj_id in seen_doc_ids:
            continue
        seen_doc_ids.add(doc_obj_id)
        final.append(doc)

    return final


def _collapse_exact_surface_variants(docs: list[dict], query_text: str) -> list[dict]:
    """
    Collapse exact surface duplicates (case variants) for the active query.

    This is a targeted fallback when source records for the same visible term
    carry different concept_id values. It only applies to exact term matches to
    the query text, so broader homonyms are not collapsed.
    """
    normalized_query = _normalize_whitespace(query_text).lower()
    if not normalized_query:
        return docs

    def choose_better(current: dict, candidate: dict) -> dict:
        current_key = (
            _source_priority_value(current),
            _tty_priority_value(current),
            _safe_int(_get_scalar(current, "term_length", len(_normalize_whitespace(str(_get_scalar(current, "term", ""))))), 9999),
        )
        candidate_key = (
            _source_priority_value(candidate),
            _tty_priority_value(candidate),
            _safe_int(_get_scalar(candidate, "term_length", len(_normalize_whitespace(str(_get_scalar(candidate, "term", ""))))), 9999),
        )
        return candidate if candidate_key < current_key else current

    selected_by_term = {}
    passthrough = []

    for doc in docs:
        normalized_term = _normalize_whitespace(str(_get_scalar(doc, "term", ""))).lower()
        if normalized_term != normalized_query:
            passthrough.append(doc)
            continue
        current = selected_by_term.get(normalized_term)
        if current is None:
            selected_by_term[normalized_term] = doc
        else:
            selected_by_term[normalized_term] = choose_better(current, doc)

    if normalized_query in selected_by_term:
        return [selected_by_term[normalized_query], *passthrough]
    return docs


def _relevance_bucket(term: str, query_text: str, query_tokens: list[str]) -> int:
    normalized_term = _normalize_whitespace(term).lower()
    if not normalized_term:
        return 9

    if not query_text:
        return 9

    if normalized_term == query_text:
        return 0

    if normalized_term.startswith(query_text):
        return 1

    term_words = set(_tokenize_words(normalized_term))
    if query_tokens and all(token in term_words for token in query_tokens):
        return 3
    if query_tokens and any(token in term_words for token in query_tokens):
        return 4
    return 9


def _rerank_docs(docs: list[dict], query_text: str) -> list[dict]:
    """
    Final Python re-ranking applied after Solr retrieval.

    Sort key priority (strict order):
    1. relevance_bucket     — exact match > starts-with > contains
    2. term_word_count      — fewer words first (1-word beats 2-word)
    3. tty_priority         — PT > PN > SY > FN > AB
    4. semantic_type_priority — Disease > Finding > Organic Chemical
    5. source_priority      — SNOMEDCT_US > ICD10CM > NCI > ... > CHV
    6. term_length_proximity — shorter completion of the typed prefix first
                               replaces alphabetical which had no clinical meaning
                               proximity = term_length - len(query)
                               Hypertension(12) - hypert(6) = 6 beats
                               Hypertrichosis(14) - hypert(6) = 8
    """
    normalized_query = _normalize_whitespace(query_text).lower()
    tokens = _tokenize_words(normalized_query)
    query_len = len(normalized_query)

    return sorted(
        docs,
        key=lambda doc: (
            _relevance_bucket(str(_get_scalar(doc, "term", "")), normalized_query, tokens),
            _safe_int(_get_scalar(doc, "term_word_count", _word_count(str(_get_scalar(doc, "term", "")))), 999),
            _tty_priority_value(doc),
            _semantic_type_priority_value(doc),
            _source_priority_value(doc),
            # Term length proximity: how many chars remain to complete the prefix.
            # Smaller = shorter completion = more likely what the doctor is typing.
            # Source priority above this ensures SNOMEDCT_US always beats CHV/MDR
            # before proximity is even considered.
            _safe_int(_get_scalar(doc, "term_length", len(_normalize_whitespace(str(_get_scalar(doc, "term", ""))))), 9999) - query_len,
        ),
    )


def _prefetch_sort_for_query(query_text: str) -> str:
    """
    Choose the Solr fetch sort based on query shape.

    For single-word prefix queries (most autocomplete cases), sort by
    word count, combined tty_priority (which encodes both TTY and source),
    then term length. This guarantees short single-word completions like
    "Hypertension" appear in the 100 docs Solr returns to Python, with
    SNOMEDCT_US PT (priority 1) always beating CHV PT (priority 11).
    Source priority is no longer needed as a separate signal.

    For multi-word queries, BM25 score is the primary signal because
    the user has typed enough context for relevance scoring to be meaningful.
    """
    tokens = _tokenize_words(query_text)
    if len(tokens) == 1:
        # Single word prefix — combined priority ensures source quality is baked in.
        return "term_word_count asc, tty_priority asc, source_priority asc, term_length asc"
    # Multi-word — BM25 score is meaningful, use it as primary signal.
    return "term_word_count asc, tty_priority asc, source_priority asc, term_length asc"


def _extract_filters_applied_from_fq(fq_list: list[str]) -> dict:
    semantic_type_val = None
    source_val = None
    abbreviations_val = None

    for fq in fq_list:
        if fq.startswith("semantic_type:"):
            semantic_type_val = fq.split(":", 1)[1]
        elif fq.startswith("source:"):
            source_val = fq.split(":", 1)[1]
        elif fq.startswith("is_abbreviation:"):
            abbreviations_val = fq.split(":", 1)[1]

    return {
        "semantic_type": semantic_type_val,
        "source": source_val,
        "abbreviations": abbreviations_val,
    }


def _setup_search_logger() -> Optional[logging.Logger]:
    logger = logging.getLogger("clinical_copilot.search")
    logger.setLevel(logging.INFO)
    logger.propagate = False

    if logger.handlers:
        return logger

    try:
        os.makedirs(os.path.dirname(SEARCH_LOG_PATH), exist_ok=True)
        handler = RotatingFileHandler(
            SEARCH_LOG_PATH,
            maxBytes=10 * 1024 * 1024,
            backupCount=5,
            encoding="utf-8",
        )
        # JSON is written explicitly; formatter is intentionally raw.
        handler.setFormatter(logging.Formatter("%(message)s"))
        logger.addHandler(handler)
        return logger
    except Exception as exc:
        logging.getLogger("clinical_copilot.bootstrap").warning(
            "Search logging disabled: %s",
            exc,
        )
        return None


def _log_search_event(
    *,
    query: str,
    results_returned: int,
    total_solr_hits: int,
    response_time_ms: float,
    filters_applied: dict,
    spell_corrected: bool = False,
) -> None:
    if SEARCH_LOGGER is None:
        return

    normalized_query = _normalize_whitespace(query)
    payload = {
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "query": query,
        "query_length": len(query),
        "query_word_count": len(normalized_query.split(" ")) if normalized_query else 0,
        "is_abbreviation_query": normalized_query.lower() in SYNONYM_EXPANSIONS,
        "spell_corrected": bool(spell_corrected),
        "results_returned": results_returned,
        "total_solr_hits": total_solr_hits,
        "response_time_ms": round(response_time_ms, 2),
        "filters_applied": {
            "semantic_type": filters_applied.get("semantic_type"),
            "source": filters_applied.get("source"),
            "abbreviations": filters_applied.get("abbreviations"),
        },
    }
    SEARCH_LOGGER.info(json.dumps(payload, ensure_ascii=True))


# Build once at startup so BLOCKED_SEMANTIC_TYPES remains the only source of truth.
BLOCKED_SEMANTIC_TYPES_FQ = _build_blocked_semantic_fq(BLOCKED_SEMANTIC_TYPES)
SEARCH_LOGGER = _setup_search_logger()


async def _fetch_filtered_docs(
    client: httpx.AsyncClient,
    parsed: dict,
    requested_start: int,
    requested_rows: int,
) -> tuple[list, int, int]:
    needed_count = max(1, requested_start + requested_rows)
    fetch_rows = min(max(needed_count * 20, MIN_SOLR_FETCH_ROWS), MAX_SOLR_FETCH_ROWS)

    parsed_for_fetch = {key: list(values) for key, values in parsed.items()}
    parsed_for_fetch["start"] = ["0"]
    parsed_for_fetch["rows"] = [str(fetch_rows)]

    final_url = f"{SOLR_URL}/select?{_encode_query_params(parsed_for_fetch)}"
    resp = await client.get(final_url, timeout=30)
    resp.raise_for_status()
    data = resp.json()

    docs = data.get("response", {}).get("docs", [])
    filtered_docs = [doc for doc in docs if _filter_doc(doc)]
    solr_num_found = _safe_int(data.get("response", {}).get("numFound", 0), 0)
    qtime = _safe_int(data.get("responseHeader", {}).get("QTime", 0), 0)
    return filtered_docs, solr_num_found, qtime


async def _fuzzy_search_fallback(
    *,
    raw_q: str,
    requested_rows: int,
    fl_value: str,
) -> tuple[list[dict], int]:
    """
    Fallback spell correction search on Solr `term` fuzzy queries.

    This runs only when the primary EdgeNGram autocomplete path returns zero
    results. Edit distance is chosen by query length to balance recovery and
    noise: <=3 chars disables fuzzy, 4-5 uses ~1, and 6+ uses ~2.
    The fallback applies the same TTY/semantic/word-count filters and then
    reuses the same filtering, deduplication, and reranking pipeline.
    """
    query_text = _extract_query_text(raw_q)
    edit_distance = _fuzzy_edit_distance(query_text)
    if edit_distance <= 0:
        return [], 0

    fuzzy_q = _build_fuzzy_query(query_text, edit_distance)
    if not fuzzy_q:
        return [], 0

    fetch_rows = min(max(requested_rows * 20, MIN_SOLR_FETCH_ROWS), MAX_SOLR_FETCH_ROWS)
    parsed = {
        "q": [fuzzy_q],
        "wt": ["json"],
        "rows": [str(fetch_rows)],
        "start": ["0"],
        "fl": [fl_value],
        "sort": ["score desc, tty_priority asc, source_priority asc"],
        "fq": [
            "tty:(" + " OR ".join(ALLOWED_TTY) + ")",
            TERM_WORD_COUNT_FQ,
            BLOCKED_SEMANTIC_TYPES_FQ,
        ],
    }

    try:
        async with httpx.AsyncClient() as client:
            final_url = f"{SOLR_URL}/select?{_encode_query_params(parsed)}"
            resp = await client.get(final_url, timeout=30)
            resp.raise_for_status()
            data = resp.json()
    except Exception:
        return [], 0

    docs = data.get("response", {}).get("docs", [])
    filtered_docs = [doc for doc in docs if _filter_doc(doc)]
    deduped_docs = _deduplicate_by_concept_id(filtered_docs)
    deduped_docs = _collapse_exact_surface_variants(deduped_docs, query_text=query_text)
    ranked_docs = _rerank_docs(deduped_docs, query_text=query_text)
    return ranked_docs[:requested_rows], _safe_int(data.get("response", {}).get("numFound", 0), 0)


# ─────────────────────────────────────────
# APP
# ─────────────────────────────────────────
app = FastAPI(
    title="Clinical Copilot Engine",
    description="Medical term autocomplete API powered by Apache Solr",
    version="1.0.0"
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


# ─────────────────────────────────────────
# ROUTES
# ─────────────────────────────────────────
async def _safe_solr_ping() -> tuple[dict, int]:
    """Return Solr status without surfacing transport errors to callers."""
    try:
        async with httpx.AsyncClient() as client:
            resp = await client.get(f"{SOLR_URL}/admin/ping?wt=json", timeout=10)
            resp.raise_for_status()
            payload = resp.json()
        return payload, 200
    except httpx.HTTPError as exc:
        return {
            "status": "unavailable",
            "error": str(exc),
            "solr_url": SOLR_URL,
        }, 503


@app.get("/")
async def root():
    return FileResponse("clinical_copilot_ui.html")


@app.get("/health")
async def health():
    solr_payload, solr_code = await _safe_solr_ping()
    response = {
        "api": "ok",
        "solr": solr_payload.get("status", "unknown"),
        "solr_url": SOLR_URL,
    }
    if solr_code != 200:
        response["solr_details"] = solr_payload
    return response


@app.get("/solr/ping")
async def solr_ping():
    payload, status_code = await _safe_solr_ping()
    return JSONResponse(content=payload, status_code=status_code)


@app.get("/solr/select")
async def solr_select(request: Request):
    start_ts = time.perf_counter()

    params = str(request.url.query)
    parsed = urllib.parse.parse_qs(params, keep_blank_values=True)

    original_rows = max(1, _safe_int((parsed.get("rows") or [15])[0], 15))
    original_start = max(0, _safe_int((parsed.get("start") or [0])[0], 0))

    raw_q = (parsed.get("q") or ["*:*"])[0]
    if _should_rewrite_to_autocomplete_query(raw_q):
        parsed["q"] = [_build_autocomplete_query(raw_q)]

    tty_filter = "tty:(" + " OR ".join(ALLOWED_TTY) + ")"
    fq_list = list(parsed.get("fq", []))
    if tty_filter not in fq_list:
        fq_list.append(tty_filter)

    term_word_count_fq = TERM_WORD_COUNT_FQ
    if term_word_count_fq not in fq_list:
        fq_list.append(term_word_count_fq)

    if BLOCKED_SEMANTIC_TYPES_FQ not in fq_list:
        fq_list.append(BLOCKED_SEMANTIC_TYPES_FQ)

    requested_fields = _ensure_ranking_fields(parsed)
    query_text = _effective_query_text_for_ranking(raw_q)
    spell_corrected = False

    parsed["sort"] = [_prefetch_sort_for_query(query_text)]
    parsed["fq"] = fq_list

    async with httpx.AsyncClient() as client:
        filtered_docs, solr_num_found, qtime_total = await _fetch_filtered_docs(
            client=client,
            parsed=parsed,
            requested_start=original_start,
            requested_rows=original_rows,
        )

    deduped_docs = _deduplicate_by_concept_id(filtered_docs)
    deduped_docs = _collapse_exact_surface_variants(deduped_docs, query_text=query_text)
    ranked_docs = _rerank_docs(deduped_docs, query_text=query_text)

    if not ranked_docs and _should_rewrite_to_autocomplete_query(raw_q):
        fuzzy_docs, fuzzy_num_found = await _fuzzy_search_fallback(
            raw_q=raw_q,
            requested_rows=original_rows,
            fl_value=parsed.get("fl", [""])[0],
        )
        if fuzzy_docs:
            ranked_docs = fuzzy_docs
            solr_num_found = fuzzy_num_found
            spell_corrected = True

    page_docs = ranked_docs[original_start: original_start + original_rows]
    page_docs = [_normalize_output_fields(doc) for doc in page_docs]
    page_docs = _project_docs_by_fl(page_docs, requested_fields)

    data = {
        "responseHeader": {
            "status": 0,
            "QTime": qtime_total,
            "params": {
                "q": parsed.get("q", ["*:*"])[0],
                "rows": str(original_rows),
                "start": str(original_start),
            },
        },
        "response": {
            "numFound": len(ranked_docs),
            "start": original_start,
            "docs": page_docs,
        },
        "meta": {
            "scanned_docs": len(filtered_docs),
            "scan_truncated": False,
        },
        "spell_corrected": spell_corrected,
    }

    try:
        _log_search_event(
            query=raw_q,
            results_returned=len(page_docs),
            total_solr_hits=solr_num_found,
            response_time_ms=(time.perf_counter() - start_ts) * 1000.0,
            filters_applied=_extract_filters_applied_from_fq(fq_list),
            spell_corrected=spell_corrected,
        )
    except Exception:
        pass

    return data


@app.get("/search")
async def search(
    q: str = Query(..., description="Search prefix"),
    semantic_type: Optional[str] = Query(None, description="Filter by semantic type"),
    source: Optional[str] = Query(None, description="Filter by source"),
    is_abbreviation: Optional[bool] = Query(None, description="Filter abbreviations"),
    rows: int = Query(15, description="Number of results"),
    start: int = Query(0, description="Pagination offset"),
):
    start_ts = time.perf_counter()
    spell_corrected = False

    fq_list = [
        "tty:(" + " OR ".join(ALLOWED_TTY) + ")",
        TERM_WORD_COUNT_FQ,
        BLOCKED_SEMANTIC_TYPES_FQ,
    ]

    if semantic_type:
        fq_list.append(f'semantic_type:"{semantic_type}"')
    if source:
        fq_list.append(f"source:{source}")
    if is_abbreviation is not None:
        fq_list.append(f"is_abbreviation:{str(is_abbreviation).lower()}")

    effective_query_text = _effective_query_text_for_ranking(q)

    parsed = {
        "q": [_build_autocomplete_query(q)],
        "wt": ["json"],
        "sort": [_prefetch_sort_for_query(_extract_query_text(effective_query_text))],
        "fl": [
            "id,term,tty,tty_priority,semantic_type,source,source_priority,code,concept_id,is_abbreviation,stn_path,parent_stn,parent_stn_id,depth_level,term_word_count,term_length"
        ],
        "fq": fq_list,
    }

    async with httpx.AsyncClient() as client:
        filtered_docs, solr_num_found, qtime_total = await _fetch_filtered_docs(
            client=client,
            parsed=parsed,
            requested_start=max(0, start),
            requested_rows=max(1, rows),
        )

    deduped_docs = _deduplicate_by_concept_id(filtered_docs)
    deduped_docs = _collapse_exact_surface_variants(deduped_docs, query_text=effective_query_text)
    ranked_docs = _rerank_docs(deduped_docs, query_text=effective_query_text)

    if not ranked_docs:
        fuzzy_docs, fuzzy_num_found = await _fuzzy_search_fallback(
            raw_q=q,
            requested_rows=max(1, rows),
            fl_value=parsed.get("fl", [""])[0],
        )
        if fuzzy_docs:
            ranked_docs = fuzzy_docs
            solr_num_found = fuzzy_num_found
            spell_corrected = True

    page_docs = ranked_docs[start: start + rows]
    page_docs = [_normalize_output_fields(doc) for doc in page_docs]

    results = []
    for doc in page_docs:
        def get_val(field):
            return _get_scalar(doc, field, "")

        results.append({
            "id": get_val("id"),
            "term": get_val("term"),
            "tty": get_val("tty"),
            "tty_priority": get_val("tty_priority"),
            "semantic_type": get_val("semantic_type"),
            "source": get_val("source"),
            "source_priority": get_val("source_priority"),
            "code": get_val("code"),
            "concept_id": get_val("concept_id"),
            "is_abbreviation": doc.get("is_abbreviation", False),
            "stn_path": get_val("stn_path"),
            "parent_stn": get_val("parent_stn"),
            "parent_stn_id": get_val("parent_stn_id"),
            "depth_level": get_val("depth_level"),
        })

    try:
        _log_search_event(
            query=q,
            results_returned=len(page_docs),
            total_solr_hits=solr_num_found,
            response_time_ms=(time.perf_counter() - start_ts) * 1000.0,
            filters_applied={
                "semantic_type": semantic_type,
                "source": source,
                "abbreviations": None if is_abbreviation is None else str(is_abbreviation).lower(),
            },
            spell_corrected=spell_corrected,
        )
    except Exception:
        pass

    return {
        "total": len(ranked_docs),
        "start": start,
        "rows": rows,
        "results": results,
        "query_time_ms": qtime_total,
        "spell_corrected": spell_corrected,
    }


@app.get("/stats")
async def stats():
    async with httpx.AsyncClient() as client:
        resp = await client.get(f"{SOLR_URL}/select?q=*:*&rows=0&wt=json", timeout=30)
        total = resp.json()["response"]["numFound"]

        resp = await client.get(f"{SOLR_URL}/select?q=*:*&fq=is_abbreviation:true&rows=0&wt=json", timeout=30)
        abbr_count = resp.json()["response"]["numFound"]

        resp = await client.get(
            f"{SOLR_URL}/select?q=*:*&rows=0&wt=json&facet=true&facet.field=source&facet.limit=10",
            timeout=30,
        )
        facets = resp.json()["facet_counts"]["facet_fields"]["source"]
        sources = {facets[i]: facets[i + 1] for i in range(0, len(facets), 2)}

        resp = await client.get(
            f"{SOLR_URL}/select?q=*:*&rows=0&wt=json&facet=true&facet.field=semantic_type&facet.limit=10",
            timeout=30,
        )
        facets = resp.json()["facet_counts"]["facet_fields"]["semantic_type"]
        sem_types = {facets[i]: facets[i + 1] for i in range(0, len(facets), 2)}

    return {
        "total_terms": total,
        "abbreviations": abbr_count,
        "top_sources": sources,
        "top_semantic_types": sem_types,
    }

from backend.api.router import router as note_router
app.include_router(note_router)
if __name__ == "__main__":
    import uvicorn
    uvicorn.run("backend.app:app", host="0.0.0.0", port=8004, reload=True)
