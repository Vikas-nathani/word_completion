# Scripts Pipeline — How the Solr Index Is Built

This document explains every script in `scripts/`, what it does internally, the
exact order to run them, what data moves where, and which scripts match the
current live index state.

---

## Prerequisites Before Running Anything

| Service | Host | Port | Database |
|---|---|---|---|
| PostgreSQL | localhost | 5432 | `umls_db` (user: `postgres`) |
| MySQL | localhost | 3306 | `umls_db` (user: `umls_user`) |
| Apache Solr | localhost | 8983 | core: `umls_core` |

PostgreSQL holds the raw UMLS source tables (`terms_master`, `autocomplete_terms`,
`mrsty`). MySQL is the intermediate staging layer. Solr is the final index served
by the API.

Install Python dependencies before running any script:

```bash
pip install -r requirements.txt
# also needs: mysql-connector-python psycopg2-binary requests
```

---

## Current Index State (as of 2026-06-19)

| Solr Field | Populated | How |
|---|---|---|
| `term`, `tty`, `semantic_type`, `source`, `stn_path`, `ancestor_path`, `depth_level`, `semantic_type_id`, `is_leaf`, `parent_stn` | Yes | Step 4 — initial index |
| `tty_priority` | Yes — 0 docs missing | Step 5a |
| `source_priority` | Yes — 0 docs missing | Step 5b |
| `term_word_count`, `term_length` | Yes — 0 docs missing | Step 5c |
| `parent_stn_id` | **No — all 4,271,299 docs empty** | Step 6 not yet run |
| `combined_priority` | **Not in schema** | Step 7 not yet run |

---

## Full Pipeline — Step by Step

```
PostgreSQL (umls_db)
  terms_master
  autocomplete_terms          MySQL (umls_db)          Solr (umls_core)
  mrsty                              │                        │
     │                               │                        │
     ▼                               │                        │
 [STEP 1]                            │                        │
 populate_stn_tree.py                │                        │
 Reads: mrsty + autocomplete_terms   │                        │
 Writes: MySQL → stn_tree ──────────►│                        │
                                     │                        │
     ▼                               │                        │
 [STEP 2]                            │                        │
 populate_terms.py                   │                        │
 Reads: PG terms_master + autocomplete_terms                  │
 Reads: MySQL stn_tree (stn_id map)  │                        │
 Writes: MySQL → terms ─────────────►│                        │
                                     │                        │
     ▼                               │                        │
 [STEP 3]                            │                        │
 populate_solr_preview.py            │                        │
 Reads: MySQL terms JOIN stn_tree    │                        │
 Writes: MySQL → solr_preview ──────►│                        │
                                     │                        │
     ▼                               │                        │
 [STEP 4]                            │                        │
 reindex_clean.sh → index_solr.py    │                        │
 Reads: MySQL solr_preview ──────────┘                        │
 Writes: Solr → 4,271,299 docs ──────────────────────────────►│
                                                              │
 [STEP 5a] update_solr_tty_priority.py                        │
 Reads: MySQL solr_preview (id, tty)                          │
 Writes: Solr → tty_priority field ──────────────────────────►│
                                                              │
 [STEP 5b] update_solr_source_priority.py                     │
 Reads: MySQL solr_preview (id, source_priority)              │
 Writes: Solr → source_priority field ───────────────────────►│
                                                              │
 [STEP 5c] update_solr_word_count_and_length.py               │
 Reads: Solr cursor (all docs, no MySQL needed)               │
 Writes: Solr → term_word_count + term_length ───────────────►│
                                                              │
              ── CURRENT STATE REACHED ──                     │
                                                              │
 [STEP 6]  update_solr_parent_stn_id.py     (not yet run)    │
 Reads: MySQL solr_preview (id, parent_stn_id)                │
 Writes: Solr → parent_stn_id field ─────────────────────────►│
                                                              │
 [STEP 7]  update_solr_combined_priority.py (not yet run)     │
 Reads: MySQL solr_preview (id, tty, source)                  │
 Writes: Solr → tty_priority field (overwrite) ──────────────►│
 WARNING: overwrites tty_priority set in Step 5a              │
```

---

## Script Details

### Step 1 — `populate_stn_tree.py`

**What it does:** Builds the Semantic Type Network (STN) hierarchy table in MySQL.

**Reads from PostgreSQL:**
- `mrsty` — distinct STN paths with their TUI (type unique identifier) and STY
  (semantic type name). One row per unique path.
