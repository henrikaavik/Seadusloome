"""Temporal-scope policy for Analüüsikeskus SPARQL aggregates (C5, #850).

The single place that codifies *which redactions of the law an
Analüüsikeskus engine counts*. Before this module the burden /
sanctions / competency / court-practice aggregates walked every
provision regardless of whether the owning act had been repealed — so a
"how many obligations does this act impose" count happily mixed live law
with provisions of acts that ceased to exist years ago. In a
legal-advisory tool that silently overstates the answer.

Why a *positive-knowledge exclusion* and not a version-chain filter
--------------------------------------------------------------------

The obvious design — "keep only the current ``ProvisionVersion``" — is a
trap in this corpus. The 2026-05-15 ontology audit (recorded in
:mod:`app.ontology.relations`, lines 140-160) confirmed:

* ``ProvisionVersion`` / ``versionValidFrom`` / ``versionValidTo`` /
  ``supersededByVersion`` / ``versionText`` exist in the SHACL shapes but
  the *populated* data is **sample-only** — deferred to ontology issue
  ``henrikaavik/estonian-legal-ontology#208`` (V2). A version-chain
  filter would match almost nothing in production.
* ``temporalStatus`` (values ``"in_force"`` / ``"in_force_partial"`` /
  ``"repealed"`` / ``"pending"``), ``repealDate``, ``entryIntoForce`` and
  ``lastAmendmentDate`` **are** populated corpus-wide on ``Act`` nodes —
  this is the data ``app.analyysikeskus.history`` already reads.

A naive ``FILTER NOT EXISTS { … repealed … }`` over the *version-chain*
predicates would therefore be a silent no-op: it passes everything in
production (the predicates aren't populated) while looking green on
synthetic fixtures that do carry them. To stay honest when the data is
sparse, the default filter is built **only on the positively-populated
predicates** (:data:`POSITIVE_REPEAL_PREDICATES`) and uses
*positive-knowledge exclusion*:

    Exclude a provision **only when it is positively marked repealed /
    superseded**. Absent data ⇒ included.

That is the correct default for an advisory tool over an incomplete
graph: we never *hide* a provision we merely lack temporal data for, and
the filter automatically gets stricter as the ontology back-fills repeal
markers — no code change required. The same property makes the filter
testable: :func:`current_law_filter` references *only* the audited
predicates, so a regression test can assert the emitted clause never
drifts onto an unpopulated predicate (which would re-introduce the
no-op).

Where the temporal markers live
-------------------------------

In production a provision links to its act via ``estleg:sourceAct``,
whose object is a **literal act title** (24,221 triples, all
``xsd:string``; ``estleg:partOf`` / ``estleg:partOfAct`` carry zero rows
— see ``docs/2026-05-18-bugfix-plan.md`` Wave 2). The repeal markers
(``temporalStatus`` / ``repealDate``) hang off a separate ``Act`` *node*
whose ``rdfs:label`` equals that title. The canonical TTL fixture instead
points ``sourceAct`` straight at an ``Act`` URI. :func:`current_law_filter`
handles **both** shapes, and additionally checks the provision *itself*
for a direct marker, so the same clause is correct on prod and on
fixtures.

Neighbour policy (the rest of the Analüüsikeskus)
-------------------------------------------------

* :mod:`app.analyysikeskus.history` is **intentionally historical** — its
  whole job is to surface repealed acts and amendment timelines, so it
  deliberately does **not** apply the current-law default. It owns its
  own temporal reads.
* :mod:`app.analyysikeskus.similarity` and
  :mod:`app.analyysikeskus.eu_transposition` do not apply a temporal
  scope today; when they adopt this helper they must declare their scope
  explicitly (pass :data:`TemporalScope.ALL` if they want history, or
  :data:`TemporalScope.CURRENT` to inherit the exclusion). They are out
  of scope for #850.

Usage
-----

The engine query-builder layers splice the clause into a
provision-bound SELECT::

    from app.ontology.temporal_scope import TemporalScope, temporal_scope_clause

    sparql = (
        PREFIXES
        + f'''
    SELECT ?provision ...
    WHERE {{
      ?provision estleg:sourceAct ?actLit .
      ...
    {temporal_scope_clause(scope, "provision")}
    }}
    '''
    )

The clause is pure SPARQL 1.1 (``FILTER NOT EXISTS`` + ``rdfs:label``
joins + equality), so it behaves identically on Apache Jena/ARQ and on
the rdflib engine the regression tests run against.
"""

from __future__ import annotations

import re
from enum import StrEnum
from typing import Final

from app.ontology.relations import PREDICATES

# ---------------------------------------------------------------------------
# Scope model
# ---------------------------------------------------------------------------


