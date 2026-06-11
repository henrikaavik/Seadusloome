"""Phase 2 document-processing edge-case tests (#309 finish).

Uses the ``tests/fixtures/drafts/`` `.docx` fixtures (created by
``__generate__.py``) to exercise the parse / extract / analyze
handlers' edge-case behaviour without needing a live Tika sidecar,
real LLM, or live Jena/Postgres connection:

Parser-side (``TestParserStructuralChecks``):
    * ``python-docx`` parses every fixture cleanly (length sanity
      checks — proxies for the structured-output guarantee the real
      Tika-backed parser makes).

Extractor edge cases (``TestExtractorEdgeCases``):
    * :func:`app.docs.entity_extractor.extract_refs_from_text` returns
      the right shape on empty / near-empty input.

Extractor recall + dedupe (``TestExtractorRecallAndDedupe``):
    * :func:`extract_refs_from_text` runs end-to-end with a mocked
      :class:`app.llm.LLMProvider` that replays the references actually
      present in ``many_references.docx``; the dedupe step collapses
      cross-chunk duplicates and the result counts match the fixture.

Extractor graceful degradation (``TestExtractorGracefulDegradation``):
    * :func:`extract_refs_from_text` degrades gracefully on the
      ``malformed_refs.docx`` fixture (no exceptions, partial
      extraction) and on provider-level failures.

#309 finish (this commit) extends with:

``TestExtractHandlerDisambiguation``:
    CELEX vs §-ref vs court-case classification — the LLM is mocked
    to return mixed buckets and the extractor preserves them.

``TestExtractHandlerDedupePatterns``:
    Same §-ref appearing in multiple paragraphs / multiple chunks
    deduplicates to a single result.

``TestExtractHandlerProviderFailures``:
    Provider exceptions and malformed JSON replies are swallowed
    (contract: extractor returns ``[]`` rather than propagating —
    log-only failure per ``_extract_from_chunk``).

``TestExtractHandlerPromptShape``:
    The prompt handed to ``provider.extract_json`` carries the
    documented instructions ("extract every legal reference") and
    the source text verbatim.

``TestExtractHandlerLargeInput``:
    A multi-paragraph document still extracts cleanly when chunked.

``TestAnalyzeHandlerImpactAnalyzer`` (analyzer-level):
    Empty findings, unresolvable-URI gap routing, conflict detection,
    EU compliance, and gap analysis — all driven by a mocked
    :class:`SparqlClient` per the analyzer's "partial > none" contract.

``TestAnalyzeHandlerScoring``:
    The pure :func:`calculate_impact_score` formula on canned
    findings.

``TestAnalyzeHandlerRowToResolvedRef``:
    The private helper that reconstructs a :class:`ResolvedRef` from
    a ``draft_entities`` row tuple — confirms tolerance for both
    string-JSON and dict shapes for the JSONB columns.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from docx import Document

from app.docs.analyze_handler import _row_to_resolved_ref
from app.docs.entity_extractor import ExtractedRef, extract_refs_from_text
from app.impact.analyzer import ImpactAnalyzer, ImpactFindings
from app.impact.scoring import calculate_impact_score

# ---------------------------------------------------------------------------
# Fixture discovery
# ---------------------------------------------------------------------------

_FIXTURE_DIR = Path(__file__).resolve().parent / "fixtures" / "drafts"

_FIXTURES = {
    "normal": _FIXTURE_DIR / "normal_legal_text.docx",
    "very_short": _FIXTURE_DIR / "very_short.docx",
    "many_refs": _FIXTURE_DIR / "many_references.docx",
    "empty_body": _FIXTURE_DIR / "empty_body.docx",
    "malformed": _FIXTURE_DIR / "malformed_refs.docx",
}


@pytest.fixture(autouse=True, scope="module")
def _fixtures_present():
    """Make sure every fixture file exists before any test runs.

    The fixtures are committed to the repo, so a missing file is a
    real problem (probably a bad checkout). Fail loudly rather than
    have individual tests trip over ``FileNotFoundError`` mid-suite.
    """
    missing = [name for name, p in _FIXTURES.items() if not p.exists()]
    if missing:
        pytest.fail(
            "Missing fixture files: "
            + ", ".join(missing)
            + "\nRun `uv run python tests/fixtures/drafts/__generate__.py`."
        )


def _read_paragraphs(path: Path) -> list[str]:
    """Return non-empty paragraph texts from a ``.docx`` fixture.

    ``python-docx``'s ``Document`` factory is typed to accept a ``str``
    or a binary file object, not ``pathlib.Path`` — so we coerce the
    fixture path here rather than at every call site.
    """
    doc = Document(str(path))
    return [p.text for p in doc.paragraphs if p.text.strip()]


def _full_text(path: Path) -> str:
    """Return the concatenated paragraph text for a ``.docx`` fixture."""
    return "\n\n".join(_read_paragraphs(path))


# ---------------------------------------------------------------------------
# Parser-side sanity checks
# ---------------------------------------------------------------------------
#
# We can't run the real Tika-backed ``parse_draft`` handler here (no
# Tika sidecar in unit tests), so we exercise ``python-docx`` directly
# against the fixtures — the same library Tika delegates to for `.docx`
# bodies. The checks are structural (length / non-empty) — Sprint 3
# will add the full mocked ``parse_draft`` test set with a fake Tika
# client.


class TestParserStructuralChecks:
    def test_very_short_parses_without_crash(self):
        """Very short docs should round-trip through python-docx cleanly."""
        paragraphs = _read_paragraphs(_FIXTURES["very_short"])
        # Heading + one body paragraph.
        assert len(paragraphs) == 2
        assert paragraphs[0].startswith("Test eelnõu 2")
        assert "lühike" in paragraphs[1]

    def test_empty_body_parses_with_only_title(self):
        """Title-only docs must still produce a structured response.

        Mirrors the contract ``parse_draft`` enforces (#step 5 of the
        handler): a body-less file must not crash the parser, even
        though downstream the empty-text guard will kick in later.
        """
        paragraphs = _read_paragraphs(_FIXTURES["empty_body"])
        assert len(paragraphs) == 1
        assert paragraphs[0].startswith("Test eelnõu 4")

    def test_many_references_has_expected_body(self):
        """The many-references fixture must contain the seeded refs in its body."""
        text = _full_text(_FIXTURES["many_refs"])
        assert "§ 1" in text and "§ 16" in text
        assert "32016R0679" in text
        assert "3-2-1-100-23" in text


# ---------------------------------------------------------------------------
# Extractor edge cases
# ---------------------------------------------------------------------------
#
# ``extract_refs_from_text`` is the public entry point of the
# entity extractor. It accepts an optional ``provider`` so tests can
# replay scripted LLM responses without hitting the network.


class TestExtractorEdgeCases:
    def test_empty_text_returns_empty_list(self):
        """Empty / whitespace input must short-circuit to ``[]``."""
        assert extract_refs_from_text("") == []
        assert extract_refs_from_text("   \n  \t  ") == []

    def test_very_short_doc_extracts_nothing(self):
        """No references in the body → extractor returns an empty list.

        We hand the extractor a MagicMock provider that always returns
        an empty ``{"refs": []}`` payload — the contract here is that
        the extractor handles the empty case without raising and
        produces no results.
        """
        text = _full_text(_FIXTURES["very_short"])
        assert text.strip(), "fixture invariant: very_short has body content"

        provider = MagicMock()
        provider.extract_json.return_value = {"refs": []}

        refs = extract_refs_from_text(text, provider=provider)

        assert refs == []
        # Even an empty document should still get sent to the LLM
        # exactly once (one chunk, one extraction call).
        provider.extract_json.assert_called_once()

    def test_empty_body_doc_extracts_nothing(self):
        """Title-only documents extract zero refs from the title."""
        text = _full_text(_FIXTURES["empty_body"])
        # The title alone is short enough to be a single chunk.
        provider = MagicMock()
        provider.extract_json.return_value = {"refs": []}

        refs = extract_refs_from_text(text, provider=provider)
        assert refs == []


class TestExtractorRecallAndDedupe:
    """Replay the references actually present in ``many_references.docx``.

    The extractor itself does not run regex over the text — it asks the
    LLM. We simulate the LLM's response by feeding the provider a
    scripted reply that lists the same references the fixture contains,
    plus one duplicate to exercise dedupe.
    """

    def test_extracts_expected_counts_on_many_refs_fixture(self):
        """≥15 §-refs, 3 CELEX, 2 court cases after dedupe."""
        text = _full_text(_FIXTURES["many_refs"])
        assert text.strip()

        # Build the scripted reply. ``ref_text`` and ``ref_type`` must
        # match what the real extractor would produce — the test
        # asserts behaviour ON THE FIXTURE, so the LLM reply mirrors
        # the fixture's content rather than a synthetic set.
        provision_refs = [
            {"ref_text": f"§ {n}", "ref_type": "provision", "confidence": 0.9}
            for n in (1, 3, 5, 6, 8, 10, 11, 13, 15)
        ] + [
            {"ref_text": "§ 2 lg 1", "ref_type": "provision", "confidence": 0.92},
            {"ref_text": "§ 4 lg 2 p 1", "ref_type": "provision", "confidence": 0.95},
            {"ref_text": "§ 7 lg 3", "ref_type": "provision", "confidence": 0.93},
            {"ref_text": "§ 9 lg 1 p 2", "ref_type": "provision", "confidence": 0.94},
            {"ref_text": "§ 12 lg 2", "ref_type": "provision", "confidence": 0.92},
            {"ref_text": "§ 14 lg 1", "ref_type": "provision", "confidence": 0.91},
            {"ref_text": "§ 16 lg 1 p 3", "ref_type": "provision", "confidence": 0.96},
        ]
        # Sanity check: we built ≥15 distinct §-references.
        assert len(provision_refs) >= 15

        eu_refs = [
            {"ref_text": "32016R0679", "ref_type": "eu_act", "confidence": 0.98},
            {"ref_text": "32019L0790", "ref_type": "eu_act", "confidence": 0.97},
            {"ref_text": "32020D0001", "ref_type": "eu_act", "confidence": 0.96},
        ]
        case_refs = [
            {"ref_text": "3-2-1-100-23", "ref_type": "court_decision", "confidence": 0.95},
            {"ref_text": "3-1-1-50-22", "ref_type": "court_decision", "confidence": 0.95},
        ]

        # Include one deliberate duplicate (``§ 5``) at lower
        # confidence to verify the dedupe step keeps the higher one.
        scripted = {
            "refs": provision_refs
            + eu_refs
            + case_refs
            + [{"ref_text": "§ 5", "ref_type": "provision", "confidence": 0.4}]
        }

        provider = MagicMock()
        provider.extract_json.return_value = scripted

        refs = extract_refs_from_text(text, provider=provider)

        # Bucket the results by type so the per-bucket counts are
        # easy to read in a failure report.
        by_type: dict[str, list[ExtractedRef]] = {}
        for r in refs:
            by_type.setdefault(r.ref_type, []).append(r)

        assert len(by_type.get("provision", [])) >= 15, (
            f"expected ≥15 deduped §-refs, got {len(by_type.get('provision', []))}: "
            f"{[r.ref_text for r in by_type.get('provision', [])]}"
        )
        assert len(by_type.get("eu_act", [])) == 3, (
            f"expected 3 CELEX refs, got {len(by_type.get('eu_act', []))}"
        )
        assert len(by_type.get("court_decision", [])) == 2, (
            f"expected 2 court cases, got {len(by_type.get('court_decision', []))}"
        )

        # Dedupe check — ``§ 5`` appears once with the higher
        # confidence value of the two replays.
        section_5 = [r for r in by_type["provision"] if r.ref_text == "§ 5"]
        assert len(section_5) == 1, f"§ 5 must be deduped, got {len(section_5)} entries"
        assert section_5[0].confidence == pytest.approx(0.9), (
            f"dedupe should keep the higher-confidence entry, got {section_5[0].confidence}"
        )


class TestExtractorGracefulDegradation:
    def test_malformed_refs_does_not_crash(self):
        """Typo-ridden references must not raise — the extractor degrades gracefully.

        The malformed fixture mixes broken inputs with one valid ``§ 5``.
        The LLM reply mirrors a plausible "found one valid ref, dropped
        the rest" outcome — what we are testing is that the extractor
        itself doesn't blow up on weird inputs and that valid refs
        still come through.
        """
        text = _full_text(_FIXTURES["malformed"])
        assert text.strip()

        provider = MagicMock()
        provider.extract_json.return_value = {
            "refs": [
                # The only well-formed reference in the fixture.
                {"ref_text": "§ 5", "ref_type": "provision", "confidence": 0.85},
            ]
        }

        refs = extract_refs_from_text(text, provider=provider)

        # No exception → graceful. Partial extraction → the valid ref
        # is preserved.
        assert isinstance(refs, list)
        assert len(refs) == 1
        assert refs[0].ref_text == "§ 5"
        assert refs[0].ref_type == "provision"

    def test_provider_exception_returns_empty_list(self):
        """A failed LLM call on the only chunk yields ``[]`` without raising.

        Mirrors the ``extract_refs: LLM call failed on chunk ...`` log
        branch in ``_extract_from_chunk`` — a chunk-level failure must
        be swallowed so one flaky window doesn't kill the whole
        draft's extraction.
        """
        text = _full_text(_FIXTURES["normal"])
        assert text.strip()

        provider = MagicMock()
        provider.extract_json.side_effect = RuntimeError("simulated provider failure")

        refs = extract_refs_from_text(text, provider=provider)
        assert refs == []


# ---------------------------------------------------------------------------
# #309 finish — extract_handler.py edge cases
# ---------------------------------------------------------------------------


class TestExtractHandlerDisambiguation:
    """The extractor's contract is to preserve the ``ref_type`` the LLM
    assigns to each match. These tests give the mocked provider a payload
    containing each of the five supported types and assert that the
    output is faithfully bucketed by ``ref_type``.

    The extractor itself doesn't perform regex classification — that's
    delegated to the LLM — but the dedupe / shape logic must keep the
    per-type buckets distinct for the resolver's downstream dispatch.
    """

    def test_celex_and_provision_classified_separately(self):
        """A mixed ``§ 12`` + ``32016R0679`` payload bucketed into provision / eu_act."""
        text = "Käesolev säte muudab § 12 ja viitab regulatsioonile 32016R0679."

        provider = MagicMock()
        provider.extract_json.return_value = {
            "refs": [
                {"ref_text": "§ 12", "ref_type": "provision", "confidence": 0.9},
                {"ref_text": "32016R0679", "ref_type": "eu_act", "confidence": 0.95},
            ]
        }

        refs = extract_refs_from_text(text, provider=provider)

        by_type: dict[str, list[ExtractedRef]] = {}
        for r in refs:
            by_type.setdefault(r.ref_type, []).append(r)

        assert "provision" in by_type and "eu_act" in by_type
        assert len(by_type["provision"]) == 1
        assert len(by_type["eu_act"]) == 1
        assert by_type["provision"][0].ref_text == "§ 12"
        assert by_type["eu_act"][0].ref_text == "32016R0679"

    def test_court_case_number_preserved_as_court_decision(self):
        """Estonian Supreme Court case number ``3-2-1-100-23`` lands as court_decision."""
        text = "Riigikohus on otsuses 3-2-1-100-23 leidnud, et …"

        provider = MagicMock()
        provider.extract_json.return_value = {
            "refs": [
                {"ref_text": "3-2-1-100-23", "ref_type": "court_decision", "confidence": 0.95},
            ]
        }

        refs = extract_refs_from_text(text, provider=provider)
        assert len(refs) == 1
        assert refs[0].ref_text == "3-2-1-100-23"
        assert refs[0].ref_type == "court_decision"

    def test_multiple_court_case_number_formats_all_preserved(self):
        """Variants — civil/criminal chambers, lower-court cases — all flow through.

        The fixture's ``many_references.docx`` already contains
        ``3-2-1-100-23`` and ``3-1-1-50-22``; this test pushes additional
        variants through the LLM mock to verify the per-type bucket
        does not deduplicate across DIFFERENT case numbers.
        """
        provider = MagicMock()
        provider.extract_json.return_value = {
            "refs": [
                {"ref_text": "3-2-1-100-23", "ref_type": "court_decision", "confidence": 0.95},
                {"ref_text": "3-1-1-50-22", "ref_type": "court_decision", "confidence": 0.92},
                # Lower-court suffix variant (dash-in-suffix) — the
                # extractor must keep it as a distinct court_decision.
                {"ref_text": "2-22-1234/15", "ref_type": "court_decision", "confidence": 0.85},
            ]
        }
        refs = extract_refs_from_text("Kohtuotsused.", provider=provider)
        decisions = [r for r in refs if r.ref_type == "court_decision"]
        assert len(decisions) == 3
        decision_texts = {r.ref_text for r in decisions}
        assert decision_texts == {"3-2-1-100-23", "3-1-1-50-22", "2-22-1234/15"}

    def test_all_five_ref_types_classified_in_one_payload(self):
        """A payload with one ref per supported type produces one bucket per type."""
        provider = MagicMock()
        provider.extract_json.return_value = {
            "refs": [
                {"ref_text": "KarS", "ref_type": "law", "confidence": 0.9},
                {"ref_text": "KarS § 133", "ref_type": "provision", "confidence": 0.95},
                {"ref_text": "32016R0679", "ref_type": "eu_act", "confidence": 0.97},
                {"ref_text": "3-1-1-63-15", "ref_type": "court_decision", "confidence": 0.95},
                {"ref_text": "hea usu põhimõte", "ref_type": "concept", "confidence": 0.7},
            ]
        }

        refs = extract_refs_from_text("Test.", provider=provider)

        types = {r.ref_type for r in refs}
        assert types == {"law", "provision", "eu_act", "court_decision", "concept"}

    def test_same_text_with_different_types_kept_as_two_refs(self):
        """Per ``_deduplicate``'s contract, the dedupe key is ``(ref_text, ref_type)`` —
        the same string with two different classifications is NOT a duplicate.

        This matches the docstring on ``_deduplicate``: "the model may
        tag the same string as both ``law`` and ``provision`` in overlap
        regions and we want to keep both so the resolver can try both
        lookups".
        """
        provider = MagicMock()
        provider.extract_json.return_value = {
            "refs": [
                {"ref_text": "KarS", "ref_type": "law", "confidence": 0.9},
                {"ref_text": "KarS", "ref_type": "provision", "confidence": 0.5},
            ]
        }
        refs = extract_refs_from_text("KarS.", provider=provider)
        assert len(refs) == 2


class TestExtractHandlerDedupePatterns:
    """Dedupe across chunks and across repeated paragraphs.

    The ``many_references.docx`` fixture is small enough to fit in a
    single chunk; to exercise the cross-chunk dedupe path we force the
    chunker via :func:`unittest.mock.patch` on ``chunk_text``, mirroring
    the pattern in ``test_docs_entity_extractor.test_dedupes_refs_across_chunks``.
    """

    def test_same_ref_in_three_paragraphs_returns_single_result(self):
        """A reference repeated across paragraphs (one chunk) deduplicates to one."""
        text = (
            "§ 12 sätestab esimese alusprintsiibi.\n\n"
            "Vastavalt § 12 on kohustuslik registreerimine.\n\n"
            "§ 12 kohaldatakse kõigile huvitatud isikutele."
        )
        provider = MagicMock()
        provider.extract_json.return_value = {
            "refs": [
                # The LLM finds the same § 12 three times — the dedupe
                # step inside ``extract_refs_from_text`` collapses them.
                {"ref_text": "§ 12", "ref_type": "provision", "confidence": 0.9},
                {"ref_text": "§ 12", "ref_type": "provision", "confidence": 0.85},
                {"ref_text": "§ 12", "ref_type": "provision", "confidence": 0.88},
            ]
        }

        refs = extract_refs_from_text(text, provider=provider)

        assert len(refs) == 1
        assert refs[0].ref_text == "§ 12"
        assert refs[0].ref_type == "provision"
        # Highest confidence survives — same contract as
        # ``test_extracts_expected_counts_on_many_refs_fixture`` above.
        assert refs[0].confidence == pytest.approx(0.9)

    def test_dedupe_keeps_highest_confidence_across_chunks(self):
        """When the same ref shows up in chunk 0 and chunk 1, the higher confidence wins."""
        from unittest.mock import patch

        from app.docs.chunking import ChunkSpan

        provider = MagicMock()
        provider.extract_json.side_effect = [
            {"refs": [{"ref_text": "§ 5", "ref_type": "provision", "confidence": 0.6}]},
            {"refs": [{"ref_text": "§ 5", "ref_type": "provision", "confidence": 0.95}]},
        ]
        text = "x" * 2500
        with patch("app.docs.entity_extractor.chunk_text") as mock_chunk:
            mock_chunk.return_value = [
                ChunkSpan(start=0, end=1250, text=text[:1250]),
                ChunkSpan(start=1250, end=2500, text=text[1250:]),
            ]
            refs = extract_refs_from_text(text, provider=provider)

        assert len(refs) == 1
        assert refs[0].confidence == pytest.approx(0.95)


class TestExtractHandlerProviderFailures:
    """Provider-side failures are swallowed at the chunk boundary.

    Contract (per ``_extract_from_chunk``):
        - ``provider.extract_json`` raising → that chunk contributes ``[]``,
          the rest of the pipeline keeps going.
        - Reply that doesn't pass ``_parse_response`` (non-dict, missing
          ``refs`` key, ``refs`` not a list) → ``[]`` from that chunk.
        - Individual ref items with invalid shape are silently dropped.
    """

    def test_non_dict_reply_returns_empty(self, caplog: pytest.LogCaptureFixture):
        """Provider returning ``"oops"`` (a string) → empty list + warning."""
        provider = MagicMock()
        provider.extract_json.return_value = "oops not a dict"

        with caplog.at_level("WARNING"):
            refs = extract_refs_from_text("Lühike tekst.", provider=provider)

        assert refs == []
        assert any("non-dict reply" in rec.message for rec in caplog.records)

    def test_reply_missing_refs_key_returns_empty(self, caplog: pytest.LogCaptureFixture):
        """Provider replying without the ``refs`` key → empty list + warning."""
        provider = MagicMock()
        provider.extract_json.return_value = {"other_key": []}

        with caplog.at_level("WARNING"):
            refs = extract_refs_from_text("Test.", provider=provider)

        assert refs == []
        assert any("missing 'refs' key" in rec.message for rec in caplog.records)

    def test_reply_with_refs_as_string_returns_empty(self, caplog: pytest.LogCaptureFixture):
        """``refs`` field is not a list — handler skips that chunk."""
        provider = MagicMock()
        provider.extract_json.return_value = {"refs": "should-be-a-list"}

        with caplog.at_level("WARNING"):
            refs = extract_refs_from_text("Test.", provider=provider)

        assert refs == []
        assert any("not a list" in rec.message for rec in caplog.records)

    def test_individual_ref_with_bad_shape_dropped(self):
        """Items inside ``refs`` that fail validation are skipped silently."""
        provider = MagicMock()
        provider.extract_json.return_value = {
            "refs": [
                # Not a dict — dropped.
                "not-a-dict",
                # Missing ref_text — dropped.
                {"ref_type": "provision", "confidence": 0.9},
                # Empty ref_text — dropped.
                {"ref_text": "", "ref_type": "provision", "confidence": 0.9},
                # Invalid ref_type — dropped.
                {"ref_text": "§ 1", "ref_type": "bogus", "confidence": 0.9},
                # Valid — kept.
                {"ref_text": "§ 2", "ref_type": "provision", "confidence": 0.9},
            ]
        }

        refs = extract_refs_from_text("Test.", provider=provider)
        assert len(refs) == 1
        assert refs[0].ref_text == "§ 2"

    def test_invalid_confidence_coerced_to_zero(self):
        """A non-numeric ``confidence`` field falls back to ``0.0`` rather than crashing."""
        provider = MagicMock()
        provider.extract_json.return_value = {
            "refs": [
                {"ref_text": "§ 3", "ref_type": "provision", "confidence": "not-a-number"},
            ]
        }

        refs = extract_refs_from_text("Test.", provider=provider)
        assert len(refs) == 1
        assert refs[0].confidence == pytest.approx(0.0)

    def test_confidence_above_one_is_clamped(self):
        """A ``confidence`` field above 1.0 is clamped to 1.0 by ``_parse_response``."""
        provider = MagicMock()
        provider.extract_json.return_value = {
            "refs": [
                {"ref_text": "§ 4", "ref_type": "provision", "confidence": 5.0},
                {"ref_text": "§ 5", "ref_type": "provision", "confidence": -0.5},
            ]
        }

        refs = extract_refs_from_text("Test.", provider=provider)
        by_text = {r.ref_text: r for r in refs}
        assert by_text["§ 4"].confidence == pytest.approx(1.0)
        assert by_text["§ 5"].confidence == pytest.approx(0.0)


class TestExtractHandlerPromptShape:
    """Prompt-template-shape assertions.

    These guard against future refactors silently breaking the prompt
    contract (e.g. removing the "extract every legal reference"
    instruction or losing the embedded source text).
    """

    def test_prompt_contains_extraction_instruction(self):
        """The exact wording the LLM keys on must reach ``extract_json``."""
        provider = MagicMock()
        provider.extract_json.return_value = {"refs": []}

        extract_refs_from_text("§ 1. Testtekst.", provider=provider)

        provider.extract_json.assert_called_once()
        prompt_arg = provider.extract_json.call_args.args[0]
        # The prompt asks for legal reference extraction (lowercased
        # match to be wording-tolerant).
        assert "extract every legal reference" in prompt_arg.lower()
        # And mentions all five ref_type buckets so the LLM picks
        # from the right set.
        for label in ("law", "provision", "eu_act", "court_decision", "concept"):
            assert label in prompt_arg

    def test_prompt_embeds_source_text_verbatim(self):
        """The chunk's text reaches the LLM unaltered (modulo backticks)."""
        provider = MagicMock()
        provider.extract_json.return_value = {"refs": []}

        text = "§ 12 lg 2 sätestab piiranguid. Käesolevat sätet kohaldatakse vastavalt."
        extract_refs_from_text(text, provider=provider)

        prompt_arg = provider.extract_json.call_args.args[0]
        assert text in prompt_arg

    def test_prompt_passes_json_schema_argument(self):
        """``provider.extract_json`` is invoked with the ``schema=`` kwarg."""
        provider = MagicMock()
        provider.extract_json.return_value = {"refs": []}

        extract_refs_from_text("§ 1.", provider=provider)

        kwargs = provider.extract_json.call_args.kwargs
        assert "schema" in kwargs
        schema = kwargs["schema"]
        assert schema["type"] == "object"
        assert "refs" in schema["properties"]


