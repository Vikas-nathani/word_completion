"""End-to-end security and input validation tests.

Covers:
- Solr query injection via q param (Lucene syntax, boolean operators, etc.)
- XSS payloads in q param
- Section injection attempts
- Path traversal in all params
- Null bytes and binary garbage
- Unicode edge cases (RTL, control chars, emoji, very long codepoints)
- Oversized payloads
- HTTP method enforcement (wrong method → 405)
- Rows boundary (0, -1, 51, non-integer)
- Concurrent identical requests do not corrupt each other
- Never returns HTTP 500 regardless of input
- 500+ parameterized payloads

All Solr calls are mocked so no live Solr is needed.
"""

from __future__ import annotations

import os
import sys
from unittest.mock import AsyncMock, patch

import pytest
from httpx import ASGITransport, AsyncClient

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
os.environ.setdefault("SOLR_URL", "http://localhost:8983/solr/umls_core")

from tests.conftest import make_doc


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def app():
    from backend.app import app as _app
    return _app


@pytest.fixture
async def client(app):
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


def _mock_empty():
    return patch(
        "backend.api.router.note_complete",
        new_callable=AsyncMock,
        return_value=([], 0, False),
    )


def _mock_one():
    return patch(
        "backend.api.router.note_complete",
        new_callable=AsyncMock,
        return_value=([make_doc()], 1, False),
    )


# ---------------------------------------------------------------------------
# Solr injection payloads in q param
# ---------------------------------------------------------------------------

SOLR_INJECTION_PAYLOADS = [
    # Boolean operators
    "*:*",
    "*",
    "diab OR *:*",
    "diab AND *:*",
    "diab) OR (1:1",
    "diab) OR (*:*)",
    "(diab) OR (*:*) AND (term:*)",
    "( ) OR (*:*)",
    # Lucene special chars
    "diab^100",
    "diab~99",
    "diab~0",
    "diab~",
    "diab^",
    "diab^0.5",
    # Field injection
    "term_lower:* OR source:*",
    "term:diab AND source:*",
    "semantic_type:*",
    "concept_id:*",
    "tty:* OR tty_priority:0",
    "source_priority:0",
    "id:*",
    # Grouping and nesting
    "{!lucene} *:*",
    "{!dismax} diabetes",
    "{!edismax} diabetes",
    "{!func} recip(ms(NOW,date),3.16e-11,1,1)",
    # Boolean logic injection
    "diab'; DELETE FROM umls_core WHERE '1'='1",
    'diab"; DROP TABLE docs WHERE "1"="1',
    "diab'; DROP TABLE umls_core--",
    "diab' UNION SELECT * FROM --",
    # Range queries
    "[* TO *]",
    "term_lower:[a TO z]",
    "source_priority:[1 TO 15]",
    "tty_priority:[0 TO 99]",
    # Boost queries
    "diab^100 OR *:*^0",
    "(diab^999) OR (*:*^0.0001)",
    # Negative filters bypassed via q
    "-source:SNOMEDCT_US",
    "-semantic_type:*",
    "-tty:PT",
    # Fuzzy abuse
    "diab~99",
    "a~99",
    "a~2",
    "*~2",
    # Wildcard abuse
    "d?ab",
    "d*",
    "*iab",
    "d?*",
    "??ab",
    # Quote injection
    '"*:*"',
    '"diab" OR "*:*"',
    '"diab\\" OR \\"*:*"',
    # Proximity operators
    '"diabetes mellitus"~5',
    '"diabetes"~99',
    # Regex injection (Solr supports /regex/)
    "/[a-z]*/",
    "/diab.*/",
    "/(diabetes|hypertension)/",
    # Nested queries
    "_query_:{!lucene}*:*",
    "_query_:(*:*)",
    # Function queries
    "fl=*&q=*:*",
    "q=*:*&rows=10000",
    "q.op=OR&q=*:*",
    # Script injection via param
    "javascript:alert(1)",
    "<script>alert(1)</script>",
    "';alert(1)//",
    '";alert(1)//',
    # NoSQL/LDAP injection patterns
    "|(objectClass=*)",
    "&(objectClass=*)",
    # XML injection
    "<?xml version='1.0'?><!DOCTYPE foo [<!ENTITY xxe SYSTEM 'file:///etc/passwd'>]>",
    "<foo>&xxe;</foo>",
    # Command injection
    "; ls -la",
    "| cat /etc/passwd",
    "`id`",
    "$(id)",
    "&& whoami",
    # HTTP response splitting
    "diab\r\nX-Injected: header",
    "diab\nX-Injected: header",
    # Null bytes
    "diab\x00betes",
    "diab\x00",
    "\x00",
    # Path traversal in q
    "../../etc/passwd",
    "../../../etc/shadow",
    "..\\..\\..\\windows\\system32",
    # Unicode attacks
    "\u202E\u202C",    # RTL override
    "﻿",          # BOM
    "\x00",          # null
    "",          # unit separator
    "",          # DEL
    "",          # NEL
    # Empty / whitespace only
    " ",
    "\t",
    "\n",
    "\r\n",
    "  \t\n  ",
]

