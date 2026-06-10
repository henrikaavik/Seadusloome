"""Tenant-scoping policy for Jena/SPARQL read paths (#844).

The single Jena dataset holds two very different kinds of data in one
place:

* the **public** enacted ontology (~1M triples) in the *default* graph,
  plus the read-only EU corpus; and
* **org-private draft named graphs** at
  ``https://data.riik.ee/ontology/estleg/drafts/<uuid>[/v<n>]`` (and the
  ephemeral ``…/estleg/adhoc/<uuid>`` probe graphs the Analüüsikeskus
  "Normi mõjuahel" workflow mints + deletes per request).

Several read paths historically ran SPARQL that could *reach into* those
named graphs — directly (the chat ``query_ontology`` tool let the LLM
write ``GRAPH ?g {…}``) or implicitly (queries that walk the default
graph would also see named-graph triples the moment Fuseki's
``unionDefaultGraph`` flag flips on). Both leak pre-publication draft
text, titles, and cross-org conflict metadata.

This module is the **one place** that codifies the policy so future read
paths don't reinvent ad-hoc scoping. Two complementary tools:

1. :func:`assert_public_only` — a validator for *LLM-authored* SPARQL.
   It rejects any query that names a graph (``GRAPH`` / ``FROM`` /
   ``FROM NAMED``) so the query can only touch the default graph. On
   TDB2/Fuseki a default-graph query excludes named graphs **unless**
   ``unionDefaultGraph`` is set — which it is not in
   ``docker/fuseki-config/ontology.ttl`` (issue #853 owns that file).

2. :func:`public_subject_filter` — a SPARQL ``FILTER`` block that drops
   any row whose subject sits in the draft / adhoc namespace. Splicing
   it into a *server-authored* template pins the result to public data
   **even if** ``unionDefaultGraph`` is on (the named-graph triples then
   appear in the default graph, but their subjects are draft/adhoc URIs,
   so the filter removes them). This is engine-portable: it relies only
   on SPARQL ``STRSTARTS`` semantics, not on any ARQ/TDB2 default-graph
   magic IRI (a ``FROM <urn:x-arq:DefaultGraph>`` clause is *not* used —
   it is ARQ-specific and the rdflib SPARQL engine the tests run against
   rejects it). Verified against a simulated-union dataset (DoD: "does
   not depend on Fuseki ``unionDefaultGraph`` behavior").

3. :func:`graph_uri_belongs_to_drafts`, :func:`draft_id_from_entity_uri`
   and :func:`is_adhoc_graph_uri` — helpers the conflict-detection +
   render-time masking paths use to recognise and bucket draft / adhoc
   graph URIs without each caller re-deriving the URI shape.
"""

from __future__ import annotations

import re

# ---------------------------------------------------------------------------
# Draft / adhoc named-graph URI shapes
# ---------------------------------------------------------------------------
#
# These mirror the URIs the app generates server-side:
#
#   draft v1 :  https://data.riik.ee/ontology/estleg/drafts/<uuid>
#   draft vN :  https://data.riik.ee/ontology/estleg/drafts/<uuid>/v<n>
#   adhoc    :  https://data.riik.ee/ontology/estleg/adhoc/<uuid>
#
# NB: the *strict* allowlist used at the Graph Store Protocol transport
# (``app.sync.jena_loader._SAFE_GRAPH_URI``) only admits the v1 ``<uuid>``
# shape today; the per-version ``/v<n>`` widening is tracked by #849.
# The patterns here are deliberately *recognisers* (used for masking /
# exclusion decisions), not transport allowlists — they must already
# recognise the ``/v<n>`` form so the self-conflict exclusion (A5) keeps
# working the moment #849 lands.

#: Namespace prefix shared by every draft named graph.
DRAFT_GRAPH_PREFIX = "https://data.riik.ee/ontology/estleg/drafts/"

#: Namespace prefix shared by every ephemeral adhoc probe graph.
ADHOC_GRAPH_PREFIX = "https://data.riik.ee/ontology/estleg/adhoc/"

