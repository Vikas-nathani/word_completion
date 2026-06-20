# Clinical Copilot Engine

Clinical Copilot Engine is a FastAPI-based backend for clinical note autocomplete. It uses Apache Solr as the search index and applies medical-domain ranking and filtering rules to return relevant suggestions while a clinician is typing.

The service is designed for:

- Section-aware term suggestions (for example diagnosis vs medications)
- Context-aware suggestions boosted from patient history
- Medical terminology normalization and abbreviation support
- High-speed retrieval from a Solr index with deterministic filtering

## Why This Project Exists

In clinical documentation workflows, free typing can be slow and inconsistent. This engine helps by:

- Reducing typing effort with autocomplete
- Improving terminology consistency using controlled vocabularies
- Surfacing context-relevant terms for each note section
- Prioritizing better candidates through ranking signals (TTY/source priority, semantic constraints, deduplication)

## Core Capabilities

- Fast autocomplete over medical terms indexed in Solr
- Two-layer result cache (in-process LRU + Redis) for sub-millisecond repeated queries
- Semantic filtering by note section (`chief_complaint`, `diagnosis`, `investigations`, `medications`, `procedures`, `advice`)
- Query normalization and abbreviation expansion (for example `mi` -> `myocardial infarction`)
- Improved ranking: preferred terms (PT/PN) rank above synonyms/abbreviations (SY/FN/AB) so concise terms surface before cryptic short abbreviations
- Fuzzy fallback when exact/prefix matches are empty
- Context boosting from parsed patient summary text or JSON
- Multi-word suggestion replacement in the UI — selecting a suggestion replaces the entire typed prefix, not just the last word
- API-first design with typed request/response models (Pydantic)

## High-Level Architecture

```text
Client/UI
  |
  v
FastAPI (backend.app + backend.api.router)
  |
  |-- Validation layer (backend.models.models)
  |-- Section rules (backend.services.section_config)
  |-- Context parser (backend.services.context_parser)
  |-- Search service (backend.services.search)
  |-- Two-layer cache (backend.core.cache)
  |     |-- Layer 1: in-process LRU  (~0ms, per worker)
  |     +-- Layer 2: Redis            (~1ms, shared across workers)
  |
  v
Apache Solr (umls_core)
```

### Request Flow (Section-Aware Completion)

1. Client calls `/api/note/complete` with `q`, `section`, and optional tuning params.
2. Request is validated using Pydantic models.
3. Section-specific semantic filter query is generated.
4. Search service builds Solr query + filtering constraints.
5. Solr docs are post-processed (filter, deduplicate, rerank).
6. API returns structured ranked suggestions.

### Request Flow (Context-Aware Completion)

1. Client sends text/JSON patient context to `/api/note/complete/context` (GET/POST/file upload variants).
2. Context parser extracts relevant history terms.
3. Base UMLS suggestions are fetched from Solr.
4. Matching patient-history terms are boosted to the top.
5. Response includes `from_patient_history` flags and `context_boosted_count`.

## Tech Stack

- Language: Python 3.10+
- API framework: FastAPI
- ASGI server: Uvicorn (4 workers in Docker)
- HTTP client: httpx
- Validation/models: Pydantic v2
- Search engine: Apache Solr 9 (`umls_core`)
- Cache: in-process LRU (OrderedDict) + Redis 7
- Containerization: Docker + Docker Compose
- Testing: pytest + httpx ASGI transport

Key dependencies are listed in `requirements.txt`.

## Project Structure

```text
backend/
  app.py                  # Main FastAPI app, Solr integration, ranking/filter logic
  api/router.py           # Section-aware + context-aware note APIs
  core/config.py          # Environment-driven runtime configuration
  core/cache.py           # Two-layer cache (LRU + Redis)
  models/models.py        # Request/response models and validation
  services/search.py      # Section-aware query pipeline
  services/context_parser.py
                          # Parser for plain-text/JSON patient context
  services/section_config.py
                          # Section -> semantic type mapping and filters

tests/                    # API/service behavior tests
scripts/                  # Solr data preparation and index utility scripts
infra/                    # Dev compose overrides and infra assets
data/                     # Data artifacts used by scripts/runtime
clinical_copilot_ui.html  # Basic UI served at `/`
```

## API Endpoints

### General Endpoints

- `GET /` -> serves `clinical_copilot_ui.html`
- `GET /health` -> API/Solr health snapshot
- `GET /solr/ping` -> Solr ping passthrough
- `GET /solr/select` -> Solr select wrapper with autocomplete-oriented rewrite/filtering
- `GET /search` -> generic search endpoint with optional filters (results cached)
- `GET /stats` -> index stats/facets
- `GET /cache/stats` -> LRU and Redis cache hit/miss statistics
- `POST /cache/clear` -> flush LRU and Redis caches

### Note Completion Endpoints

- `GET /api/note/complete`
  - Section-aware note autocomplete
