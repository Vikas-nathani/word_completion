#!/usr/bin/env python3
"""Audit section-specific clinical words against the Solr index.

The script mines candidate words from the repo's patient JSON documents,
groups them by note section, and checks whether each word is present in Solr.
It reports:
  - how many candidate words were tested per section
  - how many were found in Solr
  - average and median word length
  - the longest candidate words

The repository uses ``advice`` for the "general normal" style section, so the
CLI also accepts ``general`` as an alias for ``advice``.
"""

from __future__ import annotations

import asyncio
import argparse
import json
import os
import re
import sys
import statistics
from collections import Counter, defaultdict
from pathlib import Path
from typing import Iterable

import httpx
from dotenv import load_dotenv

# Make the repo root importable when the script is run directly from scripts/.
REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from backend.services.section_config import get_section_fq


load_dotenv()

SOLR_URL = os.getenv("SOLR_URL", "http://localhost:8983/solr/umls_core")
DEFAULT_LIMIT = 500
REQUEST_TIMEOUT = httpx.Timeout(8.0, connect=3.0)
MAX_CONCURRENCY = 20

SECTION_ALIASES = {
    "general": "advice",
    "general_normal": "advice",
}

SECTION_ORDER = [
    "chief_complaint",
    "diagnosis",
    "investigations",
    "medications",
    "procedures",
    "advice",
]

STOPWORDS = {
    "a",
    "an",
    "and",
    "are",
    "as",
    "at",
    "be",
    "by",
    "for",
    "from",
    "has",
    "have",
    "he",
    "her",
    "his",
    "i",
    "in",
    "is",
    "it",
    "no",
    "not",
    "of",
    "on",
    "or",
    "patient",
    "she",
    "the",
    "their",
    "there",
    "they",
    "to",
    "was",
    "were",
    "with",
    "you",
}

WORD_RE = re.compile(r"[A-Za-z][A-Za-z0-9'-]*")
SECTION_HEADER_RE = re.compile(r"^#{1,3}\s+(.+?)\s*$")


def normalize_section(section: str) -> str:
    section = section.strip().lower().replace(" ", "_")
    return SECTION_ALIASES.get(section, section)


def tokenize(text: str) -> list[str]:
    tokens = []
    for raw in WORD_RE.findall(text.lower()):
        token = raw.strip("-'")
        if len(token) < 2 or token in STOPWORDS:
            continue
        tokens.append(token)
    return tokens


def collect_strings(value) -> Iterable[str]:
    """Yield every useful display/text-like string from a nested JSON value."""
    if isinstance(value, str):
        if value.strip():
            yield value
        return

    if isinstance(value, list):
        for item in value:
            yield from collect_strings(item)
        return

    if isinstance(value, dict):
        for key in ("display", "text", "value", "title", "name"):
            item = value.get(key)
            if isinstance(item, str) and item.strip():
                yield item
        for nested_key in (
            "code",
            "medication",
            "reasonReference",
            "result",
            "conclusion",
            "procedure",
            "carePlan",
            "immunization",
            "imagingStudy",
        ):
            nested = value.get(nested_key)
            if nested is not None:
                yield from collect_strings(nested)
        for nested in value.values():
            if isinstance(nested, (dict, list)):
                yield from collect_strings(nested)


def iter_note_texts(encounter: dict) -> Iterable[str]:
    for report in encounter.get("diagnosticReports") or []:
        presented = report.get("presentedForm") or {}
        text = presented.get("text")
        if isinstance(text, str) and text.strip():
            yield text


def extract_heading_sections(note_text: str) -> dict[str, str]:
    """Return the raw body text for each heading in a note."""
    sections: dict[str, list[str]] = defaultdict(list)
    current_heading = None

    for line in note_text.splitlines():
        match = SECTION_HEADER_RE.match(line.strip())
        if match:
            current_heading = match.group(1).strip().lower()
            continue
        if current_heading:
            sections[current_heading].append(line)

    return {heading: "\n".join(lines).strip() for heading, lines in sections.items()}