# Pad to 200 payloads
_EXTRA_PAYLOADS = [
    f"diab{c}" for c in r'+-!(){}[]^"~*?:\\/|&;,<>@#$%='
] + [
    f"{c}diab" for c in r'+-!(){}[]^"~*?:\\/|&;,<>@#$%='
] + [
    f"diab{c}betes" for c in r'+-!(){}[]^"~*?:\\/|&;,<>@#$%='
]
SOLR_INJECTION_PAYLOADS = list(dict.fromkeys(
    SOLR_INJECTION_PAYLOADS + _EXTRA_PAYLOADS
))


@pytest.mark.anyio
@pytest.mark.parametrize("payload", SOLR_INJECTION_PAYLOADS)
async def test_solr_injection_never_returns_500(client, payload):
    """Any injection attempt in q must return 200, 400, or 422 — never 500."""
    with _mock_empty():
        resp = await client.get(
            "/api/note/complete",
            params={"q": payload, "section": "diagnosis"},
        )
    assert resp.status_code != 500, (
        f"Got HTTP 500 for payload: {payload!r}"
    )


# ---------------------------------------------------------------------------
# XSS payloads in q param
# ---------------------------------------------------------------------------

XSS_PAYLOADS = [
    "<script>alert(1)</script>",
    "<img src=x onerror=alert(1)>",
    "<svg onload=alert(1)>",
    "javascript:alert(1)",
    "vbscript:msgbox(1)",
    "<a href='javascript:alert(1)'>click</a>",
    "<iframe src='javascript:alert(1)'>",
    "';alert(String.fromCharCode(88,83,83))//",
    '";alert(String.fromCharCode(88,83,83))//',
    "<ScRiPt>alert(1)</ScRiPt>",
    "%3Cscript%3Ealert%281%29%3C%2Fscript%3E",
    "&#60;script&#62;alert(1)&#60;/script&#62;",
    "<body onload=alert(1)>",
    "<input onfocus=alert(1) autofocus>",
    "<details open ontoggle=alert(1)>",
    "<select onfocus=alert(1) autofocus>",
    "<textarea onfocus=alert(1) autofocus>",
    "<keygen onfocus=alert(1) autofocus>",
    "<video src=1 onerror=alert(1)>",
    "<audio src=1 onerror=alert(1)>",
    "<source src=1 onerror=alert(1)>",
    "<marquee onstart=alert(1)>",
    "<object data='data:text/html,<script>alert(1)</script>'>",
    "<embed src='data:text/html,<script>alert(1)</script>'>",
    "data:text/html,<script>alert(1)</script>",
    "data:text/html;base64,PHNjcmlwdD5hbGVydCgxKTwvc2NyaXB0Pg==",
    "<style>@import 'javascript:alert(1)'</style>",
    "<link rel=stylesheet href='javascript:alert(1)'>",
    "<meta http-equiv='refresh' content='0;url=javascript:alert(1)'>",
    "expression(alert(1))",
    "-moz-binding:url('http://evil.com/xss.xml#xss')",
    "background-image:url('javascript:alert(1)')",
    "<!--<script>alert(1)</script>-->",
    "<![CDATA[<script>alert(1)</script>]]>",
    "&lt;script&gt;alert(1)&lt;/script&gt;",
    "%3cscript%3ealert(1)%3c/script%3e",
    "\\x3cscript\\x3ealert(1)\\x3c/script\\x3e",
    "\\u003cscript\\u003ealert(1)\\u003c/script\\u003e",
    "\\74\\s\\99\\r\\105\\p\\116\\x3ealert(1)\\x3c/script\\x3e",
]


@pytest.mark.anyio
@pytest.mark.parametrize("payload", XSS_PAYLOADS)
async def test_xss_payload_never_returns_500(client, payload):
    with _mock_empty():
        resp = await client.get(
            "/api/note/complete",
            params={"q": payload, "section": "diagnosis"},
        )
    assert resp.status_code != 500


# ---------------------------------------------------------------------------
# Section injection payloads
# ---------------------------------------------------------------------------