class TestExtractHandlerLargeInput:
    """Large multi-paragraph inputs still extract correctly when chunked.

    The chunker's default target is 24k chars per chunk. We build a
    document large enough to be split into two chunks and verify the
    extractor invokes the provider twice + merges the deduplicated
    results.
    """

    def test_large_input_extracts_across_multiple_chunks(self):
        """A 100-paragraph document is chunked + each chunk's refs are merged."""
        # Build a document well above the 24k character chunk target.
        paragraph = (
            "Käesoleva paragrahvi kohaselt rakendatakse menetlust üksnes nendele juhtumitele, "
        )
        paragraph += "mille puhul on tagatud, et taotleja õigused ei riku õigusakte. " * 5
        text = "\n\n".join(f"{i}. {paragraph}" for i in range(120))

        provider = MagicMock()
        # Each chunk returns the same single ref. Dedupe collapses
        # them to one in the final result.
        provider.extract_json.return_value = {
            "refs": [
                {"ref_text": "§ 1", "ref_type": "provision", "confidence": 0.9},
            ]
        }

        refs = extract_refs_from_text(text, provider=provider)

        # Multiple chunks were sent.
        assert provider.extract_json.call_count >= 2
        # But dedupe collapses ``§ 1`` to a single result.
        assert len(refs) == 1
        assert refs[0].ref_text == "§ 1"

    def test_large_input_with_unique_refs_per_chunk_preserved(self):
        """Unique refs from each chunk all flow through to the final list."""
        from unittest.mock import patch

        from app.docs.chunking import ChunkSpan

        text = "x" * 4000
        provider = MagicMock()
        # 3 chunks → 3 distinct refs → 3 results.
        provider.extract_json.side_effect = [
            {"refs": [{"ref_text": "§ 1", "ref_type": "provision", "confidence": 0.9}]},
            {"refs": [{"ref_text": "§ 2", "ref_type": "provision", "confidence": 0.9}]},
            {"refs": [{"ref_text": "§ 3", "ref_type": "provision", "confidence": 0.9}]},
        ]
        with patch("app.docs.entity_extractor.chunk_text") as mock_chunk:
            mock_chunk.return_value = [
                ChunkSpan(start=0, end=1500, text=text[:1500]),
                ChunkSpan(start=1500, end=3000, text=text[1500:3000]),
                ChunkSpan(start=3000, end=4000, text=text[3000:]),
            ]
            refs = extract_refs_from_text(text, provider=provider)

        assert len(refs) == 3
        assert {r.ref_text for r in refs} == {"§ 1", "§ 2", "§ 3"}