class TemporalScope(StrEnum):
    """Which redactions of the law an aggregate counts.

    The value strings are the canonical request-parameter tokens carried
    by the ``?oigus=`` query parameter (see
    :class:`app.analyysikeskus.routes._Scope`), so a route can round-trip
    a scope through a URL with :func:`scope_from_param` and ``scope.value``.

    Members:
        CURRENT: *Kehtiv õigus* — the default. Exclude provisions that
            are **positively** marked repealed / superseded (their owning
            act, or the provision itself, carries a populated repeal
            marker). Provisions with no temporal data are **included** —
            see the module docstring on positive-knowledge exclusion.
        ALL: *Kogu ajalugu* — no temporal filtering; repealed and current
            provisions both count. The explicit historical/all scope the
            DoD requires.
    """

    CURRENT = "current"
    ALL = "all"


#: The default scope when a request carries no (or an unrecognised)
#: ``?oigus=`` token. Current law is the honest default for an advisory
#: tool — a lawyer asking "how many obligations does this act impose"
#: means *today's* law unless they opt into history.
DEFAULT_SCOPE: Final[TemporalScope] = TemporalScope.CURRENT


def scope_from_param(value: str | None) -> TemporalScope:
    """Fold a raw ``?oigus=`` token into a :class:`TemporalScope`.

    Accepts the canonical member values (``"current"`` / ``"all"``) plus a
    couple of legacy / UI aliases the scope form has historically emitted
    (``"current_plus_history"`` and ``"kogu_ajalugu"`` both mean "include
    history"). Anything unrecognised — including ``None`` and the empty
    string — folds to :data:`DEFAULT_SCOPE` so a malformed URL degrades to
    the safe current-law default rather than erroring.
    """
    token = (value or "").strip().lower()
    if not token:
        return DEFAULT_SCOPE
    if token == TemporalScope.ALL.value:
        return TemporalScope.ALL
    # Legacy / UI aliases meaning "include history".
    if token in {"kogu_ajalugu", "ajalugu", "current_plus_history", "all_history"}:
        return TemporalScope.ALL
    if token == TemporalScope.CURRENT.value:
        return TemporalScope.CURRENT
    return DEFAULT_SCOPE


# ---------------------------------------------------------------------------
# Positively-populated repeal predicates (the audit result)
# ---------------------------------------------------------------------------
#
# These are the ONLY predicates the current-law filter is allowed to
# reference. They were confirmed populated corpus-wide by the 2026-05-15
# ontology audit and are already read by app.analyysikeskus.history:
#
#   * estleg:temporalStatus — literal; the value "repealed" positively
#     marks a repealed act/provision. ("in_force" / "in_force_partial" /
#     "pending" are NOT treated as repeal markers — only the explicit
#     "repealed" string excludes.)
#   * estleg:repealDate     — a populated repeal-date literal is itself a
#     positive repeal marker (an act with a repealDate has been repealed).
#
# Deliberately EXCLUDED (sample-only / would no-op — see module docstring):
#   versionValidFrom / versionValidTo / supersededByVersion / versionText
#
# A regression test asserts the emitted FILTER references only these two
# predicate URIs, guarding against silent drift back onto an unpopulated
# predicate.

#: The literal value of ``estleg:temporalStatus`` that positively marks a
#: repealed act / provision. Mirrors the ``"repealed"`` key in
#: :data:`app.analyysikeskus.history.TEMPORAL_STATUS_LABELS_ET`.
REPEALED_STATUS_VALUE: Final[str] = "repealed"

#: The closed set of predicate URIs the current-law exclusion is built
#: on. Exposed so a regression test can assert no other predicate sneaks
#: into the emitted clause (the silent-no-op guard).
POSITIVE_REPEAL_PREDICATES: Final[frozenset[str]] = frozenset(
    {
        PREDICATES.TEMPORAL_STATUS,
        PREDICATES.REPEAL_DATE,
    }
)


# ---------------------------------------------------------------------------
# Clause builders
# ---------------------------------------------------------------------------


def _clean_var(var: str, fallback: str) -> str:
    """Strip a SPARQL variable name to ``[A-Za-z0-9_]`` (defensive)."""
    return re.sub(r"[^A-Za-z0-9_]", "", var or "") or fallback


