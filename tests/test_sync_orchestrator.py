"""Unit tests for the sync orchestrator.

Covers:

* The SHACL warning decision from #440 — violations must be logged and
  stored in the sync_log but MUST NOT abort the sync or cause
  ``run_sync()`` to return False.
* The live-progress contract from #567 — a ``running`` row is written
  before any pipeline phase executes, step labels flip as the
  orchestrator progresses, and the row is updated (not duplicated) on
  terminal state.
* The staged-publish flow from #573 — sync uploads to a staging named
  graph, verifies the triple count against a configurable threshold,
  and only then atomically COPIES the staging graph into the default
  graph. Failures along the way MUST leave the live default graph
  untouched.

The whole pipeline from clone to Jena upload is mocked so these tests
never touch the network, the DB, or Jena.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from rdflib import Graph

from app.sync.orchestrator import (
    PHASE_CLONING,
    PHASE_CONVERTING,
    PHASE_REINGESTING,
    PHASE_UPLOADING,
    PHASE_VALIDATING,
    _parse_violation_count,
    run_sync,
)


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
# Test helpers
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


def _common_patches():
    """Collect the MagicMock patchers used by most tests.

    Returns a dict suitable for ``with patch.multiple(...)`` — the test
    functions below use nested decorators for clarity but this gives a
    central place to see the defaults.

    Updated for #573: the sync now stages into a named graph, verifies
    the triple count, and COPIES to default. The defaults below set
    ``graph_triple_count`` to a value comfortably above the default
    ``SYNC_MIN_TRIPLES`` threshold so unrelated tests don't trip the
    verification gate.
    """
    return {
        "convert_ontology": MagicMock(return_value=Graph()),
        "load_shapes": MagicMock(return_value=Graph()),
        "validate_graph": MagicMock(return_value=(True, "")),
        "serialize_to_turtle": MagicMock(return_value="# turtle"),
        "drop_graph": MagicMock(return_value=True),
        "upload_turtle_to_named_graph": MagicMock(return_value=True),
        "copy_graph_to_default": MagicMock(return_value=True),
        "graph_triple_count": MagicMock(return_value=2_000_000),
    }


# ---------------------------------------------------------------------------
# SHACL-warning path in run_sync (#440)
# ---------------------------------------------------------------------------


@patch("app.sync.orchestrator._get_notify_fn", return_value=None)
@patch("app.sync.orchestrator.graph_triple_count", return_value=2_000_000)
@patch("app.sync.orchestrator.copy_graph_to_default", return_value=True)
@patch("app.sync.orchestrator.upload_turtle_to_named_graph", return_value=True)
@patch("app.sync.orchestrator.drop_graph", return_value=True)
@patch("app.sync.orchestrator.serialize_to_turtle", return_value="# turtle")
@patch("app.sync.orchestrator.validate_graph")
@patch("app.sync.orchestrator.load_shapes", return_value=Graph())
@patch("app.sync.orchestrator.convert_ontology")
@patch("app.sync.orchestrator.clone_or_pull")
@patch("app.sync.orchestrator._finalize_row")
@patch("app.sync.orchestrator._update_step")
@patch("app.sync.orchestrator._insert_running_row", return_value=1)
def test_run_sync_continues_on_shacl_violations(
    mock_insert: MagicMock,
    mock_update_step: MagicMock,
    mock_finalize: MagicMock,
    mock_clone: MagicMock,
    mock_convert: MagicMock,
    mock_load_shapes: MagicMock,
    mock_validate: MagicMock,
    mock_serialize: MagicMock,
    mock_drop: MagicMock,
    mock_upload_staging: MagicMock,
    mock_copy: MagicMock,
    mock_count: MagicMock,
    mock_notify: MagicMock,
    fake_repo: Path,
):
    """SHACL violations should log a WARNING but the sync must still
    upload and return True. _finalize_row must be called with
    status=success and an error_message that begins with 'WARN: SHACL'.

    Also asserts the staged-publish contract (#573): upload goes to a
    staging graph and is promoted via COPY to the default graph.
    """
    fake_graph = Graph()
    fake_graph.parse(data="@prefix ex: <http://example.org/> . ex:a ex:p ex:b .", format="turtle")
    mock_convert.return_value = fake_graph

    shapes_graph = Graph()
    shapes_graph.parse(
        data="@prefix sh: <http://www.w3.org/ns/shacl#> . sh:x a sh:NodeShape .",
        format="turtle",
    )
    mock_load_shapes.return_value = shapes_graph

    mock_validate.return_value = (False, "Validation Report\nResults (213):\nConstraint Violation")

    result = run_sync(repo_dir=fake_repo)

    assert result is True
    # Staging flow: upload to staging graph, then COPY to default.
    mock_upload_staging.assert_called_once()
    mock_copy.assert_called_once()
    # drop_graph runs at least twice: stale-clear at start, post-promote cleanup.
    assert mock_drop.call_count >= 2

    final_call = mock_finalize.call_args
    args, kwargs = final_call
    # _finalize_row(log_id, status, started_at, entity_count=..., error_message=...)
    assert args[1] == "success"
    error_message = kwargs.get("error_message")
    assert error_message is not None
    assert "WARN: SHACL" in error_message
    assert "213" in error_message


@patch("app.sync.orchestrator._get_notify_fn", return_value=None)
@patch("app.sync.orchestrator.graph_triple_count", return_value=2_000_000)
@patch("app.sync.orchestrator.copy_graph_to_default", return_value=True)
@patch("app.sync.orchestrator.upload_turtle_to_named_graph", return_value=True)
@patch("app.sync.orchestrator.drop_graph", return_value=True)
@patch("app.sync.orchestrator.serialize_to_turtle", return_value="# turtle")
@patch("app.sync.orchestrator.validate_graph")
@patch("app.sync.orchestrator.load_shapes", return_value=Graph())
@patch("app.sync.orchestrator.convert_ontology")
@patch("app.sync.orchestrator.clone_or_pull")
@patch("app.sync.orchestrator._finalize_row")
@patch("app.sync.orchestrator._update_step")
@patch("app.sync.orchestrator._insert_running_row", return_value=1)
def test_run_sync_clean_validation_has_no_warning_message(
    mock_insert: MagicMock,
    mock_update_step: MagicMock,
    mock_finalize: MagicMock,
    mock_clone: MagicMock,
    mock_convert: MagicMock,
    mock_load_shapes: MagicMock,
    mock_validate: MagicMock,
    mock_serialize: MagicMock,
    mock_drop: MagicMock,
    mock_upload_staging: MagicMock,
    mock_copy: MagicMock,
    mock_count: MagicMock,
    mock_notify: MagicMock,
    fake_repo: Path,
):
    """When SHACL passes cleanly, _finalize_row gets error_message=None."""
    fake_graph = Graph()
    fake_graph.parse(data="@prefix ex: <http://example.org/> . ex:a ex:p ex:b .", format="turtle")
    mock_convert.return_value = fake_graph

    shapes_graph = Graph()
    shapes_graph.parse(
        data="@prefix sh: <http://www.w3.org/ns/shacl#> . sh:x a sh:NodeShape .",
        format="turtle",
    )
    mock_load_shapes.return_value = shapes_graph

    mock_validate.return_value = (True, "")

    result = run_sync(repo_dir=fake_repo)
    assert result is True

    _args, kwargs = mock_finalize.call_args
    assert kwargs.get("error_message") is None


@patch("app.sync.orchestrator._get_notify_fn", return_value=None)
@patch("app.sync.orchestrator.graph_triple_count", return_value=2_000_000)
@patch("app.sync.orchestrator.copy_graph_to_default", return_value=True)
@patch("app.sync.orchestrator.upload_turtle_to_named_graph", return_value=False)
@patch("app.sync.orchestrator.drop_graph", return_value=True)
@patch("app.sync.orchestrator.serialize_to_turtle", return_value="# turtle")
@patch("app.sync.orchestrator.validate_graph", return_value=(True, ""))
@patch("app.sync.orchestrator.load_shapes", return_value=Graph())
@patch("app.sync.orchestrator.convert_ontology")
@patch("app.sync.orchestrator.clone_or_pull")
@patch("app.sync.orchestrator._finalize_row")
@patch("app.sync.orchestrator._update_step")
@patch("app.sync.orchestrator._insert_running_row", return_value=1)
def test_run_sync_fails_when_staging_upload_fails(
    mock_insert: MagicMock,
    mock_update_step: MagicMock,
    mock_finalize: MagicMock,
    mock_clone: MagicMock,
    mock_convert: MagicMock,
    mock_load_shapes: MagicMock,
    mock_validate: MagicMock,
    mock_serialize: MagicMock,
    mock_drop: MagicMock,
    mock_upload_staging: MagicMock,
    mock_copy: MagicMock,
    mock_count: MagicMock,
    mock_notify: MagicMock,
    fake_repo: Path,
):
    """Staging-upload failures are fatal but must NOT touch the live default graph."""
    fake_graph = Graph()
    fake_graph.parse(data="@prefix ex: <http://example.org/> . ex:a ex:p ex:b .", format="turtle")
    mock_convert.return_value = fake_graph
    mock_load_shapes.return_value = Graph()

    result = run_sync(repo_dir=fake_repo)
    assert result is False
    # Live default graph untouched — COPY must not have been attempted.
    mock_copy.assert_not_called()

    args, kwargs = mock_finalize.call_args
    assert args[1] == "failed"
    error_message = kwargs.get("error_message")
    assert error_message is not None
    assert "staging" in error_message.lower()
    assert "intact" in error_message.lower()


# ---------------------------------------------------------------------------
# #573: Staging verification threshold — staging count below threshold
# must abort without touching the live default graph.
# ---------------------------------------------------------------------------


@patch("app.sync.orchestrator._get_notify_fn", return_value=None)
@patch("app.sync.orchestrator.copy_graph_to_default", return_value=True)
@patch("app.sync.orchestrator.upload_turtle_to_named_graph", return_value=True)
@patch("app.sync.orchestrator.drop_graph", return_value=True)
@patch("app.sync.orchestrator.serialize_to_turtle", return_value="# turtle")
@patch("app.sync.orchestrator.validate_graph", return_value=(True, ""))
@patch("app.sync.orchestrator.load_shapes", return_value=Graph())
@patch("app.sync.orchestrator.convert_ontology")
@patch("app.sync.orchestrator.clone_or_pull")
@patch("app.sync.orchestrator._finalize_row")
@patch("app.sync.orchestrator._update_step")
@patch("app.sync.orchestrator._insert_running_row", return_value=1)
def test_run_sync_fails_when_staging_verification_below_threshold(
    mock_insert: MagicMock,
    mock_update_step: MagicMock,
    mock_finalize: MagicMock,
    mock_clone: MagicMock,
    mock_convert: MagicMock,
    mock_load_shapes: MagicMock,
    mock_validate: MagicMock,
    mock_serialize: MagicMock,
    mock_drop: MagicMock,
    mock_upload_staging: MagicMock,
    mock_copy: MagicMock,
    mock_notify: MagicMock,
    fake_repo: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    """Staging count below the threshold must abort the sync and leave
    the live default graph untouched (no COPY attempted)."""
    mock_convert.return_value = Graph()
    mock_load_shapes.return_value = Graph()
    # Force a low floor so we can construct a failing case with small numbers.
    monkeypatch.setenv("SYNC_MIN_TRIPLES", "100")

    # graph_triple_count is called twice: once for STAGING, once for live.
    # Return staging=10 (below threshold=100) and live=0 (so 80% floor is 0).
    with patch(
        "app.sync.orchestrator.graph_triple_count",
        side_effect=[10, 0],
    ):
        result = run_sync(repo_dir=fake_repo)

    assert result is False
    mock_copy.assert_not_called()

    args, kwargs = mock_finalize.call_args
    assert args[1] == "failed"
    error_message = kwargs.get("error_message")
    assert error_message is not None
    assert "staging verification failed" in error_message.lower()
    assert "10" in error_message


# ---------------------------------------------------------------------------
# #573: Promote failure — COPY returns False, sync marked failed.
# ---------------------------------------------------------------------------


@patch("app.sync.orchestrator._get_notify_fn", return_value=None)
@patch("app.sync.orchestrator.graph_triple_count", return_value=2_000_000)
@patch("app.sync.orchestrator.copy_graph_to_default", return_value=False)
@patch("app.sync.orchestrator.upload_turtle_to_named_graph", return_value=True)
@patch("app.sync.orchestrator.drop_graph", return_value=True)
@patch("app.sync.orchestrator.serialize_to_turtle", return_value="# turtle")
@patch("app.sync.orchestrator.validate_graph", return_value=(True, ""))
@patch("app.sync.orchestrator.load_shapes", return_value=Graph())
@patch("app.sync.orchestrator.convert_ontology")
@patch("app.sync.orchestrator.clone_or_pull")
@patch("app.sync.orchestrator._finalize_row")
@patch("app.sync.orchestrator._update_step")
@patch("app.sync.orchestrator._insert_running_row", return_value=1)
def test_run_sync_fails_when_copy_to_default_fails(
    mock_insert: MagicMock,
    mock_update_step: MagicMock,
    mock_finalize: MagicMock,
    mock_clone: MagicMock,
    mock_convert: MagicMock,
    mock_load_shapes: MagicMock,
    mock_validate: MagicMock,
    mock_serialize: MagicMock,
    mock_drop: MagicMock,
    mock_upload_staging: MagicMock,
    mock_copy: MagicMock,
    mock_count: MagicMock,
    mock_notify: MagicMock,
    fake_repo: Path,
):
    """copy_graph_to_default() returning False must mark the sync failed."""
    mock_convert.return_value = Graph()
    mock_load_shapes.return_value = Graph()

    result = run_sync(repo_dir=fake_repo)

    assert result is False
    mock_copy.assert_called_once()

    args, kwargs = mock_finalize.call_args
    assert args[1] == "failed"
    error_message = kwargs.get("error_message")
    assert error_message is not None
    assert "promote" in error_message.lower() or "copy" in error_message.lower()


# ---------------------------------------------------------------------------
# #573: Post-promote zero count — previously a warning, now a hard fail.
# ---------------------------------------------------------------------------


@patch("app.sync.orchestrator._get_notify_fn", return_value=None)
@patch("app.sync.orchestrator.copy_graph_to_default", return_value=True)
@patch("app.sync.orchestrator.upload_turtle_to_named_graph", return_value=True)
@patch("app.sync.orchestrator.drop_graph", return_value=True)
@patch("app.sync.orchestrator.serialize_to_turtle", return_value="# turtle")
@patch("app.sync.orchestrator.validate_graph", return_value=(True, ""))
@patch("app.sync.orchestrator.load_shapes", return_value=Graph())
@patch("app.sync.orchestrator.convert_ontology")
@patch("app.sync.orchestrator.clone_or_pull")
@patch("app.sync.orchestrator._finalize_row")
@patch("app.sync.orchestrator._update_step")
@patch("app.sync.orchestrator._insert_running_row", return_value=1)
def test_run_sync_fails_when_post_promote_count_is_zero(
    mock_insert: MagicMock,
    mock_update_step: MagicMock,
    mock_finalize: MagicMock,
    mock_clone: MagicMock,
    mock_convert: MagicMock,
    mock_load_shapes: MagicMock,
    mock_validate: MagicMock,
    mock_serialize: MagicMock,
    mock_drop: MagicMock,
    mock_upload_staging: MagicMock,
    mock_copy: MagicMock,
    mock_notify: MagicMock,
    fake_repo: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    """A successful COPY that results in zero triples in the default
    graph is now a hard failure (#573) — previously it was only a
    warning."""
    mock_convert.return_value = Graph()
    mock_load_shapes.return_value = Graph()
    monkeypatch.setenv("SYNC_MIN_TRIPLES", "1")

    # graph_triple_count call sequence:
    #   1. staging (verification) — must pass threshold
    #   2. live pre-promote
    #   3. default post-promote — ZERO triggers the hard fail
    with patch(
        "app.sync.orchestrator.graph_triple_count",
        side_effect=[500, 0, 0],
    ):
        result = run_sync(repo_dir=fake_repo)

    assert result is False

    args, kwargs = mock_finalize.call_args
    assert args[1] == "failed"
    error_message = kwargs.get("error_message")
    assert error_message is not None
    assert "zero triples" in error_message.lower()


# ---------------------------------------------------------------------------
# Live-progress contract (#567)
# ---------------------------------------------------------------------------


@patch("app.sync.orchestrator._get_notify_fn", return_value=None)
@patch("app.sync.orchestrator.graph_triple_count", return_value=2_000_000)
@patch("app.sync.orchestrator.copy_graph_to_default", return_value=True)
@patch("app.sync.orchestrator.upload_turtle_to_named_graph", return_value=True)
@patch("app.sync.orchestrator.drop_graph", return_value=True)
@patch("app.sync.orchestrator.serialize_to_turtle", return_value="# turtle")
@patch("app.sync.orchestrator.validate_graph", return_value=(True, ""))
@patch("app.sync.orchestrator.load_shapes", return_value=Graph())
@patch("app.sync.orchestrator.convert_ontology")
@patch("app.sync.orchestrator.clone_or_pull")
@patch("app.sync.orchestrator._finalize_row")
@patch("app.sync.orchestrator._update_step")
@patch("app.sync.orchestrator._insert_running_row", return_value=42)
def test_run_sync_writes_running_row_before_any_phase(
    mock_insert: MagicMock,
    mock_update_step: MagicMock,
    mock_finalize: MagicMock,
    mock_clone: MagicMock,
    mock_convert: MagicMock,
    mock_load_shapes: MagicMock,
    mock_validate: MagicMock,
    mock_serialize: MagicMock,
    mock_drop: MagicMock,
    mock_upload_staging: MagicMock,
    mock_copy: MagicMock,
    mock_count: MagicMock,
    mock_notify: MagicMock,
    fake_repo: Path,
):
    """A 'running' row must be inserted before the first clone/convert
    call, so the admin UI can surface progress immediately when the
    sync is kicked off."""
    fake_graph = Graph()
    mock_convert.return_value = fake_graph

    result = run_sync(repo_dir=fake_repo)
    assert result is True

    # Exactly one running row written and exactly one finalize call
    assert mock_insert.call_count == 1
    assert mock_finalize.call_count == 1

    # Every step transition references the id returned by _insert_running_row.
    assert all(call.args[0] == 42 for call in mock_update_step.call_args_list)


@patch("app.sync.orchestrator._get_notify_fn", return_value=None)
@patch("app.sync.orchestrator.graph_triple_count", return_value=2_000_000)
@patch("app.sync.orchestrator.copy_graph_to_default", return_value=True)
@patch("app.sync.orchestrator.upload_turtle_to_named_graph", return_value=True)
@patch("app.sync.orchestrator.drop_graph", return_value=True)
@patch("app.sync.orchestrator.serialize_to_turtle", return_value="# turtle")
@patch("app.sync.orchestrator.validate_graph", return_value=(True, ""))
@patch("app.sync.orchestrator.load_shapes", return_value=Graph())
@patch("app.sync.orchestrator.convert_ontology")
@patch("app.sync.orchestrator.clone_or_pull")
@patch("app.sync.orchestrator._finalize_row")
@patch("app.sync.orchestrator._update_step")
@patch("app.sync.orchestrator._insert_running_row")
def test_run_sync_skips_insert_when_caller_provides_log_id(
    mock_insert: MagicMock,
    mock_update_step: MagicMock,
    mock_finalize: MagicMock,
    mock_clone: MagicMock,
    mock_convert: MagicMock,
    mock_load_shapes: MagicMock,
    mock_validate: MagicMock,
    mock_serialize: MagicMock,
    mock_drop: MagicMock,
    mock_upload_staging: MagicMock,
    mock_copy: MagicMock,
    mock_count: MagicMock,
    mock_notify: MagicMock,
    fake_repo: Path,
):
    """The admin POST handler inserts the running row synchronously and
    passes the id; run_sync must NOT create a duplicate row."""
    mock_convert.return_value = Graph()

    result = run_sync(repo_dir=fake_repo, log_id=55)
    assert result is True

    # No duplicate insert — caller already provided the row id.
    mock_insert.assert_not_called()
    # Finalize uses the caller-provided id.
    args, _ = mock_finalize.call_args
    assert args[0] == 55


@patch("app.sync.orchestrator._get_notify_fn", return_value=None)
@patch("app.sync.orchestrator.graph_triple_count", return_value=2_000_000)
@patch("app.sync.orchestrator.copy_graph_to_default", return_value=True)
@patch("app.sync.orchestrator.upload_turtle_to_named_graph", return_value=True)
@patch("app.sync.orchestrator.drop_graph", return_value=True)
@patch("app.sync.orchestrator.serialize_to_turtle", return_value="# turtle")
@patch("app.sync.orchestrator.validate_graph", return_value=(True, ""))
@patch("app.sync.orchestrator.load_shapes", return_value=Graph())
@patch("app.sync.orchestrator.convert_ontology")
@patch("app.sync.orchestrator.clone_or_pull")
@patch("app.sync.orchestrator._finalize_row")
@patch("app.sync.orchestrator._update_step")
@patch("app.sync.orchestrator._insert_running_row", return_value=7)
def test_run_sync_emits_all_five_phase_labels_in_order(
    mock_insert: MagicMock,
    mock_update_step: MagicMock,
    mock_finalize: MagicMock,
    mock_clone: MagicMock,
    mock_convert: MagicMock,
    mock_load_shapes: MagicMock,
    mock_validate: MagicMock,
    mock_serialize: MagicMock,
    mock_drop: MagicMock,
    mock_upload_staging: MagicMock,
    mock_copy: MagicMock,
    mock_count: MagicMock,
    mock_notify: MagicMock,
    fake_repo: Path,
):
    """Phase labels must flip in the documented order so the UI's
    progress pills progress left-to-right."""
    mock_convert.return_value = Graph()

    run_sync(repo_dir=fake_repo)

    emitted_steps = [call.args[1] for call in mock_update_step.call_args_list]
    expected_order = [
        PHASE_CLONING,
        PHASE_CONVERTING,
        PHASE_VALIDATING,
        PHASE_UPLOADING,
        PHASE_REINGESTING,
    ]
    # Every expected phase must appear, in order. Allow extra updates in
    # between (future-proofing) but enforce the ordering constraint.
    it = iter(emitted_steps)
    for expected in expected_order:
        assert expected in it, f"Phase {expected} missing or out of order in {emitted_steps}"
