"""
classify_buckets.py

Fetches all (source × semantic_type) buckets from Solr with example terms,
then calls DeepInfra DeepSeek-V3 to classify each bucket as:
  KEEP   — OPD-relevant (diagnoses, medications, procedures, labs, chief complaints)
  REMOVE — not relevant to OPD clinical use
  REVIEW — borderline, needs human review

Outputs:
  data/bucket_classifications.json  — full structured results
  data/bucket_classifications.csv   — human-readable summary
  data/solr_delete_queries.txt      — ready-to-run Solr delete queries for REMOVE buckets

Usage:
  DEEPINFRA_API_KEY=<your_key> python3 scripts/classify_buckets.py
"""

import os
import json
import csv
import time
import urllib.request
import urllib.parse
from openai import OpenAI

# ─────────────────────────────────────────
# CONFIG
# ─────────────────────────────────────────
SOLR_URL       = "http://localhost:8983/solr/umls_core"
DEEPINFRA_KEY  = os.getenv("DEEPINFRA_API_KEY", "")
# DeepSeek-V3 on DeepInfra — capable, cheap, great at structured output
MODEL          = "deepseek-ai/DeepSeek-V3"
# How many buckets to send per LLM call (DeepSeek-V3 has 128K context)
BATCH_SIZE     = 80
# Minimum doc count to include a bucket (skip tiny edge-case buckets)
MIN_BUCKET_SIZE = 50
# Examples per bucket shown to the LLM
EXAMPLES_PER_BUCKET = 5

SOURCES = [
    'MSH', 'MEDCIN', 'SNOMEDCT_US', 'NCI', 'LNC', 'RXNORM',
    'OMIM', 'MTH', 'CHV', 'ICD10CM', 'ICD10PCS', 'MDR', 'MMSL', 'CPT', 'PDQ'
]

OUTPUT_DIR = os.path.join(os.path.dirname(__file__), '..', 'data')
os.makedirs(OUTPUT_DIR, exist_ok=True)

JSON_OUT   = os.path.join(OUTPUT_DIR, 'bucket_classifications.json')
CSV_OUT    = os.path.join(OUTPUT_DIR, 'bucket_classifications.csv')
QUERY_OUT  = os.path.join(OUTPUT_DIR, 'solr_delete_queries.txt')


# ─────────────────────────────────────────
# STEP 1 — Fetch all buckets from Solr
# ─────────────────────────────────────────
def fetch_buckets():
    print("Fetching buckets from Solr...")
    buckets = []

    for src in SOURCES:
        url = (f"{SOLR_URL}/select?q=source:{src}"
               f"&rows=0&facet=true&facet.field=semantic_type"
               f"&facet.mincount={MIN_BUCKET_SIZE}&facet.limit=200&wt=json")
        resp = json.loads(urllib.request.urlopen(url).read())
        total = resp['response']['numFound']
        counts = resp['facet_counts']['facet_fields']['semantic_type']
        pairs = [(counts[i], int(counts[i+1])) for i in range(0, len(counts), 2)]
        pairs.sort(key=lambda x: -x[1])

        for sem, cnt in pairs:
            # Fetch random example terms
            q = urllib.parse.quote(f'source:{src} AND semantic_type:"{sem}"')
            url2 = (f"{SOLR_URL}/select?q={q}&rows={EXAMPLES_PER_BUCKET}"
                    f"&fl=term&sort=random_42+asc&wt=json")
            resp2 = json.loads(urllib.request.urlopen(url2).read())
            examples = []
            for doc in resp2['response']['docs']:
                t = doc.get('term', ['?'])
                examples.append(t[0] if isinstance(t, list) else t)

            buckets.append({
                'source': src,
                'source_total': total,
                'semantic_type': sem,
                'count': cnt,
                'examples': examples,
                'decision': None,
                'reason': None,
            })

        print(f"  {src}: {len(pairs)} buckets, {total:,} total docs")

    print(f"\nTotal buckets to classify: {len(buckets)}")
    return buckets