def current_law_filter(provision_var: str = "provision") -> str:
    """Return the *current-law* ``FILTER NOT EXISTS`` block for a provision.

    The block excludes ``?<provision_var>`` **only when it is positively
    marked repealed** — either directly on the provision, or on its owning
    act. "Positively marked" means one of the audited
    :data:`POSITIVE_REPEAL_PREDICATES` is populated with a repeal value:

    * ``estleg:temporalStatus "repealed"`` — explicit repealed status, or
    * ``estleg:repealDate <any>`` — a populated repeal date.

    Crucially this is *exclusion by positive knowledge*: a provision with
    no temporal data at all passes the filter (``FILTER NOT EXISTS`` over
    an unmatched pattern is true), so the incomplete production corpus is
    never silently hollowed out. See the module docstring.

    Both ``sourceAct`` shapes are handled in one clause:

    * **Prod** — ``?provision estleg:sourceAct "<title>"`` (a literal); we
      match an ``Act`` node whose ``rdfs:label`` equals that title and
      check its markers.
    * **Fixture** — ``?provision estleg:sourceAct estleg:Act_x`` (a URI);
      a second arm checks that URI node directly for a marker.

    The returned string is a complete ``FILTER NOT EXISTS { … }`` block
    (indented two spaces, no trailing newline) ready to splice straight
    into a ``WHERE`` clause after the patterns that bind
    ``?<provision_var>``.

    Args:
        provision_var: The SPARQL variable (without ``?``) carrying the
            ``LegalProvision`` IRI to test. Non-word characters are
            stripped defensively.
    """
    p = _clean_var(provision_var, "provision")
    status_uri = PREDICATES.TEMPORAL_STATUS
    repeal_uri = PREDICATES.REPEAL_DATE
    repealed = REPEALED_STATUS_VALUE
    # Internal helper variables are name-spaced with the provision var so
    # splicing two of these blocks (different provision vars) into one
    # query never collides.
    act = f"_tsAct_{p}"
    # The literal-title hop (arms b) joins the provision's ``sourceAct``
    # literal to an ``Act`` node's ``rdfs:label``. We deliberately bind
    # the two sides to *distinct* variables and compare them with
    # ``STR()`` on both sides rather than re-using one variable: a shared
    # variable forces RDF-term equality, which includes the language tag,
    # so the moment labels become ``"…"@et`` (while ``sourceAct`` stays a
    # plain literal — today's JSON-LD shape) the join would silently stop
    # matching and the literal-title hop would degrade to a no-op. The
    # ``STR()`` coercion compares lexical forms only, so it survives a
    # later move to language-tagged labels. (Review follow-up, #850.)
    sa = f"_tsSourceAct_{p}"
    lbl = f"_tsLabel_{p}"
    return f"""  FILTER NOT EXISTS {{
    {{
      # (a) repeal marker directly on the provision itself
      ?{p} <{status_uri}> "{repealed}" .
    }} UNION {{
      ?{p} <{repeal_uri}> ?_tsProvRepeal_{p} .
    }} UNION {{
      # (b) repeal marker on the owning act — prod shape: sourceAct is a
      #     literal title; match the Act node whose rdfs:label equals it
      #     (STR()-coerced so a future "@et" label tag can't break it).
      ?{p} estleg:sourceAct ?{sa} .
      ?{act} rdfs:label ?{lbl} .
      ?{act} <{status_uri}> "{repealed}" .
      FILTER(STR(?{lbl}) = STR(?{sa}))
    }} UNION {{
      ?{p} estleg:sourceAct ?{sa}b .
      ?{act}b rdfs:label ?{lbl}b .
      ?{act}b <{repeal_uri}> ?_tsActRepeal_{p} .
      FILTER(STR(?{lbl}b) = STR(?{sa}b))
    }} UNION {{
      # (c) repeal marker on the owning act — fixture shape: sourceAct is
      #     an Act URI; check that URI node directly.
      ?{p} estleg:sourceAct ?{act}u .
      ?{act}u <{status_uri}> "{repealed}" .
    }} UNION {{
      ?{p} estleg:sourceAct ?{act}u2 .
      ?{act}u2 <{repeal_uri}> ?_tsActRepealU_{p} .
    }}
  }}"""


def temporal_scope_clause(
    scope: TemporalScope,
    provision_var: str = "provision",
) -> str:
    """Return the SPARQL scope clause for *scope* (the engine entry point).

    This is what the engine query-builders call. It is the *only* place
    engines need to reason about temporal scope — pass the resolved
    :class:`TemporalScope` and the provision variable, splice the result
    into the ``WHERE`` clause, and the default-current policy is inherited
    automatically:

    * :attr:`TemporalScope.CURRENT` → :func:`current_law_filter` (the
      positive-knowledge exclusion).
    * :attr:`TemporalScope.ALL` → an empty string (no filtering; repealed
      provisions are kept).

    Args:
        scope: The resolved temporal scope. A non-:class:`TemporalScope`
            value (e.g. a raw string that slipped through) is folded via
            :func:`scope_from_param` so callers can't accidentally disable
            the filter by passing ``"current"`` as a bare string.
        provision_var: The provision SPARQL variable (without ``?``).
    """
    resolved = scope if isinstance(scope, TemporalScope) else scope_from_param(str(scope))
    if resolved is TemporalScope.ALL:
        return ""
    return current_law_filter(provision_var)


__all__ = [
    "TemporalScope",
    "DEFAULT_SCOPE",
    "REPEALED_STATUS_VALUE",
    "POSITIVE_REPEAL_PREDICATES",
    "scope_from_param",
    "current_law_filter",
    "temporal_scope_clause",
]