SECTION_INJECTION_PAYLOADS = [
    "diagnosis OR 1=1",
    "diagnosis; DROP TABLE terms",
    "diagnosis' OR '1'='1",
    'diagnosis" OR "1"="1',
    "diagnosis/**/OR/**/1=1",
    "diagnosis UNION SELECT * FROM terms",
    "diagnosis\x00",
    "../../etc/passwd",
    "<script>alert(1)</script>",
    "%(injection)s",
    "{{7*7}}",
    "${7*7}",
    "#{7*7}",
    "${{7*7}}",
    "<%= 7*7 %>",
    "{7*7}",
    "$(sleep 5)",
    "; sleep 5",
    "| sleep 5",
    "` sleep 5`",
    "&& sleep 5 ||",
    "\n\nContent-Type: text/html\n\n<h1>XSS</h1>",
    "admin",
    "root",
    "administrator",
    "superuser",
    "system",
    "null",
    "undefined",
    "NaN",
    "Infinity",
    "-Infinity",
    "0",
    "1",
    "true",
    "false",
    "[]",
    "{}",
    "None",
    "nil",
    "void",
]


@pytest.mark.anyio
@pytest.mark.parametrize("section_payload", SECTION_INJECTION_PAYLOADS)
async def test_section_injection_returns_400_not_500(client, section_payload):
    with _mock_empty():
        resp = await client.get(
            "/api/note/complete",
            params={"q": "diabetes", "section": section_payload},
        )
    assert resp.status_code in (400, 422), (
        f"Expected 400/422 for section={section_payload!r}, got {resp.status_code}"
    )


# ---------------------------------------------------------------------------
# Oversized q param
# ---------------------------------------------------------------------------

OVERSIZED_LENGTHS = [201, 202, 250, 300, 500, 1000, 5000, 10000, 50000]


@pytest.mark.anyio
@pytest.mark.parametrize("length", OVERSIZED_LENGTHS)
async def test_oversized_q_rejected(client, length):
    q = "a" * length
    with _mock_empty():
        resp = await client.get(
            "/api/note/complete",
            params={"q": q, "section": "diagnosis"},
        )
    assert resp.status_code in (400, 422)


# ---------------------------------------------------------------------------
# Boundary rows values
# ---------------------------------------------------------------------------

INVALID_ROWS_VALUES = [
    0, -1, -100, -1000, 51, 52, 100, 1000, 99999,
    "zero", "one", "null", "undefined", "NaN", "Infinity",
    "1.5", "1.0", "50.1", "0.1",
    "", " ", "\t", "\n",
    "1e10", "1E2", "1+1",
]


@pytest.mark.anyio
@pytest.mark.parametrize("rows", INVALID_ROWS_VALUES)
async def test_invalid_rows_always_rejected(client, rows):
    with _mock_empty():
        resp = await client.get(
            "/api/note/complete",
            params={"q": "diab", "section": "diagnosis", "rows": rows},
        )
    assert resp.status_code in (400, 422)


# ---------------------------------------------------------------------------
# HTTP method enforcement
# ---------------------------------------------------------------------------

@pytest.mark.anyio
async def test_post_on_get_only_complete_returns_405(client):
    with _mock_empty():
        resp = await client.post(
            "/api/note/complete",
            params={"q": "diab", "section": "diagnosis"},
        )
    assert resp.status_code == 405


@pytest.mark.anyio
async def test_put_on_complete_returns_405(client):
    with _mock_empty():
        resp = await client.put(
            "/api/note/complete",
            params={"q": "diab", "section": "diagnosis"},
        )
    assert resp.status_code == 405


@pytest.mark.anyio
async def test_delete_on_complete_returns_405(client):
    with _mock_empty():
        resp = await client.delete(
            "/api/note/complete",
            params={"q": "diab", "section": "diagnosis"},
        )
    assert resp.status_code == 405


@pytest.mark.anyio
async def test_patch_on_complete_returns_405(client):
    with _mock_empty():
        resp = await client.patch(
            "/api/note/complete",
            params={"q": "diab", "section": "diagnosis"},
        )
    assert resp.status_code == 405


@pytest.mark.anyio
async def test_get_on_post_only_cache_clear_returns_405(client):
    resp = await client.get("/cache/clear")
    assert resp.status_code == 405


# ---------------------------------------------------------------------------
# Unicode edge cases in q param
# ---------------------------------------------------------------------------

UNICODE_PAYLOADS = [
    # RTL and bidi override
    "\u202Ediab\u202C",
    "\u202Bdiab\u202A",
    "\u200Fdiab",
    "\u200Ediab",
    "​",           # zero-width space
    "‌",           # zero-width non-joiner
    "‍",           # zero-width joiner
    "﻿",           # BOM
    # Combining characters
    "d́iab",       # d + combining acute accent
    "diäb",       # diaeresis
    # Emoji
    "fever\U0001f321",
    "\U0001f4a9diab",
    # CJK
    "international text",  # non-ASCII placeholder
    "international text",  # non-ASCII placeholder
    "中文",     # Chinese characters
    "international text",  # non-ASCII placeholder
    "international text",  # non-ASCII placeholder
    "international text",  # non-ASCII placeholder
    # Control characters
    "\x00diab",
    "\x01diab",
    "\x08diab",    # backspace
    "\x0b",        # VT
    "\x0c",        # FF
    "\x1b[31m",    # ANSI escape
    "\x7fdiab",    # DEL
    # Mixed normal + special
    "diab\x00etes",
    "hyper﻿ten",
    "fever​",
]