- `autocomplete_terms` — counts how many terms live under each STN path.

**Derives in Python (no DB query):**
- `parent_stn` — strips the last segment off the path
  (`A1.1.2` → `A1.1`, `A1` → `A`)
- `ancestor_path` — full chain from root to this node, slash-separated
  (`A/A1/A1.1/A1.1.2`)
- `depth_level` — integer depth (root `A` = 1, `A1` = 2, `A1.1` = 3, etc.)
- `is_leaf` — True if no other path starts with this path as a prefix

**Writes to MySQL:** `stn_tree` table

**Run command:**
```bash
python3 scripts/populate_stn_tree.py
```

**Estimated time:** 2–5 minutes

**Verify:**
```sql
SELECT COUNT(*) FROM stn_tree;             -- total STN nodes
SELECT COUNT(*) FROM stn_tree WHERE is_leaf = TRUE;   -- leaf nodes
SELECT COUNT(*) FROM stn_tree WHERE parent_stn IS NULL; -- root nodes
```

---

### Step 2 — `populate_terms.py`

**What it does:** Joins the raw UMLS term data (PostgreSQL) with the STN ID map
(MySQL) and writes the combined result into MySQL for use by downstream scripts.

**Reads from MySQL:** `stn_tree` — loads the full `stn_path → stn_id` map into
memory before the main query runs.

**Reads from PostgreSQL (server-side cursor, 10k batch):**
```sql
SELECT DISTINCT ON (tm.id)
    tm.id, tm.term, tm.term_lower, tm.is_abbreviation, tm.tty,
    at.term_id, at.concept_id, at.semantic_type, at.stn, at.source, at.code
FROM terms_master tm
LEFT JOIN autocomplete_terms at ON tm.term = at.term
ORDER BY tm.id, CASE at.tty WHEN 'PT' THEN 1 WHEN 'PN' THEN 2 ... END
```
The `DISTINCT ON` keeps only the best-TTY row per term ID.

**Writes to MySQL:** `terms` table — 4,271,299 rows, batched 10,000 at a time
with a progress bar every 10 batches.

**Run command:**
```bash
python3 scripts/populate_terms.py
```

**Estimated time:** 30–60 minutes

**Verify:**
```sql
SELECT COUNT(*) FROM terms;                      -- should be ~4,271,299
SELECT COUNT(*) FROM terms WHERE stn_id IS NULL; -- terms with no STN mapping
SELECT COUNT(DISTINCT tty) FROM terms;           -- distinct TTY types
```

---

### Step 3 — `populate_solr_preview.py`

**What it does:** Joins `terms` with `stn_tree` in MySQL to produce a single
fully-denormalized staging table (`solr_preview`) that mirrors what will be
indexed into Solr. This is the source of truth for all subsequent Solr updates.

**Reads from MySQL (server-side cursor, 10k batch):**
```sql
SELECT t.id, t.term, t.term_lower, t.is_abbreviation, t.tty,
       t.term_id, t.concept_id, t.semantic_type, t.stn, t.source, t.code,
       s.stn_path, s.parent_stn, s.ancestor_path, s.depth_level,
       s.semantic_type_id, s.is_leaf
FROM terms t
JOIN stn_tree s ON t.stn_id = s.stn_id
```

**Writes to MySQL:** `solr_preview` table — same ~4.27M rows with the full
hierarchy fields merged in.

**Run command:**
```bash
python3 scripts/populate_solr_preview.py
```

**Estimated time:** 30–60 minutes

**Verify:**
```sql
SELECT COUNT(*) FROM solr_preview;
SELECT COUNT(*) FROM solr_preview WHERE ancestor_path IS NULL; -- should be 0
SELECT COUNT(*) FROM solr_preview WHERE semantic_type_id IS NULL;
-- Disease or Syndrome sanity check:
SELECT COUNT(*) FROM solr_preview WHERE ancestor_path LIKE '%A1.2.2%';
```

---

### Step 4 — `reindex_clean.sh` + `index_solr.py`

**What it does:** Wipes the Solr index completely and rebuilds it from
`solr_preview`.

> **Warning:** `index_solr.py` is **not committed to the repository**. It must
> be present in `scripts/` before running this step. It was used during original
> setup and reads from MySQL `solr_preview`, then POSTs documents to Solr in
> batches.

**`reindex_clean.sh` sequence:**
1. `DELETE *:*` — wipes all Solr documents with a commit
2. Verifies document count is 0 (retries once if not)
3. Calls `python3 index_solr.py`
4. Verifies final document count
5. Runs a smoke-test prefix query for `hyperten`

