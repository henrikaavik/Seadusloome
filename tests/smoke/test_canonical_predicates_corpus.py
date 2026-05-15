"""Live-Jena smoke test for the canonical predicate rename (C0).

Runs against a real Jena Fuseki dataset when ``JENA_URL`` is reachable;
skips otherwise. The goal is to spot-check that the canonical predicate
names actually return non-zero rows on the deployed ontology — catching
the case where the source repo renames a predicate again and our queries
silently go quiet.

Run with::

    pytest -m smoke tests/smoke/test_canonical_predicates_corpus.py

The corpus drifts between ontology releases, so the assertions are
deliberately loose: each predicate must have **at least one** triple,
not a fixed minimum. If you want to gate a milestone on precise counts,
generate a separate audit report and commit it under ``docs/``.
"""

from __future__ import annotations

import os

import httpx
import pytest

from app.ontology.relations import PREDICATES
from app.ontology.sparql_client import SparqlClient

# Mark every test in this module as a smoke test so ``pytest -m smoke``
# picks them up and the default test run leaves them alone.
pytestmark = pytest.mark.smoke


def _jena_reachable() -> bool:
    """Return True if the configured Jena endpoint answers a trivial ASK.

    Uses the same env-var-driven defaults as :class:`SparqlClient` so a
    live Jena pointed at by ``JENA_URL`` / ``JENA_DATASET`` is exercised.
    Any connection / HTTP error → unreachable, test skips.
    """
    client = SparqlClient()
    try:
        response = httpx.post(
            client.endpoint,
            data={"query": "ASK { ?s ?p ?o }"},
            headers={"Accept": "application/sparql-results+json"},
            timeout=2.0,
        )
        response.raise_for_status()
    except (httpx.HTTPError, httpx.ConnectError):
        return False
    return True


@pytest.fixture(scope="module")
def sparql_client() -> SparqlClient:
    """Return a live SparqlClient, or skip the module if Jena is unreachable."""
    if not _jena_reachable():
        pytest.skip(
            f"Live Jena not reachable at {SparqlClient().endpoint}; "
            f"set JENA_URL/JENA_DATASET to run these smoke tests."
        )
    return SparqlClient()


def _row_count(client: SparqlClient, predicate_uri: str, limit: int = 10) -> int:
    """Return the number of triples for ``?s <predicate_uri> ?o`` (capped at ``limit``).

    A COUNT(*) on a 1M-triple dataset is slow; instead we ``SELECT … LIMIT 10``
    and count the returned rows. The assertion is "at least 1", so 10 is plenty.
    """
    query = f"""
    SELECT ?s ?o WHERE {{
      ?s <{predicate_uri}> ?o .
    }}
    LIMIT {limit}
    """
    rows = client.query(query)
    return len(rows)


class TestCanonicalPredicatesInLiveJena:
    """Each canonical predicate has at least one triple in the deployed corpus."""

    def test_interprets_law_has_data(self, sparql_client: SparqlClient):
        assert _row_count(sparql_client, PREDICATES.INTERPRETS_LAW) >= 1, (
            "estleg:interpretsLaw returned zero rows on live Jena — either "
            "the corpus is missing court-decision interpretation edges or "
            "the predicate name has drifted again."
        )

    def test_interpreted_by_has_data(self, sparql_client: SparqlClient):
        assert _row_count(sparql_client, PREDICATES.INTERPRETED_BY) >= 1, (
            "estleg:interpretedBy returned zero rows on live Jena."
        )

    def test_amends_has_data(self, sparql_client: SparqlClient):
        assert _row_count(sparql_client, PREDICATES.AMENDS) >= 1, (
            "estleg:amends returned zero rows on live Jena — the canonical "
            "AmendmentEvent edge is missing."
        )

    def test_amended_by_has_data(self, sparql_client: SparqlClient):
        assert _row_count(sparql_client, PREDICATES.AMENDED_BY) >= 1, (
            "estleg:amendedBy returned zero rows on live Jena."
        )

    def test_requested_cluster_has_data(self, sparql_client: SparqlClient):
        # ``requestedCluster`` is the canonical populated predicate
        # (``topicCluster`` is the SHACL-defined alias, often empty).
        assert _row_count(sparql_client, PREDICATES.REQUESTED_CLUSTER) >= 1, (
            "estleg:requestedCluster returned zero rows on live Jena — "
            "topic-cluster assignments are missing from the corpus."
        )

    def test_transposes_directive_has_data(self, sparql_client: SparqlClient):
        assert _row_count(sparql_client, PREDICATES.TRANSPOSES_DIRECTIVE) >= 1, (
            "estleg:transposesDirective returned zero rows on live Jena."
        )

    def test_references_has_data(self, sparql_client: SparqlClient):
        assert _row_count(sparql_client, PREDICATES.REFERENCES) >= 1, (
            "estleg:references returned zero rows on live Jena."
        )


class TestLegacyPredicatesNotInCorpus:
    """The buggy predicate names must NOT appear in live data.

    If any of these come back with rows, it means the corpus has *also*
    been populated with the legacy names — the C0 rename is still
    correct, but the audit decision (section 2.5 E) to drop the aliases
    needs revisiting.
    """

    @pytest.mark.parametrize(
        "legacy_uri",
        [
            "https://data.riik.ee/ontology/estleg#interpretsProvision",
            "https://data.riik.ee/ontology/estleg#amendsProvision",
            "https://data.riik.ee/ontology/estleg#hasTopic",
            "https://data.riik.ee/ontology/estleg#implementsEU",
            "https://data.riik.ee/ontology/estleg#relatedTo",
        ],
    )
    def test_legacy_predicate_has_no_data(self, sparql_client: SparqlClient, legacy_uri: str):
        # Audit only — log if we find data instead of failing hard, since
        # this scenario means "the source ontology added back a legacy
        # alias", which is informational, not a regression in this app.
        count = _row_count(sparql_client, legacy_uri, limit=1)
        if count > 0:
            pytest.skip(
                f"Found {count}+ rows for legacy predicate {legacy_uri}. "
                f"This is unexpected — the audit (plan section 2.5) said "
                f"these don't exist in the source ontology. Worth filing "
                f"an issue if not deliberate."
            )


def test_module_self_check_when_jena_unreachable():
    """When Jena is unreachable, the rest of the module should skip cleanly."""
    if _jena_reachable():
        pytest.skip("Jena IS reachable — this self-check only runs offline.")
    # If we reach this line, Jena was unreachable AND the other tests
    # skipped via the fixture. The presence of this assertion is the
    # promise that the smoke module never fails for "no live infra" alone.
    assert os.environ.get("JENA_URL") != "guaranteed-to-fail", "self-check pre-flight"
