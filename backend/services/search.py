
"""Minimal section-aware search pipeline for note completion.

This module reuses app.py ranking behavior and only adds positive semantic-type
filtering based on the selected note section.
"""

from __future__ import annotations

import importlib
from typing import Optional

import httpx


class _LegacyAppProxy:
    def __getattr__(self, name: str):
        module = importlib.import_module("backend.app")
        return getattr(module, name)


legacy_app = _LegacyAppProxy()

from .section_config import (
    CHV_EXCLUDED_SECTIONS,
    MEDICATION_TRUSTED_SOURCES,
    get_section_fq,
)


async def note_complete(
    q: str,
    section: str,
    rows: int,
    fuzzy: bool,
    source: Optional[str],
    tty: Optional[str],
) -> tuple[list[dict], int, bool]:
    """Execute note completion search using app.py helpers plus section semantic fq."""
    del source, tty

    fq_list = [
        fq for fq in (
            # Note suggestions surface preferred terms and synonyms (PT, PN, SY)
            # but not fully-specified names or abbreviation atoms (FN, AB).
            "tty:(PT OR PN OR SY)",
            legacy_app.TERM_WORD_COUNT_FQ,
            get_section_fq(section),
        ) if fq is not None
    ]

    if section in CHV_EXCLUDED_SECTIONS or section == "procedures":
        fq_list.append("-source:CHV")

    if section == "medications":
        fq_list.append("source:(" + " OR ".join(MEDICATION_TRUSTED_SOURCES) + ")")

    # Keep hormone-related concepts available only in medications while
    # explicitly excluding them from all other note sections.
    if section != "medications":
        fq_list.append('-semantic_type:"Hormone"')
        fq_list.append('-semantic_type:"Biologically Active Substance"')

    effective_query_text = legacy_app._effective_query_text_for_ranking(q)
    # Sort order:
    #   1. term_word_count        fewest words first
    #   2. preferred tier         PT/PN (preferred terms) above SY (synonyms), so
    #                             cryptic short abbreviations stored as SY do not
    #                             outrank concise preferred terms for short
    #                             prefixes (e.g. "Fall" beats "Fy" for q="f").
    #                             Implemented as map(tty_priority,1,2,0,1): PT=1
    #                             and PN=2 map to tier 0, everything else tier 1.
    #   3. term_length            shortest completion first within a tier, so
    #                             "Diabetes" beats "Diabulimia" for q="diab".
    #   4. tty_priority           PT before PN within the preferred tier.
    #   5. source_priority        best clinical source as the final tiebreaker.
    #
    # We deliberately do NOT use a query() score boost: term_lower is a tokenized
    # prefix field, so query("term_lower:f") scores by relevance and rewards long
    # phrases that repeat the prefix, which (as the primary key) pushes concise
    # terms out of the fetch window for small `rows`, producing rows-dependent
    # ordering. A fully-typed long term still surfaces without a boost because it
    # matches very few docs and is therefore always inside the fetch window.
    relevance_sort = (
        "term_word_count asc, map(tty_priority,1,2,0,1) asc, term_length asc, "
        "tty_priority asc, source_priority asc"
    )
    parsed = {
        "q": [legacy_app._build_autocomplete_query(q)],
        "wt": ["json"],
        "sort": [relevance_sort],
        "fl": [
            "id,term,tty,tty_priority,semantic_type,source,source_priority,code,concept_id,is_abbreviation,stn_path,parent_stn,parent_stn_id,depth_level,term_word_count,term_length"
        ],
        "fq": fq_list,
    }
    async with httpx.AsyncClient() as client:
        docs, solr_num_found, _ = await legacy_app._fetch_filtered_docs(
            client=client,
            parsed=parsed,
            requested_start=0,
            requested_rows=max(1, rows),
        )

    filtered_docs = [doc for doc in docs if legacy_app._filter_doc(doc)]
    if section in ("chief_complaint", "diagnosis"):
        filtered_docs = [
            doc for doc in filtered_docs
            if "ctcae" not in str(legacy_app._get_scalar(doc, "term", "")).lower()
        ]
    deduped_docs = legacy_app._deduplicate_by_concept_id(filtered_docs)
    ranked_docs = legacy_app._rerank_docs(deduped_docs, query_text=effective_query_text)

    spell_corrected = False
    if not ranked_docs and fuzzy:
        fuzzy_docs, fuzzy_num_found = await legacy_app._fuzzy_search_fallback(
            raw_q=q,
            requested_rows=max(1, rows),
            fl_value=parsed["fl"][0],
        )
        if fuzzy_docs:
            ranked_docs = fuzzy_docs
            solr_num_found = fuzzy_num_found
            spell_corrected = True

    return ranked_docs[:rows], solr_num_found, spell_corrected
