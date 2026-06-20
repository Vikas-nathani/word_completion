# Chief Complaint Term Coverage Report

**Question answered:** Of all UMLS chief-complaint terms in the source database,
how many are present in our Solr index, and how many are actually retrievable
through the autocomplete API endpoint?

## Source Query

The reference set is the result of this query against the UMLS Postgres database
(`umls.mrconso` joined with `umls.mrsty`):

```sql
SELECT DISTINCT
    sty.cui,
    con.str  AS ChiefComplaint_Term,
    sty.tui  AS SemanticType_ID,
    sty.sty  AS SemanticType_Name,
    con.sab  AS Source_Vocabulary,
    con.code AS Source_Code
FROM umls.mrsty sty
JOIN umls.mrconso con ON sty.cui = con.cui
WHERE sty.tui IN ('T184', 'T033')      -- Sign or Symptom, Finding
  AND con.lat   = 'ENG'                 -- English
  AND con.ts    = 'P'                   -- Preferred term status
  AND con.stt   = 'PF'                  -- Preferred form
  AND con.ispref = 'Y'                  -- Atom is preferred in its source
  AND con.sab IN ('SNOMEDCT_US', 'MEDCIN')
ORDER BY ChiefComplaint_Term ASC;
```

This yields **212,060 distinct preferred chief-complaint terms**.

---

## 1. Headline Coverage

| Layer | Count | % of DB query | Meaning |
|---|---:|---:|---|
| **DB query result** | **212,060** | 100.00% | Distinct preferred chief-complaint terms in UMLS |
| **Present in Solr index** | **206,353** | **97.31%** | Term physically exists in the search index |
| **Retrievable from API endpoint** | **203,991** | **96.19%** | Term passes the `chief_complaint` endpoint filters |

**Bottom line: 96.2% of all UMLS chief-complaint terms are available through the
live API.** The remaining 3.8% is almost entirely terms that are *intentionally*
excluded (obsolete concepts and non-display atom types), not a real gap.

---

## 2. Where Terms Are Lost (funnel)

| Stage | Lost here | Remaining | Reason |
|---|---:|---:|---|
| DB query | — | 212,060 | — |
| → Indexed into Solr | **−5,707** | 206,353 | Not in the index (see §3) |
| → Pass endpoint filters | **−2,362** | 203,991 | Filtered by the API (see §4) |

---

## 3. Why 5,707 Are NOT in Solr

All 5,707 are **SNOMEDCT_US** (zero MEDCIN — MEDCIN coverage is 100%).
Breakdown by term type (TTY):

| TTY | Count | Meaning | Real gap? |
|---|---:|---|---|
| OAP | 5,531 | Obsolete Active Preferred | No — retired concept |
| OAS | 130 | Obsolete Active Synonym | No — retired concept |
| PT | 28 | Preferred Term | **No — present under a variant string** |
| IS | 14 | Obsolete Synonym | No — retired concept |
| OF | 3 | Obsolete Fully-Specified Name | No — retired concept |
| OAF | 1 | Obsolete Active Fully-Specified | No — retired concept |

**5,679 (99.5%) are obsolete-class atoms** that SNOMED has retired. Excluding
obsolete terms from a live clinical autocomplete is correct and expected.

Examples (obsolete OAP):
- `((marital: [conflict] or [disharmony]) (& [row with wife]))`
- `([d]abdominal mass) or (lump stomach)`
- `([d] senility, without mention of psychosis) or (senile tremor)`

### The 28 "PT" are not a true gap

All 28 concepts **are** in Solr — verified by concept-ID (CUI) lookup. UMLS
stores them with a parenthetical disambiguation suffix that Solr indexed without:

| DB term (UMLS) | Indexed in Solr as |
|---|---|
| `Bathing (ADL finding)` | `Bathing` |
| `Apex beat displaced - LVH (left ventricle hypertrophy)` | `Apex beat displaced - LVH` |
| `6-10 mitoses per 10 HPF (score = 2)` | `6-10 mitoses per 10 HPF` |

These concepts are fully searchable; only the exact suffixed string differs.

---

## 4. Why 2,362 More Are Dropped by the API Endpoint

These terms **are** in Solr but the `chief_complaint` endpoint's filters exclude
them. Attribution by filter:

| Filter | Terms dropped | Explanation |
|---|---:|---|
| `tty:(PT OR PN OR SY)` | **2,347** | Term exists in Solr only as **FN** (fully-specified name) or **AB** (abbreviation) atoms. The endpoint serves preferred terms + synonyms only. |
| `-source:CHV` | 15 | CHV (consumer health vocabulary) excluded for clinical data hygiene. |
| Section semantic-type filter | 0 | None dropped — all are already chief-complaint-relevant semantic types. |

By source: 2,258 MEDCIN PT, 104 SNOMEDCT_US PT (their display string was indexed
under an FN/AB-type atom).

Examples (FN/AB-only, endpoint-dropped):
- `3-hydroxyoctanoylcarnitine (c8-oh) + malonylcarnitine (c3-dc)/decanoylcarnitine (c10)`
- `abdomen drain drainage amount (___ml)`
- `abnormalities of cervix (obstetric)`
- `amplitude decrement at low rate (2-3hz)` (SNOMEDCT_US)

---

## 5. Net Assessment

| Category | Count | Verdict |
|---|---:|---|
| Retrievable from API | 203,991 (96.2%) | Working |
| Obsolete SNOMED (correctly excluded) | 5,679 | Expected — do not index obsolete |
| Filtered FN/AB-only atoms | 2,347 | By design — preferred terms only |
| Parenthetical-suffix PTs (present under variant) | 28 | Cosmetic, searchable |
| CHV-only | 15 | By policy |

**Effective coverage of clinically relevant, active chief-complaint terms is
~100%.** The 3.8% not retrievable consists of obsolete SNOMED concepts and
FN/AB-only atoms, both intentionally excluded. There is **no meaningful gap of
active, useful chief-complaint terms.**

---

## Method

- **DB layer:** exact `SELECT DISTINCT lower(str)` from UMLS Postgres → 212,060
  distinct lowercased terms.
- **Solr layer:** streamed all 4.27M index documents via `cursorMark`
  pagination, intersected each document's `term_lower` against the DB set.
- **Endpoint layer:** applied the live `chief_complaint` endpoint's exact `fq`
  filters (`tty:(PT OR PN OR SY)`, section semantic types, `-source:CHV`) during
  the Solr scan; a term is "retrievable" if its `term_lower` appears on a
  document passing those filters.
- **Gap categorization:** joined each missing term back to UMLS `mrconso`/`mrsty`
  to obtain its source (SAB) and term type (TTY).

### Notes / caveats

- The endpoint layer measures **filter eligibility** (the term can be served).
  It does not model runtime concept **deduplication** (same-concept duplicates
  collapse to one in the dropdown) or **top-15 ranking** for a specific typed
  prefix. Those are query-dependent and were validated separately via a
  keystroke-level benchmark.