# ---------------------------------------------------------------------------
# #309 finish — analyze_handler.py edge cases
# ---------------------------------------------------------------------------
#
# The full ``analyze_impact`` handler is heavily mocked in
# ``tests/test_docs_analyze_handler.py`` (DB + Jena + analyzer). The
# tests below focus on the handler's internal contracts at a lower
# level: the :class:`ImpactAnalyzer` driving the four SPARQL passes and
# the :func:`_row_to_resolved_ref` helper that reconstructs a
# :class:`ResolvedRef` from a ``draft_entities`` row tuple. Both are
# pure unit-testable surfaces that don't need a real DB / Jena.


# Graph URI shape must match the production allowlist enforced by
# ``app.impact.queries._validate_graph_uri`` — see #476.
_GRAPH_URI = "https://data.riik.ee/ontology/estleg/drafts/22222222-2222-2222-2222-222222222222"


def _mock_sparql_client(responses: dict[str, list[dict[str, str]]]) -> MagicMock:
    """Build a SparqlClient mock that returns canned rows by query fingerprint.

    Mirrors the helper in ``tests/test_docs_impact_analyzer.py``:
    ``responses`` keys are substring fingerprints; the first one found
    in the submitted SPARQL text wins. Unmatched queries return ``[]``.
    """
    mock = MagicMock()

    def side_effect(sparql: str, *args, **kwargs) -> list[dict[str, str]]:
        for needle, rows in responses.items():
            if needle in sparql:
                return rows
        return []

    mock.query.side_effect = side_effect
    return mock


