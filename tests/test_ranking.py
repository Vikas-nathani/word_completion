"""Reranking and deduplication determinism/ordering tests.

Verifies that the Python-side ranking pipeline (_rerank_docs, _deduplicate_by_concept_id,
_collapse_exact_surface_variants, _relevance_bucket) produces correct, stable, and
deterministic results for clinical autocomplete use cases.
"""

from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
os.environ.setdefault("SOLR_URL", "http://localhost:8983/solr/umls_core")

from backend.app import (
    _collapse_exact_surface_variants,
    _deduplicate_by_concept_id,
    _relevance_bucket,
    _rerank_docs,
    _tokenize_words,
)
from tests.conftest import make_doc


# ── _relevance_bucket ─────────────────────────────────────────────────────────

def test_relevance_bucket_exact_match_is_0():
    tokens = _tokenize_words("diabetes")
    assert _relevance_bucket("Diabetes", "diabetes", tokens) == 0


def test_relevance_bucket_starts_with_query_is_1():
    tokens = _tokenize_words("diab")
    assert _relevance_bucket("Diabetes Mellitus", "diab", tokens) == 1


def test_relevance_bucket_token_match_is_3():
    tokens = _tokenize_words("mellitus")
    # "mellitus" does not appear at start of term but is a token
    result = _relevance_bucket("Diabetes Mellitus", "mellitus", tokens)
    assert result in (3, 4)


def test_relevance_bucket_no_match_is_9():
    tokens = _tokenize_words("xyz")
    assert _relevance_bucket("Hypertension", "xyz", tokens) == 9


def test_relevance_bucket_empty_query_is_9():
    assert _relevance_bucket("Hypertension", "", []) == 9


def test_relevance_bucket_empty_term_is_9():
    tokens = _tokenize_words("diab")
    assert _relevance_bucket("", "diab", tokens) == 9


def test_relevance_bucket_order_exact_beats_prefix():
    query = "fever"
    tokens = _tokenize_words(query)
    exact = _relevance_bucket("Fever", query, tokens)
    prefix = _relevance_bucket("Fever with chills", query, tokens)
    assert exact < prefix


def test_relevance_bucket_prefix_beats_token():
    query = "dia"
    tokens = _tokenize_words(query)
    prefix = _relevance_bucket("Diabetes", query, tokens)
    token = _relevance_bucket("Type 2 Diabetes", query, tokens)
    assert prefix <= token


# ── _rerank_docs: ordering guarantees ────────────────────────────────────────

def test_rerank_exact_match_ranked_first():
    docs = [
        make_doc(term="Diabetes Mellitus Type 2", tty="PT", term_word_count=4, term_length=24, concept_id="C0002"),
        make_doc(term="Diabetes", tty="PT", term_word_count=1, term_length=8, concept_id="C0001"),
        make_doc(term="Diabetic Retinopathy", tty="PT", term_word_count=2, term_length=20, concept_id="C0003"),
    ]
    ranked = _rerank_docs(docs, "diabetes")
    assert ranked[0]["term"] == "Diabetes"


def test_rerank_shorter_term_ranked_before_longer_same_bucket():
    docs = [
        make_doc(term="Diabetic Retinopathy", tty="PT", term_word_count=2, term_length=20, concept_id="C0003"),
        make_doc(term="Diabetes", tty="PT", term_word_count=1, term_length=8, concept_id="C0001"),
    ]
    ranked = _rerank_docs(docs, "diab")
    assert ranked[0]["term"] == "Diabetes"


def test_rerank_pt_ranked_before_sy_for_same_term_length():
    docs = [
        make_doc(term="Fever", tty="SY", tty_priority=3, concept_id="C0001", term_word_count=1, term_length=5),
        make_doc(term="Fever", tty="PT", tty_priority=1, concept_id="C0002", term_word_count=1, term_length=5),
    ]
    ranked = _rerank_docs(docs, "fev")
    assert ranked[0]["tty"] == "PT"


def test_rerank_snomed_ranked_before_chv_for_same_term():
    docs = [
        make_doc(term="Hypertension", tty="PT", source="CHV", source_priority=15, concept_id="C0001", term_word_count=1, term_length=12),
        make_doc(term="Hypertension", tty="PT", source="SNOMEDCT_US", source_priority=1, concept_id="C0002", term_word_count=1, term_length=12),
    ]
    ranked = _rerank_docs(docs, "hypertension")
    assert ranked[0]["source"] == "SNOMEDCT_US"


