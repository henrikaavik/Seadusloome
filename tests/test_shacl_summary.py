"""Tests for app/sync/shacl_summary.py."""

# ruff: noqa: E501

from __future__ import annotations

from app.sync.shacl_summary import parse_report, summarise_report

_REAL_REPORT_SNIPPET = """Validation Report
Conforms: False
Results (22503):
Constraint Violation in MinCountConstraintComponent (http://www.w3.org/ns/shacl#MinCountConstraintComponent):
	Severity: sh:Violation
	Source Shape: [ sh:datatype xsd:string ; sh:minCount Literal("1", datatype=xsd:integer) ; sh:name Literal("summary") ; sh:path estleg:summary ]
	Focus Node: <https://data.riik.ee/ontology/estleg#ASeS_Par_19>
	Result Path: estleg:summary
	Message: Less than 1 values on <https://data.riik.ee/ontology/estleg#ASeS_Par_19>->estleg:summary
Constraint Violation in MinCountConstraintComponent (http://www.w3.org/ns/shacl#MinCountConstraintComponent):
	Severity: sh:Violation
	Source Shape: [ sh:path estleg:summary ; sh:minCount 1 ]
	Focus Node: <https://data.riik.ee/ontology/estleg#PS_Par_8>
	Result Path: estleg:summary
	Message: Less than 1 values on <https://data.riik.ee/ontology/estleg#PS_Par_8>->estleg:summary
Constraint Violation in MinCountConstraintComponent (http://www.w3.org/ns/shacl#MinCountConstraintComponent):
	Severity: sh:Violation
	Source Shape: [ sh:path estleg:sourceAct ; sh:minCount 1 ]
	Focus Node: <https://data.riik.ee/ontology/estleg#KarS_Par_1>
	Result Path: estleg:sourceAct
	Message: Less than 1 values
"""


class TestParseReport:
    def test_groups_by_component_and_path(self):
        groups = parse_report(_REAL_REPORT_SNIPPET)
        # Two groups: summary × 2, sourceAct × 1
        assert len(groups) == 2
        # Most-common first
        assert groups[0].count == 2
        assert groups[0].path == "estleg:summary"
        assert groups[1].count == 1
        assert groups[1].path == "estleg:sourceAct"

    def test_collects_sample_focus_nodes_shortened(self):
        groups = parse_report(_REAL_REPORT_SNIPPET)
        summary_group = groups[0]
        # Focus nodes shortened to the fragment after '#'
        assert "ASeS_Par_19" in summary_group.samples
        assert "PS_Par_8" in summary_group.samples

    def test_empty_report_returns_empty_list(self):
        assert parse_report("") == []

    def test_malformed_block_does_not_crash(self):
        malformed = "Results (1):\nConstraint Violation in Xyzzy (no body)"
        groups = parse_report(malformed)
        assert len(groups) == 1
        assert groups[0].count == 1


class TestSummariseReport:
    def test_emits_header_with_total_count(self):
        s = summarise_report(_REAL_REPORT_SNIPPET)
        # Localised header in Estonian
        assert s.startswith("22,503 SHACL hoiatust:")

    def test_includes_per_group_counts(self):
        s = summarise_report(_REAL_REPORT_SNIPPET)
        assert "2\u00d7" in s
        assert "1\u00d7" in s

    def test_includes_estonian_action_verb(self):
        s = summarise_report(_REAL_REPORT_SNIPPET)
        # MinCountConstraintComponent → "missing"
        assert "missing estleg:summary" in s
        assert "missing estleg:sourceAct" in s

    def test_includes_sample_focus_nodes(self):
        s = summarise_report(_REAL_REPORT_SNIPPET)
        assert "ASeS_Par_19" in s
        assert "PS_Par_8" in s

    def test_empty_report_returns_explainer(self):
        assert summarise_report("") == "SHACL report empty"

    def test_truncates_very_long_summaries(self):
        # Build a synthetic report with many distinct groups.
        blocks = ["Results (10000):"]
        for i in range(200):
            blocks.append(
                f"Constraint Violation in DatatypeConstraintComponent (...):\n"
                f"\tSource Shape: [ sh:path estleg:prop_{i} ]\n"
                f"\tFocus Node: <https://data.riik.ee/ontology/estleg#N_{i}>\n"
                f"\tResult Path: estleg:prop_{i}\n"
                f"\tMessage: bad datatype\n"
            )
        report = "\n".join(blocks)
        s = summarise_report(report)
        # Capped below the output-table-friendly limit
        assert len(s) <= 1000
        # Truncation marker present
        assert "\u2026" in s or "veel" in s