class TestAnalyzeHandlerImpactAnalyzer:
    """Edge cases for :class:`ImpactAnalyzer` — the engine the handler drives.

    The analyzer's contract is "partial > none": a single pass raising
    drops that pass to an empty list rather than failing the whole
    report. These tests assert that contract against the four passes
    used by ``analyze_impact`` (affected / conflicts / gaps / EU).
    """

    def test_empty_extraction_result_zero_counts(self):
        """No matching rows in any pass → zero counts on every bucket."""
        client = _mock_sparql_client({})
        findings = ImpactAnalyzer(sparql_client=client).analyze(_GRAPH_URI)

        assert isinstance(findings, ImpactFindings)
        assert findings.affected_count == 0
        assert findings.conflict_count == 0
        assert findings.gap_count == 0
        assert findings.affected_entities == []
        assert findings.conflicts == []
        assert findings.gaps == []
        assert findings.eu_compliance == []

    def test_unresolvable_refs_land_in_gaps_not_affected(self):
        """When the draft references topic clusters with no matching
        affected entities, the gap pass surfaces them and the affected
        bucket stays empty.

        This is the SPARQL-level contract: gaps are computed as
        "topic clusters the draft touches without referencing their
        core provisions". A draft whose references all live in
        unmatched clusters has zero affected entities but a populated
        gap list — exactly the shape we assert here.
        """
        client = _mock_sparql_client(
            {
                # No affected entities (the URIs the resolver wrote
                # don't exist in the ontology).
                "SELECT DISTINCT ?entity ?label ?type": [],
                # But there are gap clusters — the draft touches
                # topics without their core provisions.
                "SELECT ?cluster": [
                    {
                        "cluster": "urn:cluster:unknown-1",
                        "clusterLabel": "Tundmatu valdkond",
                        "totalProvisions": "10",
                        "referencedProvisions": "0",
                    }
                ],
            }
        )
        findings = ImpactAnalyzer(sparql_client=client).analyze(_GRAPH_URI)

        assert findings.affected_count == 0
        assert findings.gap_count == 1
        assert findings.gaps[0]["topic_cluster"] == "urn:cluster:unknown-1"
        assert "0 of 10" in findings.gaps[0]["description"]

    def test_conflict_detection_surfaces_supersede_style_rows(self):
        """A row marked as a conflict via the ``draftRef`` pass surfaces
        in :attr:`ImpactFindings.conflicts`, not :attr:`affected_entities`.

        The ``reason`` field is the only place the handler distinguishes
        between supersede / interpretation / other conflict types — the
        SPARQL pass projects whatever the underlying query templates
        produce.
        """
        client = _mock_sparql_client(
            {
                "?draftRef ?conflictEntity": [
                    {
                        "draftRef": "urn:draft-ref-1",
                        "conflictEntity": "urn:enacted-provision-1",
                        "conflictLabel": "KarS § 100",
                        "reason": "Draft supersedes enacted provision",
                    },
                ],
            }
        )
        findings = ImpactAnalyzer(sparql_client=client).analyze(_GRAPH_URI)

        assert findings.conflict_count == 1
        assert findings.affected_count == 0
        conflict = findings.conflicts[0]
        assert conflict["draft_ref"] == "urn:draft-ref-1"
        assert "supersedes" in conflict["reason"]

    def test_eu_compliance_pass_runs_and_returns_status(self):
        """A draft with EU CELEX references surfaces a transposition row."""
        client = _mock_sparql_client(
            {
                "SELECT DISTINCT ?euAct": [
                    {
                        "euAct": "https://data.riik.ee/ontology/estleg#EU_Reg_2016_679",
                        "euLabel": "GDPR",
                        "estonianProvision": "https://data.riik.ee/ontology/estleg#IKS_Par_5",
                        "provisionLabel": "IKS § 5",
                        "relation": "https://data.riik.ee/ontology/estleg#transposesDirective",
                    }
                ]
            }
        )
        findings = ImpactAnalyzer(sparql_client=client).analyze(_GRAPH_URI)

        assert len(findings.eu_compliance) == 1
        eu_row = findings.eu_compliance[0]
        assert eu_row["eu_act"].endswith("EU_Reg_2016_679")
        # The handler always tags rows with ``"linked"`` — the deeper
        # status (transposed / partially-transposed / missing) is a
        # phase-3 follow-up per the analyzer's module docstring.
        assert eu_row["transposition_status"] == "linked"
        assert eu_row["eu_label"] == "GDPR"

    def test_gap_analysis_surfaces_unknown_clusters(self):
        """A draft touching a topic cluster without its core refs surfaces in gaps."""
        client = _mock_sparql_client(
            {
                "SELECT ?cluster": [
                    {
                        "cluster": "https://data.riik.ee/ontology/estleg#topic/andmekaitse",
                        "clusterLabel": "Andmekaitse",
                        "totalProvisions": "25",
                        "referencedProvisions": "3",
                    },
                    {
                        "cluster": "https://data.riik.ee/ontology/estleg#topic/karistusoigus",
                        "clusterLabel": "Karistusõigus",
                        "totalProvisions": "50",
                        "referencedProvisions": "1",
                    },
                ]
            }
        )
        findings = ImpactAnalyzer(sparql_client=client).analyze(_GRAPH_URI)

        assert findings.gap_count == 2
        labels = {g["topic_cluster_label"] for g in findings.gaps}
        assert labels == {"Andmekaitse", "Karistusõigus"}

    def test_one_pass_failure_does_not_kill_others(self):
        """``partial > none`` — a raising SPARQL pass yields ``[]`` for that
        bucket but the other passes still contribute."""
        client = MagicMock()

        def side_effect(sparql: str, *args, **kwargs) -> list[dict[str, str]]:
            if "?draftRef ?conflictEntity" in sparql:
                raise RuntimeError("planner exploded")
            if "SELECT DISTINCT ?entity ?label ?type" in sparql:
                return [{"entity": "urn:foo", "label": "foo", "type": "urn:type"}]
            return []

        client.query.side_effect = side_effect
        findings = ImpactAnalyzer(sparql_client=client).analyze(_GRAPH_URI)

        assert findings.affected_count == 1
        assert findings.conflict_count == 0
        assert findings.gap_count == 0