**Run command:**
```bash
bash scripts/reindex_clean.sh
```

**Estimated time:** 10–30 minutes

**Verify:**
```bash
curl "http://localhost:8983/solr/umls_core/select?q=*:*&rows=0&wt=json"
# numFound should be 4271299
```

---

### Step 5a — `update_solr_tty_priority.py`

**What it does:** Reads the `tty` (term type) for every document from MySQL
`solr_preview` and writes a numeric `tty_priority` field back to Solr via
atomic updates. Lower number = higher quality term.

**Priority mapping:**

| TTY | Priority | Meaning |
|---|---|---|
| PT | 1 | Preferred Term |
| PN | 2 | Preferred Name |
| SY | 3 | Synonym |
| FN | 4 | Fully Specified Name |
| AB | 5 | Abbreviation |
| (others) | 6 | Default |

**Reads from MySQL:** `solr_preview` — columns `id`, `tty`

**Writes to Solr:** `tty_priority` field via atomic `{"set": value}` updates,
10,000 docs per batch, commit at end.

**Run command:**
```bash
python3 scripts/update_solr_tty_priority.py
```

**Estimated time:** 30–60 minutes

**Verify (built into script):** Runs a sample query for `diab` sorted by
`tty_priority asc` and prints the top 10 results.

---

### Step 5b — `update_solr_source_priority.py`

**What it does:** Same pattern as 5a but for the `source` field. Writes
`source_priority` to Solr so that clinically authoritative vocabularies rank
above consumer-facing ones.

**Priority mapping:**

| Priority | Source |
|---|---|
| 1 | SNOMEDCT_US |
| 2 | ICD10CM |
| 3 | NCI |
| 4 | RXNORM |
| 5 | MSH |
| 6 | MEDCIN |
| 7 | LNC |
| 8 | ICD10PCS |
| 9 | OMIM |
| 10 | Others |

**Reads from MySQL:** `solr_preview` — columns `id`, `source_priority`

**Writes to Solr:** `source_priority` field, 5,000 docs per batch, commit at end.

**Can run in parallel with 5a** — they write different fields.

**Run command:**
```bash
python3 scripts/update_solr_source_priority.py
```

**Estimated time:** 30–60 minutes

---

### Step 5c — `update_solr_word_count_and_length.py`

**What it does:** Computes `term_word_count` (number of space-separated words)
and `term_length` (character count) for every Solr document and writes them back
via atomic updates. Unlike 5a/5b this reads directly from Solr using a cursor
— it does not need MySQL.

**Key setting:** `MISSING_ONLY = True` — the script only processes docs where
`term_word_count` is not yet set. This makes it safe to re-run without
re-processing already-populated docs.

**Reads from Solr:** cursor over `*:*` filtered by `-term_word_count:[* TO *]`,
fetching `id` and `term` only, 2,000 docs per batch.

**Writes to Solr:** `term_word_count` and `term_length` fields. Commits every
20,000 docs and at the end.

**Can run in parallel with 5a and 5b** — it writes different fields.

**Run command:**
```bash
python3 scripts/update_solr_word_count_and_length.py
```

**Estimated time:** 1–2 hours (reads from and writes to Solr, slowest of the
post-processing scripts)

**Verify:**
```bash
# Should return 0 after completion
curl "http://localhost:8983/solr/umls_core/select" \
  --data-urlencode "q=*:*" \
  --data-urlencode "rows=0" \
  --data-urlencode "fq=-term_word_count:[* TO *]" \
  --data-urlencode "wt=json"
```

---

## Steps Not Yet Run (optional / future)

### Step 6 — `update_solr_parent_stn_id.py`

**What it does:** Backfills the `parent_stn_id` field in Solr. This is the
numeric ID of the parent node in the STN hierarchy for each term, enabling
ancestor-based hierarchical filtering.

**Current state:** Field exists in the Solr schema but is empty on all
4,271,299 documents. The API fetches it in the `fl` list but does not currently
use it in any filter queries.

**Reads from MySQL:** `solr_preview` — columns `id`, `parent_stn_id`

**Writes to Solr:** `parent_stn_id` field, 5,000 docs per batch, commit at end.

**Run command:**
```bash
python3 scripts/update_solr_parent_stn_id.py
```

**Estimated time:** 30–60 minutes

---

### Step 7 — `update_solr_combined_priority.py`

