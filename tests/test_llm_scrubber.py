"""Unit tests for :mod:`app.llm.scrubber`.

Covers the pattern set required by NFR §7.1 (emails, Estonian / E.164
phone numbers, UUIDs, PEM keys, JWT and ``sk-``/``pk_`` tokens), the
``allow_raw`` opt-out, and the message-shape preservation contract of
:func:`scrub_messages`.
"""

from __future__ import annotations

from app.llm.scrubber import (
    SCRUB_PATTERNS,
    scrub_messages,
    scrub_prompt,
)


class TestScrubPromptPatterns:
    def test_email_redacted(self) -> None:
        assert scrub_prompt("contact user@example.com now") == ("contact [REDACTED_EMAIL] now")

    def test_multiple_emails(self) -> None:
        out = scrub_prompt("a@b.ee and c.d+tag@sub.example.com")
        assert "a@b.ee" not in out
        assert "c.d+tag@sub.example.com" not in out
        assert out.count("[REDACTED_EMAIL]") == 2

    def test_phone_ee(self) -> None:
        assert scrub_prompt("helista +37256789012 kell 10") == ("helista [REDACTED_PHONE] kell 10")

    def test_phone_e164(self) -> None:
        assert scrub_prompt("call +15551234567 please") == ("call [REDACTED_PHONE] please")

    def test_phone_bare_ee(self) -> None:
        assert scrub_prompt("call 003725678901 now") == ("call [REDACTED_PHONE] now")

    def test_uuid_redacted(self) -> None:
        assert scrub_prompt("id=550e8400-e29b-41d4-a716-446655440000.") == ("id=[REDACTED_UUID].")

    def test_uuid_uppercase(self) -> None:
        text = "id=550E8400-E29B-41D4-A716-446655440000"
        assert "[REDACTED_UUID]" in scrub_prompt(text)

    def test_sk_token(self) -> None:
        # 16+ chars after ``sk-`` prefix.
        out = scrub_prompt("key=sk-abc123DEF456ghi789")
        assert out == "key=[REDACTED_TOKEN]"

    def test_pk_token(self) -> None:
        out = scrub_prompt("pk_live_abcdef0123456789xyz")
        assert "[REDACTED_TOKEN]" in out

    def test_jwt_like_token(self) -> None:
        jwt = "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjMifQ.signatureabc"
        out = scrub_prompt(f"Bearer {jwt}")
        assert out == "Bearer [REDACTED_TOKEN]"

    def test_pem_private_key(self) -> None:
        pem = (
            "-----BEGIN PRIVATE KEY-----\n"
            "MIIEvQIBADANBgkqhkiG9w0BAQEFAASCBKcwggSjAgEAAoIBAQ\n"
            "-----END PRIVATE KEY-----"
        )
        out = scrub_prompt(f"cert:\n{pem}\nend")
        assert "BEGIN PRIVATE KEY" not in out
        assert "[REDACTED_KEY]" in out

    def test_mixed_content(self) -> None:
        text = (
            "Mari Maasikas (mari@example.ee, +37251234567) "
            "draft-id=550e8400-e29b-41d4-a716-446655440000 "
            "token=sk-abcdefghijklmnop123456"
        )
        out = scrub_prompt(text)
        assert "mari@example.ee" not in out
        assert "+37251234567" not in out
        assert "550e8400-e29b-41d4-a716-446655440000" not in out
        assert "sk-abcdefghijklmnop123456" not in out
        assert "[REDACTED_EMAIL]" in out
        assert "[REDACTED_PHONE]" in out
        assert "[REDACTED_UUID]" in out
        assert "[REDACTED_TOKEN]" in out

    def test_empty_string_passthrough(self) -> None:
        assert scrub_prompt("") == ""

    def test_no_pii_unchanged(self) -> None:
        plain = "See on tavaline lause ilma isikuandmeteta."
        assert scrub_prompt(plain) == plain


class TestAllowRaw:
    def test_allow_raw_preserves_prompt(self) -> None:
        text = "contact mari@example.ee about draft 550e8400-e29b-41d4-a716-446655440000"
        assert scrub_prompt(text, allow_raw=True) == text

    def test_allow_raw_preserves_pem(self) -> None:
        pem = "-----BEGIN PRIVATE KEY-----\nabc\n-----END PRIVATE KEY-----"
        assert scrub_prompt(pem, allow_raw=True) == pem


class TestScrubMessages:
    def test_preserves_ordering_and_roles(self) -> None:
        msgs = [
            {"role": "system", "content": "You are an assistant."},
            {"role": "user", "content": "email me at a@b.ee"},
            {"role": "assistant", "content": "Sure."},
            {"role": "user", "content": "uuid 550e8400-e29b-41d4-a716-446655440000"},
        ]
        out = scrub_messages(msgs)

        assert [m["role"] for m in out] == ["system", "user", "assistant", "user"]
        assert out[0]["content"] == "You are an assistant."
        assert out[1]["content"] == "email me at [REDACTED_EMAIL]"
        assert out[2]["content"] == "Sure."
        assert "[REDACTED_UUID]" in out[3]["content"]

    def test_does_not_mutate_input(self) -> None:
        msgs = [{"role": "user", "content": "a@b.ee"}]
        _ = scrub_messages(msgs)
        assert msgs[0]["content"] == "a@b.ee"

    def test_list_content_blocks(self) -> None:
        msgs = [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "mari@example.ee wrote this"},
                    {"type": "text", "text": "ok"},
                ],
            }
        ]
        out = scrub_messages(msgs)
        blocks = out[0]["content"]
        assert blocks[0]["type"] == "text"
        assert blocks[0]["text"] == "[REDACTED_EMAIL] wrote this"
        assert blocks[1]["text"] == "ok"

    def test_tool_result_inner_content(self) -> None:
        msgs = [
            {
                "role": "user",
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": "toolu_1",
                        "content": "user=mari@example.ee",
                    }
                ],
            }
        ]
        out = scrub_messages(msgs)
        block = out[0]["content"][0]
        assert block["tool_use_id"] == "toolu_1"
        assert block["content"] == "user=[REDACTED_EMAIL]"

    def test_allow_raw_returns_input_unchanged(self) -> None:
        msgs = [{"role": "user", "content": "a@b.ee"}]
        assert scrub_messages(msgs, allow_raw=True) is msgs


class TestPatternList:
    def test_scrub_patterns_exported(self) -> None:
        assert SCRUB_PATTERNS
        placeholders = {placeholder for _, placeholder in SCRUB_PATTERNS}
        assert placeholders == {
            "[REDACTED_EMAIL]",
            "[REDACTED_PHONE]",
            "[REDACTED_UUID]",
            "[REDACTED_TOKEN]",
            "[REDACTED_KEY]",
        }