class TestAnalyzeHandlerScoring:
    """Pure formula tests for :func:`calculate_impact_score`.

    The handler stores ``score = calculate_impact_score(findings)`` in
    ``impact_reports.impact_score``. The formula
    (spec §8.5, simplified Batch-3 variant):

        base             = min(100, affected * 2)
        conflict_penalty = conflicts * 10
        gap_penalty      = gaps * 5
        score            = clamp(base + penalties, 0, 100)
    """

    def _findings(self, *, affected=0, conflicts=0, gaps=0) -> ImpactFindings:
        return ImpactFindings(
            affected_entities=[],
            conflicts=[],
            gaps=[],
            eu_compliance=[],
            affected_count=affected,
            conflict_count=conflicts,
            gap_count=gaps,
        )

    def test_canned_five_affected_two_conflicts_one_gap(self):
        """5 affected (10) + 2 conflicts (20) + 1 gap (5) = 35."""
        score = calculate_impact_score(self._findings(affected=5, conflicts=2, gaps=1))
        assert score == 35

    def test_empty_findings_zero_score(self):
        """Empty report → score 0."""
        score = calculate_impact_score(self._findings())
        assert score == 0

    def test_score_caps_at_100(self):
        """Arbitrary large counts saturate at 100."""
        score = calculate_impact_score(self._findings(affected=500, conflicts=100, gaps=100))
        assert score == 100

    def test_score_floor_is_zero(self):
        """Defensive: negative counts can't drag the score below zero."""
        score = calculate_impact_score(self._findings(affected=-10, conflicts=-5, gaps=-1))
        assert score == 0

    def test_score_purely_from_conflicts(self):
        """0 affected + 3 conflicts → 30 (10 per conflict)."""
        score = calculate_impact_score(self._findings(conflicts=3))
        assert score == 30

    def test_score_purely_from_gaps(self):
        """0 affected + 8 gaps → 40 (5 per gap)."""
        score = calculate_impact_score(self._findings(gaps=8))
        assert score == 40


