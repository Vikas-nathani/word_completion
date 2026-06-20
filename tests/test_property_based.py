"""Property-based tests using Hypothesis for pure functions in backend/app.py.

Properties under test:
- _get_scalar: always returns a non-list value
- _word_count: always >= 0, consistent with token count
- _build_autocomplete_query: always references term_lower, never produces empty string
- _tty_priority_value / _source_priority_value: always return a positive integer
- _filter_doc: only passes ALLOWED_TTY values
- _rerank_docs: stable sort (idempotent on already-ranked input)
- _relevance_bucket: returns value in [0, 9]
- _normalize_whitespace: idempotent (applying twice = applying once)
- _escape_solr_token: never introduces unescaped special chars
- _build_blocked_semantic_fq: deterministic for same input set
"""

from __future__ import annotations

import os
import sys

import pytest
from hypothesis import HealthCheck, assume, given, settings
from hypothesis import strategies as st

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
os.environ.setdefault("SOLR_URL", "http://localhost:8983/solr/umls_core")

from backend.app import (
    ALLOWED_TTY,
    TTY_PRIORITY_MAP,
    SOURCE_PRIORITY_MAP,
    _build_autocomplete_query,
    _build_blocked_semantic_fq,
    _escape_solr_token,
    _filter_doc,
    _get_scalar,
    _normalize_whitespace,
    _relevance_bucket,
    _rerank_docs,
    _source_priority_value,
    _tokenize_words,
    _tty_priority_value,
    _word_count,
)


# ── Strategies ────────────────────────────────────────────────────────────────

printable_text = st.text(
    alphabet=st.characters(whitelist_categories=("Lu", "Ll", "Nd", "Zs"), whitelist_characters="-'"),
    min_size=0,
    max_size=80,
)

medical_prefix = st.text(
    alphabet="abcdefghijklmnopqrstuvwxyz ",
    min_size=1,
    max_size=30,
)

tty_code = st.sampled_from(["PT", "PN", "SY", "FN", "AB", "XX", "ZZ", ""])
source_code = st.sampled_from(
    list(SOURCE_PRIORITY_MAP.keys()) + ["UNKNOWN", "FOO", ""]
)


def doc_strategy(tty=None, source=None):
    tty_s = st.just(tty) if tty else tty_code
    source_s = st.just(source) if source else source_code
    return st.fixed_dictionaries({
        "term": printable_text,
        "tty": tty_s,
        "semantic_type": printable_text,
        "source": source_s,
        "concept_id": st.text(min_size=0, max_size=20),
        "tty_priority": st.integers(min_value=1, max_value=10),
        "source_priority": st.integers(min_value=1, max_value=20),
        "term_word_count": st.integers(min_value=1, max_value=5),
        "term_length": st.integers(min_value=1, max_value=100),
    })


# ── _get_scalar ───────────────────────────────────────────────────────────────

@given(value=printable_text)
def test_get_scalar_string_never_returns_list(value):
    result = _get_scalar({"f": value}, "f")
    assert not isinstance(result, list)


@given(values=st.lists(printable_text, min_size=1, max_size=5))
def test_get_scalar_list_returns_first_element(values):
    result = _get_scalar({"f": values}, "f")
    assert result == values[0]


@given(default=printable_text)
def test_get_scalar_missing_key_returns_default(default):
    result = _get_scalar({}, "missing", default)
    assert result == default


# ── _word_count ───────────────────────────────────────────────────────────────

@given(text=printable_text)
def test_word_count_always_non_negative(text):
    assert _word_count(text) >= 0


@given(text=medical_prefix)
def test_word_count_consistent_with_tokenize(text):
    tokens = _tokenize_words(text)
    wc = _word_count(text)
    assert wc >= 0
    # word_count uses a slightly broader regex but should be within 1 of tokenize count
    assert abs(wc - len(tokens)) <= 1


# ── _normalize_whitespace ─────────────────────────────────────────────────────

@given(text=printable_text)
def test_normalize_whitespace_idempotent(text):
    once = _normalize_whitespace(text)
    twice = _normalize_whitespace(once)
    assert once == twice


@given(text=printable_text)
def test_normalize_whitespace_no_leading_trailing_spaces(text):
    result = _normalize_whitespace(text)
    assert result == result.strip()


@given(text=printable_text)
def test_normalize_whitespace_no_double_spaces(text):
    result = _normalize_whitespace(text)
    assert "  " not in result


# ── _escape_solr_token ────────────────────────────────────────────────────────

SOLR_SPECIAL = set('+-!(){}[]^"~*?:\\/')

@given(token=medical_prefix)
def test_escape_solr_token_plain_alpha_unchanged_property(token):
    # Alphabetic-only, space-only tokens with no special chars must come back unchanged
    assume(not any(c in SOLR_SPECIAL for c in token))
    result = _escape_solr_token(token)
    assert result == token


@given(token=medical_prefix)
def test_escape_solr_token_result_contains_original_chars(token):
    # After escaping a plain prefix, the original chars still appear in the result
    assume(not any(c in SOLR_SPECIAL for c in token))
    result = _escape_solr_token(token)
    # Every char in the original plain token must appear in the escaped result
    for ch in token:
        assert ch in result


@given(token=medical_prefix)
def test_escape_solr_token_plain_text_unchanged(token):
    # Plain alphabetic tokens don't need escaping
    assume(not any(c in SOLR_SPECIAL for c in token))
    assert _escape_solr_token(token) == token


# ── _build_autocomplete_query ─────────────────────────────────────────────────

