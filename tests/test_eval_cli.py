"""Tests for ``scripts/run_evals.py``.

Verifies the CLI runs and outputs valid JSON with the expected structure.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

_PROJECT_ROOT = str(Path(__file__).resolve().parent.parent)


class TestEvalCLI:
    def test_all_scenario_outputs_valid_json(self):
        """Running with --scenario all produces valid JSON."""
        result = subprocess.run(
            [sys.executable, "scripts/run_evals.py", "--scenario", "all"],
            capture_output=True,
            text=True,
            cwd=_PROJECT_ROOT,
        )
        assert result.returncode == 0
        output = json.loads(result.stdout)
        assert "timestamp" in output
        assert output["scenario"] == "all"
        assert "results" in output
        assert isinstance(output["results"], list)
        assert "summary" in output
        summary = output["summary"]
        assert "total" in summary
        assert "passed" in summary
        assert "failed" in summary
        assert "skipped" in summary

    def test_chat_scenario(self):
        """Running with --scenario chat only includes chat evals."""
        result = subprocess.run(
            [sys.executable, "scripts/run_evals.py", "--scenario", "chat"],
            capture_output=True,
            text=True,
            cwd=_PROJECT_ROOT,
        )
        assert result.returncode == 0
        output = json.loads(result.stdout)
        assert output["scenario"] == "chat"
        names = [r["name"] for r in output["results"]]
        assert "basic_greeting" in names
        assert "provision_lookup" in names
        # drafter evals should NOT be present
        assert "vtk_structure_quality" not in names

    def test_drafter_scenario(self):
        """Running with --scenario drafter only includes drafter evals."""
        result = subprocess.run(
            [sys.executable, "scripts/run_evals.py", "--scenario", "drafter"],
            capture_output=True,
            text=True,
            cwd=_PROJECT_ROOT,
        )
        assert result.returncode == 0
        output = json.loads(result.stdout)
        assert output["scenario"] == "drafter"
        names = [r["name"] for r in output["results"]]
        assert "vtk_structure_quality" in names
        assert "basic_greeting" not in names

    def test_all_results_are_skipped(self):
        """All placeholder scenarios should have status 'skip'."""
        result = subprocess.run(
            [sys.executable, "scripts/run_evals.py", "--scenario", "all"],
            capture_output=True,
            text=True,
            cwd=_PROJECT_ROOT,
        )
        output = json.loads(result.stdout)
        for r in output["results"]:
            assert r["status"] == "skip"
        assert output["summary"]["skipped"] == output["summary"]["total"]
        assert output["summary"]["passed"] == 0
        assert output["summary"]["failed"] == 0

    def test_summary_counts_match(self):
        """Summary counts should add up to total."""
        result = subprocess.run(
            [sys.executable, "scripts/run_evals.py"],
            capture_output=True,
            text=True,
            cwd=_PROJECT_ROOT,
        )
        output = json.loads(result.stdout)
        summary = output["summary"]
        assert summary["total"] == summary["passed"] + summary["failed"] + summary["skipped"]
        assert summary["total"] == len(output["results"])

    def test_timestamp_is_iso_format(self):
        """Timestamp should be a valid ISO 8601 string."""
        from datetime import datetime

        result = subprocess.run(
            [sys.executable, "scripts/run_evals.py"],
            capture_output=True,
            text=True,
            cwd=_PROJECT_ROOT,
        )
        output = json.loads(result.stdout)
        # Should not raise
        datetime.fromisoformat(output["timestamp"])