**What it does:** Writes a single combined TTY+source priority integer to the
`tty_priority` field, replacing the value set by Step 5a. The idea is to bake
both source quality and term type into one number so only `term_length` is
needed as a final tiebreaker.

**Priority table (source, tty) → priority:**

| Priority | Source + TTY |
|---|---|
| 1 | SNOMEDCT_US PT |
| 2 | ICD10CM PT |
| 3 | NCI PT |
| 4 | MEDCIN PT / RXNORM PT |
| 5 | SNOMEDCT_US PN / SNOMEDCT_US SY |
| 6 | NCI SY / ICD10CM SY |
| 7 | MDR PT |
| 8 | MSH PT / RXNORM SY |
| 9 | PDQ PT / MMSL PT |
| 10 | MTH PN |
| 11 | CHV PT |
| 12 | CHV SY |
| 13 | (everything else) |

> **Warning:** This overwrites `tty_priority` set by Step 5a. The current API
> ranking uses separate `tty_priority` and `source_priority` fields and does NOT
> rely on this combined value. Only run this if you intend to switch to a
> combined-field ranking strategy.

**Current state:** `combined_priority` is not in the Solr schema. This script
writes into `tty_priority` (not a new field). It has never been run on the
current index.

**Run command:**
```bash
python3 scripts/update_solr_combined_priority.py
```

**Estimated time:** 30–60 minutes

---

### Audit Tool — `audit_section_terms.py`

**What it does:** Diagnostic only — does not modify any data.

Mines clinical words from the patient JSON files in the repo, groups them by
note section, checks each word against Solr, and reports hit/miss rates and
word-length statistics per section.

**Run command:**
```bash
python3 scripts/audit_section_terms.py --limit 500 \
  --output reports/section_term_audit.json
```

`--limit` controls how many candidate words to test per section.  
Safe to run at any time against the live index.

---

## Exact Commands to Rebuild the Index to Current State

Run these in order. Steps 5a, 5b, and 5c can be run in parallel (separate
terminals) once Step 4 is done.

```bash
# Step 1 — STN hierarchy (PostgreSQL → MySQL)
python3 scripts/populate_stn_tree.py

# Step 2 — Terms table (PostgreSQL + MySQL → MySQL)
python3 scripts/populate_terms.py

# Step 3 — Solr preview staging table (MySQL → MySQL)
python3 scripts/populate_solr_preview.py

# Step 4 — Initial Solr index (MySQL → Solr)
# Ensure index_solr.py is present in scripts/ first
bash scripts/reindex_clean.sh

# Steps 5a, 5b, 5c — Post-process Solr fields
# These can run in parallel in separate terminals
python3 scripts/update_solr_tty_priority.py       # terminal 1
python3 scripts/update_solr_source_priority.py    # terminal 2
python3 scripts/update_solr_word_count_and_length.py  # terminal 3

# Done — index is now in the same state as the current live index
```

---

## What Each Script Reads and Writes — Summary Table

| Script | Reads From | Writes To | Batch Size | Est. Time |
|---|---|---|---|---|
| `populate_stn_tree.py` | PostgreSQL (`mrsty`, `autocomplete_terms`) | MySQL `stn_tree` | all at once | 2–5 min |
| `populate_terms.py` | PostgreSQL (`terms_master`, `autocomplete_terms`) + MySQL `stn_tree` | MySQL `terms` | 10,000 | 30–60 min |
| `populate_solr_preview.py` | MySQL `terms` + `stn_tree` | MySQL `solr_preview` | 10,000 | 30–60 min |
| `reindex_clean.sh` + `index_solr.py` | MySQL `solr_preview` | Solr `umls_core` | varies | 10–30 min |
| `update_solr_tty_priority.py` | MySQL `solr_preview` | Solr `tty_priority` | 10,000 | 30–60 min |
| `update_solr_source_priority.py` | MySQL `solr_preview` | Solr `source_priority` | 5,000 | 30–60 min |
| `update_solr_word_count_and_length.py` | Solr cursor | Solr `term_word_count`, `term_length` | 2,000 | 1–2 hrs |
| `update_solr_parent_stn_id.py` | MySQL `solr_preview` | Solr `parent_stn_id` | 5,000 | 30–60 min |
| `update_solr_combined_priority.py` | MySQL `solr_preview` | Solr `tty_priority` (overwrites) | 5,000 | 30–60 min |
| `audit_section_terms.py` | Repo JSON + Solr | None (read-only) | n/a | minutes |