def extract_candidates_from_note(note_text: str) -> dict[str, list[str]]:
    """Mine section-specific candidate phrases from a note body."""
    sections = extract_heading_sections(note_text)
    candidates: dict[str, list[str]] = defaultdict(list)

    chief = sections.get("chief complaint", "")
    if chief:
        candidates["chief_complaint"].append(chief)

    assessment = sections.get("assessment and plan", "")
    if assessment:
        candidates["diagnosis"].extend(
            re.findall(r"presenting with\s+(.+?)(?:\.\s*|\n|##|\Z)", assessment, flags=re.I | re.S)
        )
        candidates["diagnosis"].extend(
            re.findall(r"([A-Za-z][A-Za-z ,/-]+?)\s*\((?:finding|disorder|situation|problem|condition)\)", assessment)
        )
        candidates["medications"].extend(
            re.findall(r"prescribed the following medications:\s*(.+?)(?:\n\n|The patient was|##|\Z)", assessment, flags=re.I | re.S)
        )
        candidates["procedures"].extend(
            re.findall(r"following procedures were conducted:\s*(.+?)(?:\n\n|The patient was|##|\Z)", assessment, flags=re.I | re.S)
        )
        candidates["investigations"].extend(
            re.findall(r"following lab reports were completed:\s*(.+?)(?:\n\n|The patient was|##|\Z)", assessment, flags=re.I | re.S)
        )
        candidates["advice"].extend(
            re.findall(r"placed on a careplan:\s*(.+?)(?:\n\n|##|\Z)", assessment, flags=re.I | re.S)
        )

    meds = sections.get("medications", "")
    if meds:
        candidates["medications"].append(meds)

    return candidates


def extract_section_terms_from_record(record: dict) -> dict[str, list[str]]:
    """Extract candidate phrases for each section from one JSON record."""
    section_phrases: dict[str, list[str]] = defaultdict(list)

    for encounter in record.get("encounters") or []:
        # Structured data from the FHIR-ish record.
        for cond in encounter.get("conditions") or []:
            section_phrases["diagnosis"].extend(collect_strings(cond))

        for medication in encounter.get("medications") or []:
            section_phrases["medications"].extend(collect_strings(medication))

        for procedure in encounter.get("procedures") or []:
            section_phrases["procedures"].extend(collect_strings(procedure))

        for report in encounter.get("diagnosticReports") or []:
            section_phrases["investigations"].extend(collect_strings(report))

        for obs in encounter.get("observations") or []:
            section_phrases["investigations"].extend(collect_strings(obs))

        for care_plan in encounter.get("carePlans") or []:
            section_phrases["advice"].extend(collect_strings(care_plan))

        for note_text in iter_note_texts(encounter):
            note_phrases = extract_candidates_from_note(note_text)
            for section, phrases in note_phrases.items():
                section_phrases[section].extend(phrases)

    return section_phrases


def build_candidate_terms(records: list[dict], limit: int) -> dict[str, list[str]]:
    """Build the top N unique terms per section from the provided records."""
    frequencies: dict[str, Counter[str]] = {section: Counter() for section in SECTION_ORDER}

    for record in records:
        section_phrases = extract_section_terms_from_record(record)
        for section, phrases in section_phrases.items():
            normalized_section = normalize_section(section)
            if normalized_section not in frequencies:
                continue
            for phrase in phrases:
                for token in tokenize(phrase):
                    frequencies[normalized_section][token] += 1

    top_terms: dict[str, list[str]] = {}
    for section in SECTION_ORDER:
        ranked = [term for term, _ in frequencies[section].most_common(limit)]
        top_terms[section] = ranked
    return top_terms


def load_records(paths: list[Path]) -> list[dict]:
    records: list[dict] = []
    for path in paths:
        data = json.loads(path.read_text())
        if isinstance(data, list):
            records.extend(item for item in data if isinstance(item, dict))
        elif isinstance(data, dict):
            records.append(data)
    return records


