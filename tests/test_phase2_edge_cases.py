"""Phase 2 document-processing edge-case tests (#309 start).

Uses the ``tests/fixtures/drafts/`` `.docx` fixtures (created by
``__generate__.py``) to exercise the parse / extract handlers'
edge-case behaviour without needing a live Tika sidecar or real LLM:

    * ``python-docx`` parses every fixture cleanly (length sanity
      checks — proxies for the structured-output guarantee the real
      Tika-backed parser makes).
    * :func:`app.docs.entity_extractor.extract_refs_from_text` returns
      the right shape on empty / near-empty input.
    * :func:`extract_refs_from_text` runs end-to-end with a mocked
      :class:`app.llm.LLMProvider` that replays the references actually
      present in ``many_references.docx``; the dedupe step collapses
      cross-chunk duplicates and the result counts match the fixture.
    * :func:`extract_refs_from_text` degrades gracefully on the
      ``malformed_refs.docx`` fixture (no exceptions, partial
      extraction).

Sprint 3 will add the full parse / extract / analyze unit-test suite
(file-by-file mock harness mirroring :mod:`tests.test_docs_parse_handler`).
This file covers the happy + edge paths that gate the next sprint of
work.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest
from docx import Document

from app.docs.entity_extractor import ExtractedRef, extract_refs_from_text

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