class TestAnalyzeHandlerRowToResolvedRef:
    """:func:`_row_to_resolved_ref` is the SQL-to-domain bridge used by the
    handler's draft_entities SELECT. It must tolerate the polymorphism
    psycopg's JSONB loader can produce (dict vs string) and the
    partial-match column added by migration 034.
    """

    def test_fully_resolved_row(self):
        """A row with ``entity_uri`` and no ``partial_match`` → match_score == 1.0."""
        row = (
            "KarS § 133",
            "urn:kars-133",
            0.9,
            "provision",
            json.dumps({"chunk": 0, "offset": 0}),
            None,
        )
        resolved = _row_to_resolved_ref(row)
        assert resolved.entity_uri == "urn:kars-133"
        assert resolved.match_score == pytest.approx(1.0)
        assert resolved.partial_match is None
        assert resolved.extracted.ref_text == "KarS § 133"
        assert resolved.extracted.confidence == pytest.approx(0.9)
        assert resolved.extracted.location == {"chunk": 0, "offset": 0}

    def test_partial_match_row(self):
        """A row with ``entity_uri=None`` but populated ``partial_match`` →
        match_score == 0.5 (Wave 2 Step 5 contract)."""
        partial_payload = {
            "act_token": "KARS",
            "act_title": "Karistusseadustik",
            "section": "§ 999",
        }
        row = (
            "Karistusseadustik § 999",
            None,
            0.7,
            "provision",
            json.dumps({}),
            json.dumps(partial_payload),
        )
        resolved = _row_to_resolved_ref(row)
        assert resolved.entity_uri is None
        assert resolved.match_score == pytest.approx(0.5)
        assert resolved.partial_match == partial_payload

    def test_fully_unresolved_row(self):
        """A row with both ``entity_uri`` and ``partial_match`` NULL → match_score == 0.0."""
        row = (
            "Mystery § 1",
            None,
            0.3,
            "provision",
            None,
            None,
        )
        resolved = _row_to_resolved_ref(row)
        assert resolved.entity_uri is None
        assert resolved.match_score == pytest.approx(0.0)
        assert resolved.partial_match is None

    def test_jsonb_columns_tolerate_dict_shape(self):
        """psycopg JSONB type-adapter sometimes returns dicts instead of
        JSON strings — both must work."""
        partial_dict = {"act_token": "TSUS", "act_title": "TsÜS", "section": None}
        location_dict = {"chunk": 2, "offset": 4200, "extra": "data"}
        row = (
            "TsÜS § 12",
            None,
            0.6,
            "provision",
            location_dict,
            partial_dict,
        )
        resolved = _row_to_resolved_ref(row)
        assert resolved.partial_match == partial_dict
        assert resolved.extracted.location == location_dict

    def test_invalid_json_location_falls_back_to_empty_dict(self):
        """A corrupted ``location`` JSON string degrades to ``{}`` rather than raising."""
        row = (
            "§ 5",
            "urn:five",
            0.8,
            "provision",
            "{not-valid-json",
            None,
        )
        resolved = _row_to_resolved_ref(row)
        assert resolved.extracted.location == {}

    def test_invalid_json_partial_match_falls_back_to_none(self):
        """A corrupted ``partial_match`` JSON string degrades to ``None``."""
        row = (
            "§ 6",
            None,
            0.4,
            "provision",
            None,
            "{not-valid-json",
        )
        resolved = _row_to_resolved_ref(row)
        assert resolved.partial_match is None

    def test_non_numeric_confidence_coerced_to_zero(self):
        """A non-numeric ``confidence`` column falls back to ``0.0``."""
        row = (
            "§ 7",
            "urn:seven",
            "not-a-number",
            "provision",
            None,
            None,
        )
        resolved = _row_to_resolved_ref(row)
        assert resolved.extracted.confidence == pytest.approx(0.0)

    def test_ref_type_default_when_null(self):
        """A NULL ``ref_type`` defaults to ``"provision"`` to keep the dataclass valid."""
        row = (
            "§ 8",
            "urn:eight",
            0.5,
            None,
            None,
            None,
        )
        resolved = _row_to_resolved_ref(row)
        assert resolved.extracted.ref_type == "provision"