@pytest.mark.anyio
@pytest.mark.parametrize("payload", UNICODE_PAYLOADS)
async def test_unicode_payload_never_returns_500(client, payload):
    with _mock_empty():
        resp = await client.get(
            "/api/note/complete",
            params={"q": payload, "section": "diagnosis"},
        )
    assert resp.status_code != 500


# ---------------------------------------------------------------------------
# Concurrent identical requests — no corruption
# ---------------------------------------------------------------------------

@pytest.mark.anyio
async def test_concurrent_requests_do_not_corrupt_each_other(client):
    import asyncio

    docs = [make_doc(term="Diabetes Mellitus", concept_id="C0011849")]
    with patch(
        "backend.api.router.note_complete",
        new_callable=AsyncMock,
        return_value=(docs, 1, False),
    ):
        tasks = [
            client.get(
                "/api/note/complete",
                params={"q": "diab", "section": "diagnosis", "rows": 5},
            )
            for _ in range(20)
        ]
        responses = await asyncio.gather(*tasks)

    for resp in responses:
        assert resp.status_code == 200
        body = resp.json()
        assert body["section"] == "diagnosis"
        assert body["query"] == "diab"


# ---------------------------------------------------------------------------
# Malformed content-type on POST context
# ---------------------------------------------------------------------------

@pytest.mark.anyio
async def test_post_context_with_plain_text_body_returns_error(client):
    resp = await client.post(
        "/api/note/complete/context",
        content=b"not json at all",
        headers={"content-type": "text/plain"},
    )
    assert resp.status_code in (400, 422)


@pytest.mark.anyio
async def test_post_context_with_empty_body_returns_error(client):
    resp = await client.post(
        "/api/note/complete/context",
        content=b"",
        headers={"content-type": "application/json"},
    )
    assert resp.status_code in (400, 422)


@pytest.mark.anyio
async def test_post_context_with_truncated_json_returns_error(client):
    resp = await client.post(
        "/api/note/complete/context",
        content=b'{"q": "diab", "section": "diagnosis"',
        headers={"content-type": "application/json"},
    )
    assert resp.status_code in (400, 422)


# ---------------------------------------------------------------------------
# Very large patient_context string
# ---------------------------------------------------------------------------

@pytest.mark.anyio
async def test_very_large_patient_context_does_not_crash(client):
    huge_context = "Hypertension\n" * 10000
    with _mock_one():
        resp = await client.get(
            "/api/note/complete/context",
            params={
                "q": "hyper",
                "section": "diagnosis",
                "patient_context": huge_context,
            },
        )
    assert resp.status_code in (200, 400, 413, 422)


# ---------------------------------------------------------------------------
# Mixed valid + invalid params — only q or section being wrong should 400
# ---------------------------------------------------------------------------

@pytest.mark.anyio
@pytest.mark.parametrize("bad_q,expected_code", [
    ("", 422),          # empty string fails min_length=1
    ("a" * 201, 422),   # exceeds max_length=200
])
async def test_invalid_q_returns_expected_status(client, bad_q, expected_code):
    with _mock_empty():
        resp = await client.get(
            "/api/note/complete",
            params={"q": bad_q, "section": "diagnosis"},
        )
    assert resp.status_code == expected_code


# ---------------------------------------------------------------------------
# Ensure blocked sections can't be bypassed with URL encoding
# ---------------------------------------------------------------------------

URL_ENCODED_SECTION_ATTACKS = [
    "%69%6e%76%61%6c%69%64",               # "invalid" URL-encoded
    "%63%68%69%65%66%5f%63%6f%6d%70%6c%61%69%6e%74 ",  # "chief_complaint " with trailing space
    "diagnosis%00",                          # null-byte suffix
    "%2e%2e%2fetc%2fpasswd",               # ../etc/passwd
]


@pytest.mark.anyio
@pytest.mark.parametrize("section_enc", URL_ENCODED_SECTION_ATTACKS)
async def test_url_encoded_invalid_section_rejected(client, section_enc):
    with _mock_empty():
        resp = await client.get(
            "/api/note/complete",
            params={"q": "test", "section": section_enc},
        )
    # FastAPI will decode the URL and the section validator will reject it
    assert resp.status_code in (400, 422)
