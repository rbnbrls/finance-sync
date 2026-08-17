"""Regression tests: provider credentials never leak via redaction helpers.

Story t_eab3b15a — tenant-scoped audit logging and credential redaction.

These tests pin the contract of the shared scrubbing helpers
(``redact_text`` / ``sanitize_error``) that the connection-management API,
audit service and error paths rely on: explicit credential values,
well-known secret shapes (JWTs, bearer tokens, ``sk_live_``-style keys,
``key=value`` pairs) and long opaque runs must never survive into a
log line, API response or persisted error message.
"""

# pyright: basic

from __future__ import annotations

from finance_sync.utils.redaction import REDACTED, redact_text, sanitize_error

# ── redact_text: explicit credential values ────────────────────────────


def test_redacts_exact_secret_value() -> None:
    text = "Authentication failed with key abc123secret"
    safe = redact_text(text, ["abc123secret"])
    assert "abc123secret" not in safe
    assert REDACTED in safe


def test_redacts_multiple_secrets() -> None:
    text = "key1=val-one and key2=val-two"
    safe = redact_text(text, ["val-one", "val-two"])
    assert "val-one" not in safe
    assert "val-two" not in safe
    assert safe.count(REDACTED) == 2


def test_redacts_slash_stripped_variant() -> None:
    # Secrets containing slashes (e.g. API tokens) may appear without
    # the slash after URL parsing.
    safe = redact_text("token abcd/efgh in response", ["abcd/efgh"])
    assert "abcd/efgh" not in safe
    assert "abcdefgh" not in safe


def test_redacts_url_encoded_variant() -> None:
    safe = redact_text("query key=a%20b%2Fc", ["a b/c"])
    assert "a%20b%2Fc" not in safe


def test_redacts_secret_when_embedded_in_longer_text() -> None:
    safe = redact_text("url=https://x?token=abc123secret&x=1", ["abc123secret"])
    assert "abc123secret" not in safe


def test_short_secrets_are_left_alone() -> None:
    # Values shorter than 4 chars are too ambiguous to scrub verbatim.
    safe = redact_text("pin 123", ["123"])
    assert "123" in safe
    assert REDACTED not in safe


def test_empty_text_passthrough() -> None:
    assert redact_text("") == ""
    assert redact_text("", ["secret"]) == ""


def test_no_secrets_is_identity() -> None:
    text = "plain message without secrets"
    assert redact_text(text) == text


# ── redact_text: secret shapes ─────────────────────────────────────────


def test_redacts_jwt_shape() -> None:
    jwt = "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0In0.sig1234567890abc"
    safe = redact_text(f"token={jwt}")
    assert jwt not in safe
    assert REDACTED in safe


def test_redacts_bearer_header() -> None:
    safe = redact_text(
        "Authorization: Bearer ghp_1234567890abcdefghijklmnopqrst"
    )
    assert "ghp_1234567890abcdefghijklmnopqrst" not in safe
    assert "Bearer" in safe  # scheme label survives
    assert REDACTED in safe


def test_redacts_key_value_pairs() -> None:
    cases = [
        "password=hunter2",
        "secret=abc123",
        "api_key=xyz987",
        "client_secret=deadbeef",
        "token=tok_1234567890abc",
    ]
    for case in cases:
        safe = redact_text(case)
        assert REDACTED in safe, f"{case!r} was not redacted"


def test_redacts_token_prefixes() -> None:
    for token in (
        "sk_live_1234567890abcdef",
        "pk_test_1234567890abcdef",
        "ghp_1234567890abcdefgh",
        "glpat-1234567890abcdef",
        "SK_LIVE_1234567890ABCDEF",  # case-insensitive
    ):
        safe = redact_text(f"key={token}")
        assert token not in safe
        assert REDACTED in safe


def test_redacts_long_opaque_runs() -> None:
    # Base64/hex-ish runs of 32+ chars (e.g. raw key material).
    blob = "A1b2C3d4E5f6G7h8I9j0K1l2M3n4O5p6"
    assert len(blob) >= 32
    safe = redact_text(f"enc={blob}")
    assert blob not in safe
    assert REDACTED in safe


def test_redacted_marker_is_stable() -> None:
    safe = redact_text(f"x {REDACTED} y", ["secret"])
    assert safe == f"x {REDACTED} y"


# ── sanitize_error ─────────────────────────────────────────────────────


def test_sanitize_error_scrubs_secrets() -> None:
    msg = "bunq rejected api key abc123secret for user 42"
    safe = sanitize_error(msg, ["abc123secret"])
    assert "abc123secret" not in safe
    assert REDACTED in safe


def test_sanitize_error_truncates_long_messages() -> None:
    msg = " ".join(f"word{i}" for i in range(400))
    safe = sanitize_error(msg, max_length=200)
    assert len(safe) <= 200 + len(" …")
    assert safe.endswith(" …")


def test_sanitize_error_short_message_untouched() -> None:
    msg = "Connection successful"
    assert sanitize_error(msg) == msg


def test_sanitize_error_empty_message() -> None:
    assert sanitize_error("") == ""
    assert sanitize_error(None) == ""  # type: ignore[arg-type]


def test_sanitize_error_redacts_before_truncating() -> None:
    # A secret sitting at the cut boundary must never survive the
    # truncation window.
    long_msg = "x" * 400
    msg = f"key abc123secret {long_msg}"
    safe = sanitize_error(msg, ["abc123secret"], max_length=300)
    assert "abc123secret" not in safe
    assert REDACTED in safe


def test_sanitize_error_truncates_at_sentence_boundary() -> None:
    msg = ("error one. " * 200) + ("error two. " * 200)
    safe = sanitize_error(msg, max_length=150)
    assert len(safe) <= 150 + len(" …")
    assert safe.endswith(" …")