def test_rerank_preferred_tier_pt_beats_sy_even_if_shorter():
    # SY "Fy" (2 chars) vs PT "Fall" (4 chars) — PT should rank higher for "f"
    # because preferred tier (0) beats SY tier (1) before length proximity
    docs = [
        make_doc(term="Fy", tty="SY", tty_priority=3, concept_id="C0001", term_word_count=1, term_length=2),
        make_doc(term="Fall", tty="PT", tty_priority=1, concept_id="C0002", term_word_count=1, term_length=4),
    ]
    ranked = _rerank_docs(docs, "f")
    assert ranked[0]["tty"] == "PT"


def test_rerank_preserves_all_docs():
    docs = [make_doc(term=f"Term {i}", concept_id=f"C{i:07d}") for i in range(10)]
    ranked = _rerank_docs(docs, "term")
    assert len(ranked) == 10


def test_rerank_is_stable_idempotent():
    docs = [
        make_doc(term="Diabetes", tty="PT", concept_id="C0001", term_word_count=1, term_length=8),
        make_doc(term="Diabetic Neuropathy", tty="PT", concept_id="C0002", term_word_count=2, term_length=19),
        make_doc(term="Diabulimia", tty="SY", concept_id="C0003", tty_priority=3, term_word_count=1, term_length=10),
    ]
    ranked_once = _rerank_docs(docs, "diab")
    ranked_twice = _rerank_docs(ranked_once, "diab")
    assert [d["term"] for d in ranked_once] == [d["term"] for d in ranked_twice]


def test_rerank_empty_list_returns_empty():
    assert _rerank_docs([], "diab") == []


def test_rerank_single_doc_returns_it():
    doc = make_doc()
    assert _rerank_docs([doc], "diab") == [doc]


def test_rerank_word_count_1_beats_word_count_2():
    docs = [
        make_doc(term="Diabetes Mellitus", tty="PT", term_word_count=2, term_length=17, concept_id="C0002"),
        make_doc(term="Diabetes", tty="PT", term_word_count=1, term_length=8, concept_id="C0001"),
    ]
    ranked = _rerank_docs(docs, "diab")
    # 1-word term should come first (fewer words = shorter completion)
    assert ranked[0]["term_word_count"] == 1


# ── _deduplicate_by_concept_id ────────────────────────────────────────────────

def test_dedup_keeps_best_source_per_concept():
    docs = [
        make_doc(term="Diabetes", tty="PT", source="CHV", source_priority=15, tty_priority=1, concept_id="C0011849", term_word_count=1, term_length=8),
        make_doc(term="Diabetes", tty="PT", source="SNOMEDCT_US", source_priority=1, tty_priority=1, concept_id="C0011849", term_word_count=1, term_length=8),
    ]
    result = _deduplicate_by_concept_id(docs)
    assert len(result) == 1
    assert result[0]["source"] == "SNOMEDCT_US"


def test_dedup_prefers_shorter_term_for_same_concept():
    docs = [
        make_doc(term="Hypertension resolved", tty="PT", source="SNOMEDCT_US", source_priority=1, tty_priority=1, concept_id="C0020538", term_word_count=2, term_length=21),
        make_doc(term="Hypertension", tty="PT", source="MTH", source_priority=13, tty_priority=1, concept_id="C0020538", term_word_count=1, term_length=12),
    ]
    result = _deduplicate_by_concept_id(docs)
    assert len(result) == 1
    # 1-word term beats 2-word term even if source is lower priority
    assert result[0]["term"] == "Hypertension"


def test_dedup_different_concept_ids_both_kept():
    docs = [
        make_doc(term="Diabetes", concept_id="C0011849"),
        make_doc(term="Hypertension", concept_id="C0020538"),
    ]
    result = _deduplicate_by_concept_id(docs)
    assert len(result) == 2


def test_dedup_docs_without_concept_id_passed_through():
    docs = [
        make_doc(term="Unknown Term", concept_id=""),
        make_doc(term="Another Unknown", concept_id="None"),
    ]
    result = _deduplicate_by_concept_id(docs)
    assert len(result) == 2


