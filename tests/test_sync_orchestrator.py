"""Unit tests for the sync orchestrator — focused on SHACL warning handling.

Covers the decision made in #440: SHACL violations must be logged and stored
in the sync_log, but MUST NOT abort the sync or cause `run_sync()` to return
False. The whole pipeline from clone to Jena upload is mocked so these tests
never touch the network, the DB, or Jena.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from rdflib import Graph

from app.sync.orchestrator import _parse_violation_count, run_sync


class TestParseViolationCount:
    def test_parses_simple_count(self):
        assert _parse_violation_count("Results (2634)") == 2634

    def test_parses_embedded_line(self):
        assert _parse_violation_count("Validation Report: Results (213):") == 213

    def test_zero_when_not_found(self):
        assert _parse_violation_count("no match here") == 0

    def test_zero_on_unparseable(self):
        # pyshacl will never emit Results (abc), but handle it gracefully
        assert _parse_violation_count("Results (abc)") == 0


# ---------------------------------------------------------------------------
# SHACL-warning path in run_sync
# ---------------------------------------------------------------------------


@pytest.fixture
def fake_repo(tmp_path: Path) -> Path:
    """Build a minimal fake ontology repo with an empty shacl/ directory
    so the orchestrator enters the validation branch."""
    shacl_dir = tmp_path / "shacl"
    shacl_dir.mkdir()
    # A tiny Turtle file so `any(shapes_dir.iterdir())` is True
    (shacl_dir / "placeholder.ttl").write_text(
        "@prefix sh: <http://www.w3.org/ns/shacl#> .\n", encoding="utf-8"
    )
    return tmp_path


@patch("app.sync.orchestrator._get_notify_fn", return_value=None)
@patch("app.sync.orchestrator.get_triple_count", return_value=42)
@patch("app.sync.orchestrator.upload_turtle", return_value=True)
@patch("app.sync.orchestrator.clear_default_graph")
@patch("app.sync.orchestrator.serialize_to_turtle", return_value="# turtle")
@patch("app.sync.orchestrator.validate_graph")
@patch("app.sync.orchestrator.load_shapes", return_value=Graph())
@patch("app.sync.orchestrator.convert_ontology")
@patch("app.sync.orchestrator.clone_or_pull")
@patch("app.sync.orchestrator.log_sync")
def test_run_sync_continues_on_shacl_violations(
    mock_log_sync: MagicMock,
    mock_clone: MagicMock,
    mock_convert: MagicMock,
    mock_load_shapes: MagicMock,
    mock_validate: MagicMock,
    mock_serialize: MagicMock,
    mock_clear: MagicMock,
    mock_upload: MagicMock,
    mock_triple_count: MagicMock,
    mock_notify: MagicMock,
    fake_repo: Path,
):
    """SHACL violations should log a WARNING but the sync must still
    upload and return True. log_sync must be called with status=success
    and an error_message that begins with 'WARN: SHACL'."""
    # Arrange: converter produces a non-empty graph
    fake_graph = Graph()
    fake_graph.parse(data="@prefix ex: <http://example.org/> . ex:a ex:p ex:b .", format="turtle")
    mock_convert.return_value = fake_graph

    # Load shapes returns a non-empty graph so the validation branch runs.
    # We pass in a real Graph so `len(shapes) > 0` is True after patching.
    shapes_graph = Graph()
    shapes_graph.parse(
        data="@prefix sh: <http://www.w3.org/ns/shacl#> . sh:x a sh:NodeShape .",
        format="turtle",
    )
    mock_load_shapes.return_value = shapes_graph

    # Make validation return "not conforms" with a fake report.
    mock_validate.return_value = (False, "Validation Report\nResults (213):\nConstraint Violation")

    # Act
    result = run_sync(repo_dir=fake_repo)

    # Assert: sync succeeded end-to-end
    assert result is True
    mock_upload.assert_called_once()
    mock_clear.assert_called_once()

    # log_sync was called twice? No — only once at the end. Check the final call.
    final_call = mock_log_sync.call_args_list[-1]
    args, kwargs = final_call
    assert (
        args[0] == "success"
        or kwargs.get("status") == "success"
        or kwargs == {}
        and args[0] == "success"
    )
    # The warning should be in error_message
    error_message = kwargs.get("error_message") or (args[3] if len(args) > 3 else None)
    assert error_message is not None, f"log_sync final call had no error_message: {final_call}"
    assert "WARN: SHACL" in error_message
    assert "213" in error_message


@patch("app.sync.orchestrator._get_notify_fn", return_value=None)
@patch("app.sync.orchestrator.get_triple_count", return_value=42)
@patch("app.sync.orchestrator.upload_turtle", return_value=True)
@patch("app.sync.orchestrator.clear_default_graph")
@patch("app.sync.orchestrator.serialize_to_turtle", return_value="# turtle")
@patch("app.sync.orchestrator.validate_graph")
@patch("app.sync.orchestrator.load_shapes", return_value=Graph())
@patch("app.sync.orchestrator.convert_ontology")
@patch("app.sync.orchestrator.clone_or_pull")
@patch("app.sync.orchestrator.log_sync")
def test_run_sync_clean_validation_has_no_warning_message(
    mock_log_sync: MagicMock,
    mock_clone: MagicMock,
    mock_convert: MagicMock,
    mock_load_shapes: MagicMock,
    mock_validate: MagicMock,
    mock_serialize: MagicMock,
    mock_clear: MagicMock,
    mock_upload: MagicMock,
    mock_triple_count: MagicMock,
    mock_notify: MagicMock,
    fake_repo: Path,
):
    """When SHACL passes cleanly, log_sync gets error_message=None."""
    fake_graph = Graph()
    fake_graph.parse(data="@prefix ex: <http://example.org/> . ex:a ex:p ex:b .", format="turtle")
    mock_convert.return_value = fake_graph

    shapes_graph = Graph()
    shapes_graph.parse(
        data="@prefix sh: <http://www.w3.org/ns/shacl#> . sh:x a sh:NodeShape .",
        format="turtle",
    )
    mock_load_shapes.return_value = shapes_graph

    # Clean validation
    mock_validate.return_value = (True, "")

    result = run_sync(repo_dir=fake_repo)
    assert result is True

    # log_sync final call should have error_message=None
    final_call = mock_log_sync.call_args_list[-1]
    _args, kwargs = final_call
    assert kwargs.get("error_message") is None


@patch("app.sync.orchestrator._get_notify_fn", return_value=None)
@patch("app.sync.orchestrator.upload_turtle", return_value=False)
@patch("app.sync.orchestrator.clear_default_graph")
@patch("app.sync.orchestrator.serialize_to_turtle", return_value="# turtle")
@patch("app.sync.orchestrator.validate_graph", return_value=(True, ""))
@patch("app.sync.orchestrator.load_shapes", return_value=Graph())
@patch("app.sync.orchestrator.convert_ontology")
@patch("app.sync.orchestrator.clone_or_pull")
@patch("app.sync.orchestrator.log_sync")
def test_run_sync_still_fails_on_upload_error(
    mock_log_sync: MagicMock,
    mock_clone: MagicMock,
    mock_convert: MagicMock,
    mock_load_shapes: MagicMock,
    mock_validate: MagicMock,
    mock_serialize: MagicMock,
    mock_clear: MagicMock,
    mock_upload: MagicMock,
    mock_notify: MagicMock,
    fake_repo: Path,
):
    """Upload failures are still fatal — only SHACL violations were downgraded."""
    fake_graph = Graph()
    fake_graph.parse(data="@prefix ex: <http://example.org/> . ex:a ex:p ex:b .", format="turtle")
    mock_convert.return_value = fake_graph

    # Make the shapes graph non-empty so validation runs (and passes).
    mock_load_shapes.return_value = Graph()
    # But since len(Graph()) == 0, the validation branch is skipped entirely.
    # That is fine for this test — we just need upload to fail.

    result = run_sync(repo_dir=fake_repo)
    assert result is False

    # log_sync was called with status="failed"
    final_call = mock_log_sync.call_args_list[-1]
    args, kwargs = final_call
    status = args[0] if args else kwargs.get("status")
    assert status == "failed"