# ─────────────────────────────────────────
# STEP 2 — Build prompt for one batch
# ─────────────────────────────────────────
SYSTEM_PROMPT = """You are a clinical informatics expert classifying medical term buckets for an OPD (outpatient/primary care) autocomplete system used by doctors in India.

Doctors use this autocomplete when typing:
- Diagnoses (diseases, syndromes, injuries, neoplasms)
- Medications (drug names, brand names, formulations)
- Procedures (surgical, diagnostic, therapeutic)
- Investigations (lab tests, imaging)
- Chief complaints (symptoms, signs, findings)

Your task: for each bucket (source vocabulary + semantic type + count + example terms), decide:
  KEEP   — these terms are things a doctor would type in OPD clinical use
  REMOVE — not relevant to OPD (pure biochemistry, genetics research, organisms, chemicals, geographic areas, etc.)
  REVIEW — genuinely borderline — needs a human to sample more terms

Rules:
- Organic Chemicals that are just chemical compound names (not drug names) → REMOVE
- Gene or Genome entries → REMOVE
- Amino Acid sequences, enzymes purely as biochemistry → REMOVE
- Bacteria, fungi, birds, fish, reptiles, plants, mammals (as organisms) → REMOVE
- Geographic areas → REMOVE
- Clinical drugs, brand names, formulations → KEEP
- Pharmacologic substances with real drug names → KEEP
- Diseases, syndromes, injuries, neoplasms → KEEP
- Lab tests, diagnostic procedures → KEEP
- Findings, signs, symptoms → KEEP
- Therapeutic and preventive procedures → KEEP
- Body parts used in clinical context → KEEP
- Intellectual Products (clinical guidelines, scales, scores) — consider REVIEW
- If even 30%+ of the bucket examples look like something a doctor would type → REVIEW not REMOVE

Output: one line per bucket, tab-separated:
SOURCE\tSEMANTIC_TYPE\tDECISION\tREASON

No headers. No extra text. One line per input bucket, in the same order."""


def build_user_message(batch):
    lines = []
    for b in batch:
        ex = ' / '.join(b['examples'][:EXAMPLES_PER_BUCKET])
        lines.append(f"[{b['source']}] [{b['semantic_type']}] n={b['count']:,}")
        lines.append(f"  Examples: {ex}")
        lines.append("")
    return '\n'.join(lines)


# ─────────────────────────────────────────
# STEP 3 — Call DeepInfra
# ─────────────────────────────────────────
def classify_batch(client, batch):
    user_msg = build_user_message(batch)

    response = client.chat.completions.create(
        model=MODEL,
        messages=[
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": user_msg},
        ],
        temperature=0.1,
        max_tokens=4096,
    )

    raw = response.choices[0].message.content.strip()
    lines = [l.strip() for l in raw.splitlines() if l.strip()]

    results = []
    for line in lines:
        parts = line.split('\t')
        if len(parts) >= 3:
            results.append({
                'source': parts[0],
                'semantic_type': parts[1],
                'decision': parts[2].upper().strip(),
                'reason': parts[3] if len(parts) > 3 else '',
            })

    return results, raw


# ─────────────────────────────────────────
# STEP 4 — Match LLM results back to buckets
# ─────────────────────────────────────────
def merge_results(buckets, all_results):
    # Build lookup: (source, semantic_type) -> result
    lookup = {}
    for r in all_results:
        key = (r['source'], r['semantic_type'])
        lookup[key] = r

    for b in buckets:
        key = (b['source'], b['semantic_type'])
        if key in lookup:
            b['decision'] = lookup[key]['decision']
            b['reason']   = lookup[key]['reason']
        else:
            b['decision'] = 'REVIEW'
            b['reason']   = 'No LLM response — needs manual review'

    return buckets


