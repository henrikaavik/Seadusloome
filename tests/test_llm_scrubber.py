"""Unit tests for :mod:`app.llm.scrubber`.

Covers the pattern set required by NFR §7.1 (emails, Estonian / E.164
phone numbers, UUIDs, PEM keys, JWT and ``sk-``/``pk_`` tokens,
Estonian isikukoodid and EE IBANs per issue #846), the ``allow_raw``
opt-out, and the message-shape preservation contract of
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

    # --- Estonian isikukood (#846) -------------------------------------

    def test_isikukood_male_redacted(self) -> None:
        out = scrub_prompt("kaebaja isikukood 38501010002 esitas taotluse")
        assert "38501010002" not in out
        assert out == "kaebaja isikukood [REDACTED_ISIKUKOOD] esitas taotluse"

    def test_isikukood_female_2000s_redacted(self) -> None:
        out = scrub_prompt("isik 60002290003 on registreeritud")
        assert out == "isik [REDACTED_ISIKUKOOD] on registreeritud"

    def test_isikukood_372_prefix_not_mislabelled_as_phone(self) -> None:
        # A bare isikukood starting with 372 (male born 1972) must win
        # over the Estonian phone pattern via list ordering.
        out = scrub_prompt("isikukood 37201010002")
        assert out == "isikukood [REDACTED_ISIKUKOOD]"

    def test_invalid_isikukood_first_digit_unchanged(self) -> None:
        # First digit 9 is not a valid century/sex digit.
        plain = "number 99901010002 ei ole isikukood"
        assert scrub_prompt(plain) == plain

    def test_invalid_isikukood_month_unchanged(self) -> None:
        # Month 91 fails the [01]\d constraint.
        plain = "number 48191010002 ei ole isikukood"
        assert scrub_prompt(plain) == plain

    def test_isikukood_inside_longer_digit_run_unchanged(self) -> None:
        plain = "viitenumber 3850101000212345 jääb alles"
        assert scrub_prompt(plain) == plain

    def test_isikukood_in_email_redacted_as_email(self) -> None:
        out = scrub_prompt("kiri 38501010002@example.com saadetud")
        assert out == "kiri [REDACTED_EMAIL] saadetud"

    # --- Estonian IBAN (#846) ------------------------------------------

    def test_ee_iban_compact_redacted(self) -> None:
        out = scrub_prompt("konto EE382200221020145685 makse")
        assert out == "konto [REDACTED_IBAN] makse"

    def test_ee_iban_grouped_redacted(self) -> None:
        out = scrub_prompt("konto EE38 2200 2210 2014 5685 makse")
        assert out == "konto [REDACTED_IBAN] makse"

    def test_short_ee_prefix_unchanged(self) -> None:
        plain = "direktiiv EE12 3456 ei ole IBAN"
        assert scrub_prompt(plain) == plain

    def test_foreign_iban_unchanged(self) -> None:
        # Only Estonian IBANs are in scope for the EE pattern.
        plain = "konto SE3550000000054910000003"
        assert scrub_prompt(plain) == plain

    def test_phone_still_redacted_alongside_isikukood(self) -> None:
        out = scrub_prompt("isik 38501010002, tel +37256789012")
        assert "[REDACTED_ISIKUKOOD]" in out
        assert "[REDACTED_PHONE]" in out


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
            "[REDACTED_ISIKUKOOD]",
            "[REDACTED_IBAN]",
        }
