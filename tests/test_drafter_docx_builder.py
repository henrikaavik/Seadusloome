"""Tests for the drafter DOCX builder's citation rendering (issue #842, part 2).

The "Lisa A: Viidete register" appendix must NOT print raw, unverified
citation strings as authoritative legal references. Verified enriched
citations render as authoritative (resolver label); unverified ones —
including legacy raw strings from old sessions — are explicitly marked
"kontrollimata viide: ...".
"""

from __future__ import annotations

from pathlib import Path

from docx import Document

from app.drafter.docx_builder import build_drafter_docx


def _appendix_texts(path: Path) -> list[str]:
    """Reopen the saved .docx and return all paragraph texts."""
    doc = Document(str(path))
    return [p.text for p in doc.paragraphs]


def _build_with_citations(tmp_path: Path, monkeypatch) -> list[str]:
    """Build a minimal full_law doc whose single clause carries a mix of
    verified / unverified / legacy citations, then return its paragraph texts.
    """
    monkeypatch.setenv("EXPORT_DIR", str(tmp_path))

    clauses = [
        {
            "chapter": "1",
            "paragraph": "§ 1",
            "text": "Klausli tekst.",
            "citations": [
                # Verified enriched dict -> authoritative.
                {
                    "text": "HKTS § 13",
                    "verified": True,
                    "label": "HKTS § 13",
                    "resolved_uri": "https://example.org/hkts/13",
                    "explorer_url": "/explorer?focus=https://example.org/hkts/13",
                },
                # Unverified enriched dict -> marked kontrollimata.
                {
                    "text": "Foo § 9",
                    "verified": False,
                    "label": "Foo § 9",
                    "resolved_uri": None,
                    "explorer_url": None,
                },
                # Legacy raw string -> coerced to unverified -> kontrollimata.
                "estleg:Bar/par/1",
            ],
        },
    ]
    structure = {
        "chapters": [
            {
                "number": "1",
                "title": "Üldsätted",
                "sections": [{"paragraph": "§ 1", "title": "Reguleerimisala"}],
            }
        ]
    }

    out_path = build_drafter_docx(
        session_id="test-session",
        title="Testeelnõu",
        workflow_type="full_law",
        structure=structure,
        clauses=clauses,
    )
    return _appendix_texts(out_path)


def test_verified_citation_is_authoritative(tmp_path, monkeypatch):
    texts = _build_with_citations(tmp_path, monkeypatch)
    blob = "\n".join(texts)

    # The verified label appears...
    assert any("HKTS § 13" in t for t in texts)
    # ...and is NOT marked as unverified.
    verified_lines = [t for t in texts if "HKTS § 13" in t]
    assert verified_lines, "verified citation line missing"
    for line in verified_lines:
        assert "kontrollimata" not in line, (
            f"verified citation must not carry the kontrollimata prefix: {line!r}"
        )

    # Sanity: the appendix heading is present.
    assert "Lisa A: Viidete register" in blob


def test_unverified_dict_is_marked(tmp_path, monkeypatch):
    texts = _build_with_citations(tmp_path, monkeypatch)
    assert any("kontrollimata viide: Foo § 9" in t for t in texts), (
        "unverified dict citation must be marked kontrollimata"
    )


def test_legacy_string_is_marked(tmp_path, monkeypatch):
    texts = _build_with_citations(tmp_path, monkeypatch)
    assert any("kontrollimata viide: estleg:Bar/par/1" in t for t in texts), (
        "legacy raw-string citation must be marked kontrollimata"
    )