async def check_term(client: httpx.AsyncClient, section: str, term: str, sem: asyncio.Semaphore) -> dict:
    fq = get_section_fq(section)
    params = {
        "q": f"term_lower:{term}",
        "fq": fq,
        "rows": 1,
        "wt": "json",
        "fl": "id,term,term_length",
    }

    async with sem:
        try:
            response = await client.get(f"{SOLR_URL}/select", params=params)
            response.raise_for_status()
            payload = response.json()
            docs = payload.get("response", {}).get("docs", [])
            return {
                "term": term,
                "found": bool(docs),
                "letters": len(term),
                "solr_term": docs[0].get("term") if docs else None,
                "solr_term_length": docs[0].get("term_length") if docs else None,
            }
        except Exception as exc:  # pragma: no cover - network/runtime safety
            return {
                "term": term,
                "found": False,
                "letters": len(term),
                "error": str(exc),
            }


async def check_terms(client: httpx.AsyncClient, section: str, terms: list[str]) -> list[dict]:
    sem = asyncio.Semaphore(MAX_CONCURRENCY)
    tasks = [check_term(client, section, term, sem) for term in terms]
    return await asyncio.gather(*tasks)


def summarize(section: str, rows: list[dict]) -> dict:
    total = len(rows)
    found = sum(1 for row in rows if row.get("found"))
    lengths = [row["letters"] for row in rows if "letters" in row]
    avg_length = round(statistics.mean(lengths), 2) if lengths else 0.0
    median_length = round(statistics.median(lengths), 2) if lengths else 0.0
    longest = sorted(
        (row for row in rows if "letters" in row),
        key=lambda row: (-row["letters"], row["term"]),
    )[:10]

    return {
        "section": section,
        "tested": total,
        "found": found,
        "missing": total - found,
        "found_rate_pct": round((found / total * 100.0) if total else 0.0, 2),
        "avg_letters": avg_length,
        "median_letters": median_length,
        "longest_terms": longest,
    }


async def main() -> int:
    parser = argparse.ArgumentParser(description="Audit 500 candidate words per clinical note section.")
    parser.add_argument(
        "--limit",
        type=int,
        default=DEFAULT_LIMIT,
        help="How many candidate terms to test per section (default: 500).",
    )
    parser.add_argument(
        "--input",
        nargs="*",
        default=[
            "tests/sample_patient.json",
            "data/791427b4-9cc4-8bcc-3fee-e3e14b6d3fea_clean.json",
        ],
        help="JSON files to mine for candidate terms.",
    )
    parser.add_argument(
        "--output",
        default="",
        help="Optional JSON file path for the full report.",
    )
    args = parser.parse_args()

    paths = [Path(item) for item in args.input if Path(item).exists()]
    if not paths:
        raise SystemExit("No valid JSON input files were found.")

    records = load_records(paths)
    candidates = build_candidate_terms(records, args.limit)

    print(f"Loaded {len(records)} records from {len(paths)} file(s).")
    print(f"Solr: {SOLR_URL}")
    print()

    report = {"solr_url": SOLR_URL, "limit": args.limit, "sections": {}}

    async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT) as client:
        for section in SECTION_ORDER:
            terms = candidates.get(section, [])[: args.limit]
            rows = await check_terms(client, section, terms)
            summary = summarize(section, rows)
            report["sections"][section] = {
                "summary": summary,
                "rows": rows,
            }

            print(f"[{section}]")
            print(f"  tested: {summary['tested']}")
            print(f"  found: {summary['found']} ({summary['found_rate_pct']}%)")
            print(f"  missing: {summary['missing']}")
            print(f"  avg letters: {summary['avg_letters']}")
            print(f"  median letters: {summary['median_letters']}")
            longest_preview = ", ".join(
                f"{row['term']} ({row['letters']})" for row in summary["longest_terms"][:5]
            )
            print(f"  longest terms: {longest_preview}")
            print()

    if args.output:
        output_path = Path(args.output)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(json.dumps(report, indent=2, sort_keys=True))
        print(f"Wrote report to {output_path}")

    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