# A draft UUID is the canonical 36-char lowercase hyphenated form.
_UUID_RE = r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"

# Recognises a draft graph URI (v1 or v<n>) and captures the draft UUID.
_DRAFT_GRAPH_RE = re.compile(
    r"^https://data\.riik\.ee/ontology/estleg/drafts/(" + _UUID_RE + r")(?:/v\d+)?(?:#.*)?$"
)

# Recognises an adhoc probe graph URI (and the ``#self`` subject inside).
_ADHOC_GRAPH_RE = re.compile(
    r"^https://data\.riik\.ee/ontology/estleg/adhoc/" + _UUID_RE + r"(?:#.*)?$"
)


# ---------------------------------------------------------------------------
# Public-only SPARQL validation (for LLM-authored queries)
# ---------------------------------------------------------------------------
#
# We strip single-line comments first so an attacker cannot smuggle a
# ``GRAPH`` keyword past the check behind ``# GRAPH …`` misdirection
# (mirrors the comment-stripping ``app.chat.tools`` already does before
# its read-only check). Only ``#`` at start-of-line or after whitespace
# counts as a comment — a ``#`` inside a ``<…#fragment>`` URI is left
# alone.
_COMMENT_RE = re.compile(r"(?:^|(?<=\s))#[^\n]*", re.MULTILINE)

# Block any graph-naming keyword. ``GRAPH``, ``FROM`` and ``FROM NAMED``
# all let a query escape the default graph and reach a draft/adhoc named
# graph; rejecting them confines the query to public default-graph data.
# (``FROM NAMED`` is caught by the ``FROM`` arm — the leading ``FROM`` is
# what matters.)
_GRAPH_KEYWORD_RE = re.compile(r"\b(GRAPH|FROM)\b", re.IGNORECASE)


def _strip_comments(query: str) -> str:
    return _COMMENT_RE.sub("", query)


def references_named_graph(query: str) -> bool:
    """Return ``True`` if *query* names a graph (``GRAPH`` / ``FROM``).

    Comments are stripped first so the check cannot be bypassed by
    hiding the keyword behind ``# …``.
    """
    return _GRAPH_KEYWORD_RE.search(_strip_comments(query)) is not None


def assert_public_only(query: str) -> None:
    """Raise :class:`ValueError` if *query* could touch a non-default graph.

    The contract for LLM-authored SPARQL: it may only read the public
    default graph. Any ``GRAPH`` / ``FROM`` / ``FROM NAMED`` keyword is a
    hard reject — that is the only way a query can reach an org-private
    draft named graph (or the ephemeral adhoc graphs), so forbidding the
    keyword confines every query to public data.

    Raises:
        ValueError: when the (comment-stripped) query references a named
            graph. Callers should surface this as a tool-level error,
            never silently run the query.
    """
    if references_named_graph(query):
        raise ValueError(
            "SPARQL may not reference named graphs (GRAPH / FROM / FROM NAMED); "
            "only the public default graph is queryable here."
        )


# ---------------------------------------------------------------------------
# Draft / adhoc graph-URI recognisers (for conflict masking)
# ---------------------------------------------------------------------------


def is_draft_graph_uri(uri: str) -> bool:
    """Return ``True`` if *uri* is a draft named-graph URI (any version)."""
    return bool(uri) and _DRAFT_GRAPH_RE.match(uri) is not None


def is_adhoc_graph_uri(uri: str) -> bool:
    """Return ``True`` if *uri* is an ephemeral adhoc probe-graph URI.

    Matches both the bare graph URI and the ``…#self`` subject IRI minted
    inside it by :mod:`app.analyysikeskus.adhoc_analysis`. Adhoc probes
    are typed ``estleg:DraftLegislation`` so they would otherwise surface
    as phantom conflicts in a concurrent draft's report.
    """
    return bool(uri) and _ADHOC_GRAPH_RE.match(uri) is not None


