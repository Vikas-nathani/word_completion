"""
Random sample search test: picks 10k terms from /tmp/cc_terms.tsv,
searches each against the /search endpoint, and logs every result.

Run:
    python tests/test_api_sample.py
or:
    python tests/test_api_sample.py --api http://localhost:8004 --rows 10 --workers 20

Output log: /tmp/api_sample_results.jsonl  (one JSON object per term)
Summary:    /tmp/api_sample_summary.json
"""

import argparse
import asyncio
import json
import random
import time
import urllib.parse
from pathlib import Path

import httpx

TERMS_FILE = Path("/tmp/cc_terms.tsv")
LOG_FILE = Path("/tmp/api_sample_results.jsonl")
SUMMARY_FILE = Path("/tmp/api_sample_summary.json")

SAMPLE_SIZE = 10_000
DEFAULT_API = "http://localhost:8004"
DEFAULT_ROWS = 10
DEFAULT_WORKERS = 10


def load_terms(path: Path) -> list[str]:
    lines = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            term = line.strip()
            # skip blanks and comment-like lines starting with #
            if term and not term.startswith("#"):
                lines.append(term)
    return lines


async def search_term(
    client: httpx.AsyncClient,
    api_base: str,
    term: str,
    rows: int,
) -> dict:
    url = f"{api_base}/search"
    params = {"q": term, "rows": rows}
    t0 = time.perf_counter()
    try:
        resp = await client.get(url, params=params, timeout=15)
        elapsed_ms = round((time.perf_counter() - t0) * 1000, 2)
        if resp.status_code != 200:
            return {
                "query": term,
                "status": resp.status_code,
                "error": resp.text[:200],
                "elapsed_ms": elapsed_ms,
                "total": 0,
                "results": [],
            }
        data = resp.json()
        return {
            "query": term,
            "status": 200,
            "elapsed_ms": elapsed_ms,
            "total": data.get("total", 0),
            "spell_corrected": data.get("spell_corrected", False),
            "results": [
                {
                    "term": r.get("term"),
                    "tty": r.get("tty"),
                    "source": r.get("source"),
                    "semantic_type": r.get("semantic_type"),
                    "concept_id": r.get("concept_id"),
                    "code": r.get("code"),
                }
                for r in data.get("results", [])
            ],
        }
    except Exception as exc:
        elapsed_ms = round((time.perf_counter() - t0) * 1000, 2)
        return {
            "query": term,
            "status": -1,
            "error": str(exc),
            "elapsed_ms": elapsed_ms,
            "total": 0,
            "results": [],
        }


async def run(api_base: str, rows: int, workers: int) -> None:
    all_terms = load_terms(TERMS_FILE)
    print(f"Loaded {len(all_terms):,} terms from {TERMS_FILE}")

    sample = random.sample(all_terms, min(SAMPLE_SIZE, len(all_terms)))
    print(f"Sampled {len(sample):,} terms — searching with {workers} concurrent workers")

    semaphore = asyncio.Semaphore(workers)
    log_fh = LOG_FILE.open("w", encoding="utf-8")

    stats = {
        "total_queries": len(sample),
        "zero_results": 0,
        "errors": 0,
        "spell_corrected": 0,
        "total_elapsed_ms": 0.0,
        "min_elapsed_ms": float("inf"),
        "max_elapsed_ms": 0.0,
    }
    done = 0

    async def bounded(term: str):
        async with semaphore:
            return await search_term(client, api_base, term, rows)

    t_start = time.perf_counter()
    async with httpx.AsyncClient() as client:
        # Process in batches to avoid creating 10k coroutines at once
        batch_size = workers * 4
        for batch_start in range(0, len(sample), batch_size):
            batch = sample[batch_start: batch_start + batch_size]
            tasks = [asyncio.create_task(bounded(t)) for t in batch]
            for coro in asyncio.as_completed(tasks):
                result = await coro
                log_fh.write(json.dumps(result, ensure_ascii=False) + "\n")

            # update stats
            e = result["elapsed_ms"]
            stats["total_elapsed_ms"] += e
            stats["min_elapsed_ms"] = min(stats["min_elapsed_ms"], e)
            stats["max_elapsed_ms"] = max(stats["max_elapsed_ms"], e)
            if result.get("status", -1) not in (200,):
                stats["errors"] += 1
            if result.get("total", 0) == 0:
                stats["zero_results"] += 1
            if result.get("spell_corrected"):
                stats["spell_corrected"] += 1

            done += 1
            if done % 500 == 0 or done == len(sample):
                print(f"  {done}/{len(sample)} done  "
                      f"(zero_results={stats['zero_results']}, errors={stats['errors']})")

    log_fh.close()
    wall_s = round(time.perf_counter() - t_start, 2)

    stats["avg_elapsed_ms"] = round(stats["total_elapsed_ms"] / max(1, stats["total_queries"]), 2)
    stats["wall_seconds"] = wall_s
    stats["queries_per_second"] = round(stats["total_queries"] / max(0.001, wall_s), 1)
    if stats["min_elapsed_ms"] == float("inf"):
        stats["min_elapsed_ms"] = 0.0

    SUMMARY_FILE.write_text(json.dumps(stats, indent=2))

    print("\n=== SUMMARY ===")
    print(f"  Total queries   : {stats['total_queries']:,}")
    print(f"  Zero results    : {stats['zero_results']:,}  "
          f"({100*stats['zero_results']/max(1,stats['total_queries']):.1f}%)")
    print(f"  Spell corrected : {stats['spell_corrected']:,}")
    print(f"  Errors          : {stats['errors']:,}")
    print(f"  Avg latency     : {stats['avg_elapsed_ms']} ms")
    print(f"  Min / Max       : {stats['min_elapsed_ms']} / {stats['max_elapsed_ms']} ms")
    print(f"  Wall time       : {wall_s}s  ({stats['queries_per_second']} q/s)")
    print(f"\n  Results log  -> {LOG_FILE}")
    print(f"  Summary JSON -> {SUMMARY_FILE}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--api", default=DEFAULT_API)
    parser.add_argument("--rows", type=int, default=DEFAULT_ROWS)
    parser.add_argument("--workers", type=int, default=DEFAULT_WORKERS)
    args = parser.parse_args()
    asyncio.run(run(args.api, args.rows, args.workers))