# ─────────────────────────────────────────
# STEP 5 — Write outputs
# ─────────────────────────────────────────
def write_outputs(buckets):
    # JSON
    with open(JSON_OUT, 'w') as f:
        json.dump(buckets, f, indent=2)
    print(f"\nWrote {JSON_OUT}")

    # CSV
    with open(CSV_OUT, 'w', newline='') as f:
        w = csv.writer(f)
        w.writerow(['source', 'semantic_type', 'count', 'decision', 'reason', 'examples'])
        for b in sorted(buckets, key=lambda x: (x['decision'] or 'Z', x['source'])):
            w.writerow([
                b['source'], b['semantic_type'], b['count'],
                b['decision'], b['reason'],
                ' | '.join(b['examples'])
            ])
    print(f"Wrote {CSV_OUT}")

    # Solr delete queries for REMOVE buckets
    removes = [b for b in buckets if b['decision'] == 'REMOVE']
    keeps   = [b for b in buckets if b['decision'] == 'KEEP']
    reviews = [b for b in buckets if b['decision'] == 'REVIEW']

    remove_docs = sum(b['count'] for b in removes)
    keep_docs   = sum(b['count'] for b in keeps)
    review_docs = sum(b['count'] for b in reviews)

    with open(QUERY_OUT, 'w') as f:
        f.write("# Solr delete queries for REMOVE buckets\n")
        f.write(f"# Generated from {len(removes)} buckets totalling {remove_docs:,} docs\n")
        f.write(f"# KEEP: {len(keeps)} buckets, {keep_docs:,} docs\n")
        f.write(f"# REVIEW: {len(reviews)} buckets, {review_docs:,} docs\n\n")
        for b in removes:
            sem = b['semantic_type'].replace('"', '\\"')
            src = b['source']
            f.write(f"# {src} | {sem} | n={b['count']:,} | {b['reason']}\n")
            f.write('{"delete":{"query":"source:' + src + ' AND semantic_type:\\"' + sem + '\\""}}' + '\n\n')

    print(f"Wrote {QUERY_OUT}")

    print(f"\n{'='*60}")
    print(f"  KEEP   : {len(keeps):>4} buckets  {keep_docs:>10,} docs")
    print(f"  REMOVE : {len(removes):>4} buckets  {remove_docs:>10,} docs")
    print(f"  REVIEW : {len(reviews):>4} buckets  {review_docs:>10,} docs")
    print(f"{'='*60}")


# ─────────────────────────────────────────
# MAIN
# ─────────────────────────────────────────
def main():
    if not DEEPINFRA_KEY:
        raise SystemExit("ERROR: Set DEEPINFRA_API_KEY environment variable first.\n"
                         "  export DEEPINFRA_API_KEY=your_key_here")

    client = OpenAI(
        api_key=DEEPINFRA_KEY,
        base_url="https://api.deepinfra.com/v1/openai",
    )

    # Step 1: fetch buckets
    buckets = fetch_buckets()

    # Step 2: classify in batches
    all_results = []
    total_batches = (len(buckets) + BATCH_SIZE - 1) // BATCH_SIZE
    print(f"\nClassifying {len(buckets)} buckets in {total_batches} batches of {BATCH_SIZE}...")

    for i in range(0, len(buckets), BATCH_SIZE):
        batch = buckets[i:i + BATCH_SIZE]
        batch_num = i // BATCH_SIZE + 1
        print(f"\n  Batch {batch_num}/{total_batches} ({len(batch)} buckets)...", end=' ', flush=True)

        try:
            results, raw = classify_batch(client, batch)
            all_results.extend(results)
            print(f"got {len(results)} results")
        except Exception as e:
            print(f"ERROR: {e}")
            # Mark batch as REVIEW on failure
            for b in batch:
                all_results.append({
                    'source': b['source'],
                    'semantic_type': b['semantic_type'],
                    'decision': 'REVIEW',
                    'reason': f'API error: {e}',
                })

        # Small pause between batches to avoid rate limits
        if i + BATCH_SIZE < len(buckets):
            time.sleep(1)

    # Step 3: merge back
    buckets = merge_results(buckets, all_results)

    # Step 4: write outputs
    write_outputs(buckets)


if __name__ == '__main__':
    main()