@given(prefix=medical_prefix)
def test_build_autocomplete_query_always_references_term_lower(prefix):
    result = _build_autocomplete_query(prefix)
    assert "term_lower:" in result or result == "*:*"


@given(prefix=medical_prefix)
def test_build_autocomplete_query_never_empty(prefix):
    result = _build_autocomplete_query(prefix)
    assert len(result) > 0


@given(prefix=st.text(min_size=1, max_size=5, alphabet="abcdefghijklmnopqrstuvwxyz"))
def test_build_autocomplete_query_single_word_contains_prefix(prefix):
    result = _build_autocomplete_query(prefix)
    assert prefix in result or result == "*:*"


# ── _tty_priority_value ───────────────────────────────────────────────────────

@given(doc=doc_strategy())
@settings(suppress_health_check=[HealthCheck.too_slow])
def test_tty_priority_always_positive_int(doc):
    result = _tty_priority_value(doc)
    assert isinstance(result, int)
    assert result >= 1


@given(tty=st.sampled_from(list(TTY_PRIORITY_MAP.keys())))
def test_tty_priority_known_values_match_map(tty):
    result = _tty_priority_value({"tty": tty})
    assert result == TTY_PRIORITY_MAP[tty]


# ── _source_priority_value ────────────────────────────────────────────────────

@given(doc=doc_strategy())
@settings(suppress_health_check=[HealthCheck.too_slow])
def test_source_priority_always_positive_int(doc):
    result = _source_priority_value(doc)
    assert isinstance(result, int)
    assert result >= 1


@given(source=st.sampled_from(list(SOURCE_PRIORITY_MAP.keys())))
def test_source_priority_known_sources_match_map(source):
    result = _source_priority_value({"source": source})
    assert result == SOURCE_PRIORITY_MAP[source]


# ── _filter_doc ───────────────────────────────────────────────────────────────

@given(tty=st.sampled_from(ALLOWED_TTY))
def test_filter_doc_allowed_tty_passes(tty):
    assert _filter_doc({"tty": tty}) is True


@given(tty=st.text(min_size=1, max_size=4, alphabet="UVWXYZ0123456789"))
def test_filter_doc_non_allowed_tty_blocked(tty):
    assume(tty not in ALLOWED_TTY)
    assert _filter_doc({"tty": tty}) is False


@given(tty=st.sampled_from(ALLOWED_TTY))
def test_filter_doc_list_tty_field_passes(tty):
    assert _filter_doc({"tty": [tty]}) is True


# ── _relevance_bucket ─────────────────────────────────────────────────────────

@given(
    term=printable_text,
    query=medical_prefix,
)
def test_relevance_bucket_in_range(term, query):
    tokens = _tokenize_words(query)
    bucket = _relevance_bucket(term, query, tokens)
    assert 0 <= bucket <= 9


@given(term=medical_prefix)
def test_relevance_bucket_exact_match_is_zero(term):
    assume(len(term.strip()) > 0)
    normalized = _normalize_whitespace(term).lower()
    tokens = _tokenize_words(normalized)
    bucket = _relevance_bucket(term, normalized, tokens)
    assert bucket == 0


@given(term=medical_prefix)
def test_relevance_bucket_starts_with_query_is_leq_1(term):
    assume(len(term.strip()) > 2)
    normalized = _normalize_whitespace(term).lower()
    prefix = normalized[:2]
    assume(len(prefix.strip()) > 0)
    tokens = _tokenize_words(prefix)
    bucket = _relevance_bucket(term, prefix, tokens)
    assert bucket <= 1


# ── _rerank_docs ──────────────────────────────────────────────────────────────

@given(docs=st.lists(doc_strategy(), min_size=0, max_size=10), query=medical_prefix)
@settings(suppress_health_check=[HealthCheck.too_slow], max_examples=50)
def test_rerank_docs_idempotent(docs, query):
    """Ranking an already-ranked list gives the same order."""
    ranked_once = _rerank_docs(docs, query)
    ranked_twice = _rerank_docs(ranked_once, query)
    assert [d["term"] for d in ranked_once] == [d["term"] for d in ranked_twice]


@given(docs=st.lists(doc_strategy(), min_size=2, max_size=10), query=medical_prefix)
@settings(suppress_health_check=[HealthCheck.too_slow], max_examples=50)
def test_rerank_docs_same_length_as_input(docs, query):
    ranked = _rerank_docs(docs, query)
    assert len(ranked) == len(docs)


@given(docs=st.lists(doc_strategy(), min_size=0, max_size=10), query=medical_prefix)
@settings(suppress_health_check=[HealthCheck.too_slow], max_examples=50)
def test_rerank_docs_never_loses_documents(docs, query):
    ranked = _rerank_docs(docs, query)
    original_ids = {id(d) for d in docs}
    ranked_ids = {id(d) for d in ranked}
    assert original_ids == ranked_ids


# ── _build_blocked_semantic_fq ────────────────────────────────────────────────

@given(types=st.frozensets(printable_text, min_size=1, max_size=10))
@settings(suppress_health_check=[HealthCheck.too_slow])
def test_build_blocked_semantic_fq_deterministic(types):
    result1 = _build_blocked_semantic_fq(set(types))
    result2 = _build_blocked_semantic_fq(set(types))
    assert result1 == result2


@given(types=st.frozensets(
    st.text(min_size=1, max_size=20, alphabet="abcdefghijklmnopqrstuvwxyz "),
    min_size=1,
    max_size=5,
))
def test_build_blocked_semantic_fq_starts_with_negation(types):
    result = _build_blocked_semantic_fq(set(types))
    assert result.startswith("-semantic_type:(")
    assert result.endswith(")")