def draft_id_from_uri(uri: str) -> str | None:
    """Return the draft UUID embedded in a draft graph/subject *uri*.

    Works for both the graph URI (``…/drafts/<uuid>`` and
    ``…/drafts/<uuid>/v<n>``) and the ``…#self`` subject IRI inside it
    (which the conflict query projects as ``?conflictEntity``). Returns
    ``None`` for any URI that is not a draft URI (e.g. a public ontology
    entity, an adhoc probe, or a plain ``urn:`` value).
    """
    if not uri:
        return None
    m = _DRAFT_GRAPH_RE.match(uri)
    if m is None:
        return None
    return m.group(1)


# Backwards-friendly aliases (read nicely at call sites).
draft_id_from_entity_uri = draft_id_from_uri
graph_uri_belongs_to_drafts = is_draft_graph_uri


# ---------------------------------------------------------------------------
# Explicit public-data scoping for server-authored templates
# ---------------------------------------------------------------------------


def public_subject_filter(var: str = "entity") -> str:
    """Return a SPARQL ``FILTER`` block excluding draft/adhoc subjects.

    Splice this into a server-authored SELECT (after the triple patterns
    binding ``?<var>``) to scope the result to public ontology data
    explicitly. Two ``STRSTARTS`` guards drop any row whose subject IRI
    lives under the draft (``…/estleg/drafts/``) or adhoc
    (``…/estleg/adhoc/``) namespace.

    Why this and not ``FROM <urn:x-arq:DefaultGraph>``: the ARQ magic IRI
    only the Jena/Fuseki engine understands, and the rdflib SPARQL engine
    the test suite runs against rejects it (it tries to dereference the
    ``urn:`` as an external graph). The ``STRSTARTS`` approach is pure
    SPARQL 1.1 and behaves identically whether the draft triples live in
    a named graph (normal config) or have bled into the default graph
    (``unionDefaultGraph=true``) — in both cases the subject IRI is a
    draft/adhoc URI, so the filter removes it. This satisfies the DoD's
    "does not depend on Fuseki ``unionDefaultGraph`` behavior" without a
    config dependency.

    Args:
        var: The SPARQL variable name (without ``?``) carrying the
            subject IRI to test. Non-word characters are stripped as a
            defensive measure so the value can never break out of the
            FILTER context.
    """
    clean = re.sub(r"[^A-Za-z0-9_]", "", var) or "entity"
    return (
        f'  FILTER(!STRSTARTS(STR(?{clean}), "{DRAFT_GRAPH_PREFIX}"))\n'
        f'  FILTER(!STRSTARTS(STR(?{clean}), "{ADHOC_GRAPH_PREFIX}"))'
    )


def draft_graph_prefix_for(graph_uri: str) -> str:
    """Return the ``…/drafts/<uuid>`` prefix shared by a draft's graphs.

    Given any version's graph URI (``…/drafts/<uuid>`` or
    ``…/drafts/<uuid>/v<n>``), return the version-agnostic
    ``…/drafts/<uuid>`` prefix. The conflict query uses this with
    ``!STRSTARTS(str(?otherGraph), <prefix>)`` so a draft never
    self-conflicts against its own prior-version graphs (A5).

    For a URI that is not a draft graph URI the input is returned
    unchanged — the caller then degrades to an exact-match exclusion,
    which is still correct (just less aggressive).
    """
    did = draft_id_from_uri(graph_uri)
    if did is None:
        return graph_uri
    return f"{DRAFT_GRAPH_PREFIX}{did}"


__all__ = [
    "DRAFT_GRAPH_PREFIX",
    "ADHOC_GRAPH_PREFIX",
    "assert_public_only",
    "references_named_graph",
    "public_subject_filter",
    "is_draft_graph_uri",
    "is_adhoc_graph_uri",
    "draft_id_from_uri",
    "draft_id_from_entity_uri",
    "graph_uri_belongs_to_drafts",
    "draft_graph_prefix_for",
]