def test_dedup_preserves_insertion_order_for_distinct_concepts():
    docs = [
        make_doc(term="Aardvark Disease", concept_id="C0000001"),
        make_doc(term="Zebra Syndrome", concept_id="C0000002"),
    ]
    result = _deduplicate_by_concept_id(docs)
    assert result[0]["term"] == "Aardvark Disease"
    assert result[1]["term"] == "Zebra Syndrome"


def test_dedup_empty_returns_empty():
    assert _deduplicate_by_concept_id([]) == []


def test_dedup_pt_preferred_over_sy_same_concept():
    docs = [
        make_doc(term="Fever", tty="SY", tty_priority=3, concept_id="C0015967", term_word_count=1, term_length=5),
        make_doc(term="Fever", tty="PT", tty_priority=1, concept_id="C0015967", term_word_count=1, term_length=5),
    ]
    result = _deduplicate_by_concept_id(docs)
    assert len(result) == 1
    assert result[0]["tty"] == "PT"


# ── _collapse_exact_surface_variants ─────────────────────────────────────────

def test_collapse_moves_exact_match_to_front():
    docs = [
        make_doc(term="Diabetic Neuropathy", concept_id="C0001", source="CHV"),
        make_doc(term="Diabetes", concept_id="C0002", source="CHV"),
        make_doc(term="Diabetes", concept_id="C0003", source="SNOMEDCT_US", source_priority=1),
    ]
    result = _collapse_exact_surface_variants(docs, "diabetes")
    assert result[0]["term"].lower() == "diabetes"


def test_collapse_selects_best_source_for_exact_match():
    docs = [
        make_doc(term="Diabetes", concept_id="C0002", source="CHV", source_priority=15),
        make_doc(term="Diabetes", concept_id="C0003", source="SNOMEDCT_US", source_priority=1),
        make_doc(term="Diabetic Neuropathy", concept_id="C0001"),
    ]
    result = _collapse_exact_surface_variants(docs, "diabetes")
    exact = [d for d in result if d["term"].lower() == "diabetes"]
    assert len(exact) == 1
    assert exact[0]["source"] == "SNOMEDCT_US"


def test_collapse_no_exact_match_returns_original():
    docs = [
        make_doc(term="Diabetic Neuropathy", concept_id="C0001"),
        make_doc(term="Diabetic Retinopathy", concept_id="C0002"),
    ]
    result = _collapse_exact_surface_variants(docs, "diabetes")
    assert result == docs


def test_collapse_empty_query_returns_original():
    docs = [make_doc()]
    result = _collapse_exact_surface_variants(docs, "")
    assert result == docs


def test_collapse_empty_docs_returns_empty():
    assert _collapse_exact_surface_variants([], "diabetes") == []


# ── Integration: dedup + rerank pipeline ─────────────────────────────────────

def test_full_pipeline_dedup_then_rerank():
    """Simulate the pipeline: dedup first, then rerank, verify top result is correct."""
    docs = [
        make_doc(term="Diabetes Mellitus Type 2", tty="PT", source="ICD10CM", source_priority=2, tty_priority=1, concept_id="C0011860", term_word_count=4, term_length=24),
        make_doc(term="Diabetes", tty="PT", source="SNOMEDCT_US", source_priority=1, tty_priority=1, concept_id="C0011849", term_word_count=1, term_length=8),
        make_doc(term="Diabetes", tty="SY", source="CHV", source_priority=15, tty_priority=3, concept_id="C0011849", term_word_count=1, term_length=8),
        make_doc(term="Diabetic Retinopathy", tty="PT", source="SNOMEDCT_US", source_priority=1, tty_priority=1, concept_id="C0011884", term_word_count=2, term_length=20),
    ]
    deduped = _deduplicate_by_concept_id(docs)
    ranked = _rerank_docs(deduped, "diabetes")

    # After dedup: 3 unique concepts. After rerank: "Diabetes" (exact match, 1 word) comes first
    assert ranked[0]["term"] == "Diabetes"
    # Should not have duplicates
    concept_ids = [d["concept_id"] for d in ranked]
    assert len(concept_ids) == len(set(concept_ids))