- `GET /api/note/complete/context`
  - Context-aware autocomplete via query params
- `POST /api/note/complete/context`
  - Context-aware autocomplete via JSON body
- `POST /api/note/complete/context/file`
  - Context-aware autocomplete via multipart file upload (`.json` or plain text)
- `GET /api/note/sections`
  - Lists valid sections and their semantic type filters

## Configuration

Configuration is environment-driven.

Important variables:

- `SOLR_URL` (default: `http://localhost:8983/solr/umls_core`)
- `NOTE_API_DEFAULT_ROWS` (default: `15`)
- `NOTE_API_MAX_ROWS` (default: `50`)
- `NOTE_API_VERSION` (default: `1.0.0`)
- `NOTE_API_VALID_SECTIONS` — comma-separated override for valid sections
- `REDIS_URL` (default: `redis://localhost:6379/0`) — set to `redis://redis:6379/0` in Docker Compose
- `CACHE_LRU_MAX_SIZE` (default: `10000`) — set to `0` to disable the in-process LRU
- `CACHE_LRU_TTL_SEC` (default: `3600`)
- `CACHE_REDIS_TTL_SEC` (default: `3600`) — set to `0` to disable Redis caching

`docker-compose.yml` expects a `.env` file for backend environment injection.

## How To Run

### Option 1: Local Python Runtime

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
uvicorn backend.app:app --host 0.0.0.0 --port 8004 --reload
```

Prerequisite: a running Solr instance with `umls_core` available.

### Option 2: Docker Compose

```bash
docker compose up --build
```

This starts:

- `solr` on `8983`
- `redis` on `6390` (mapped from container port `6379`)
- `backend` on `8004` (4 Uvicorn workers)

Then open:

- API docs: `http://localhost:8004/docs`
- UI page: `http://localhost:8004/`

### Development Override (Optional)

The repository includes `infra/docker-compose.dev.yml` with dev-oriented overrides. It is intended as an override layer and may reference services not defined in the base compose file.

```bash
docker compose -f docker-compose.yml -f infra/docker-compose.dev.yml up --build 
```

## Example API Usage

### Section-Aware Completion

```bash
curl "http://localhost:8004/api/note/complete?q=diab&section=diagnosis&rows=10"
```

### Context-Aware Completion (JSON body)

```bash
curl -X POST "http://localhost:8004/api/note/complete/context" \
  -H "Content-Type: application/json" \
  -d '{
    "q": "met",
    "section": "medications",
    "rows": 10,
    "patient_context_json": {
      "conditions": ["Type 2 diabetes mellitus"],
      "medications": ["Metformin"]
    }
  }'
```

### List Valid Sections

```bash
curl "http://localhost:8004/api/note/sections"
```

## Testing

Run the test suite with:

```bash
pytest -q
```

Tests cover section validation, response shape, fuzzy fallback, and ranking/filter behavior for multiple note sections.

## Data and Indexing Utilities

The `scripts/` directory contains helper scripts for Solr data preparation and index updates (for example source/TTY priority updates and word-count enrichments). These scripts are useful when rebuilding or tuning the search index.

For a quick section vocabulary audit, run:

```bash
python3 scripts/audit_section_terms.py --limit 500 --output reports/section_term_audit.json
```

The script samples section-specific terms from the repo's clinical JSON files, checks whether Solr contains them, and prints a length summary for each section.

## Ranking

Results are ordered by a strict priority key:

1. **Relevance bucket** — exact match > starts-with > contains
2. **Term word count** — fewer words first (single-word beats multi-word)
3. **Preferred tier** — PT/PN (preferred terms) above SY/FN/AB, so cryptic short abbreviations do not outrank concise preferred terms for short prefixes
4. **Term length proximity** — shortest completion of the typed prefix first within a tier
5. **TTY priority** — PT before PN as a tiebreaker within the preferred tier
6. **Semantic type priority** — Disease > Finding > Organic Chemical
7. **Source priority** — SNOMEDCT_US > ICD10CM > NCI > ... > CHV

`MAX_WORDS` no longer imposes a hard upper bound on term word count. Short terms still appear first because both the Solr fetch sort and the Python re-rank order by word count ascending.

## Operational Notes

- The API includes CORS middleware configured with permissive defaults.
- The backend relies on Solr availability for autocomplete/search endpoints.
- Request resilience includes guarded fallbacks and service-unavailable responses for upstream failures.
- The `/search` endpoint caches plain prefix queries in a two-layer cache (LRU + Redis). Extra filters (`semantic_type`, `source`, `is_abbreviation`) bypass the cache. Use `GET /cache/stats` to inspect hit rates and `POST /cache/clear` to flush both layers.
- Redis is a soft dependency. If Redis is unreachable at startup, the backend falls back gracefully to Solr with only the in-process LRU active.

## Compatibility

Compatibility import used by tests and legacy callers is preserved:

```python
from backend import app
```
