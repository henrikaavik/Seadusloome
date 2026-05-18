"""Wave 2 Step 1 diagnostic spike — probe the prod Estonian Legal Ontology shape.

Standalone script (no FastHTML imports) that runs four SPARQL queries against
a Jena Fuseki SPARQL endpoint to lock down the data shape that the
``app/docs/reference_resolver.py`` rewrite (Wave 2 Step 2 of the
``docs/2026-05-18-bugfix-plan.md`` execution plan) will rely on.

Usage:

    # Run all four queries against a local endpoint (the default).
    python scripts/probe_ontology_shape.py

    # Run just one query.
    python scripts/probe_ontology_shape.py --query A

    # Point at a different endpoint or dataset.
    python scripts/probe_ontology_shape.py \
        --jena-url http://localhost:13030 --dataset ontology --query all

To run against prod, SSH-tunnel Fuseki to a local port first, e.g.:

    ssh -L 13030:localhost:3030 root@89.116.22.4 \
        "docker port wznupyix6h3opupyu1v4uuod-132640737525 3030 && \
         tail -f /dev/null"

Then ``python scripts/probe_ontology_shape.py --jena-url http://localhost:13030``.
Alternatively, dispatch each query directly via ``ssh root@89.116.22.4
"docker exec ... curl ..."`` (as the bug-fix plan documents) and paste the
results — the script's ``--jena-url`` is just a convenience for repeat runs.

Output: per query — the SPARQL text, the raw JSON response, a one-line
human summary. Final block (``=== Resolver-rewrite implications ===``)
summarises the three decisions the rewrite needs.
"""

from __future__ import annotations

import argparse
import json
import sys
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

PREFIXES = """\
PREFIX estleg: <https://data.riik.ee/ontology/estleg#>
PREFIX rdfs: <http://www.w3.org/2000/01/rdf-schema#>
PREFIX xsd: <http://www.w3.org/2001/XMLSchema#>
"""

# ---------------------------------------------------------------------------
# Query catalogue
# ---------------------------------------------------------------------------
#
# The original spike text in docs/2026-05-18-bugfix-plan.md (around line 213)
# proposed Query A as:
#
#     SELECT (DATATYPE(?o) AS ?dt) (SAMPLE(?o) AS ?ex) (COUNT(*) AS ?n)
#     WHERE { ?s estleg:sourceAct ?o } GROUP BY (DATATYPE(?o))
#
# Fuseki rejects that with "Non-group key variable in SELECT: ?o in
# expression (datatype ?o)" because ``DATATYPE(?o)`` is recomputed in the
# SELECT projection over a grouping key derived from the same. The
# semantically-equivalent shape below — bind the datatype and a sample
# variable first, then group by the datatype — is what actually executes
# against Jena 5.x.

QUERY_A = (
    PREFIXES
    + """
# A. sourceAct datatype histogram — literal title vs object URI?
SELECT ?dt (SAMPLE(?ex_o) AS ?ex) (COUNT(*) AS ?n)
WHERE {
  ?s estleg:sourceAct ?o .
  BIND(DATATYPE(?o) AS ?dt)
  BIND(?o AS ?ex_o)
}
GROUP BY ?dt
"""
)

QUERY_B = (
    PREFIXES
    + """
# B. Canonical paragrahv literal form (does it carry the trailing period?).
SELECT ?paragrahv (COUNT(*) AS ?n)
WHERE { ?s estleg:paragrahv ?paragrahv }
GROUP BY ?paragrahv
ORDER BY DESC(?n)
LIMIT 20
"""
)

QUERY_C = (
    PREFIXES
    + """
# C. Parent-act predicate coverage — count partOf, partOfAct, and any
#    other estleg predicate whose local name contains "part" (defence
#    against unexpected predicate names).
SELECT ?p (COUNT(*) AS ?n) (SAMPLE(?o) AS ?ex)
WHERE {
  ?s ?p ?o .
  FILTER(
    ?p = estleg:partOf ||
    ?p = estleg:partOfAct ||
    CONTAINS(LCASE(STR(?p)), "part")
  )
}
GROUP BY ?p
ORDER BY DESC(?n)
"""
)

QUERY_D = (
    PREFIXES
    + """
# D. Provision -> act join paths (sample 10 rows to eyeball).
SELECT ?prov ?actLit ?partOf ?partOfAct
WHERE {
  ?prov estleg:paragrahv ?par .
  OPTIONAL { ?prov estleg:sourceAct  ?actLit }
  OPTIONAL { ?prov estleg:partOf     ?partOf }
  OPTIONAL { ?prov estleg:partOfAct  ?partOfAct }
}
LIMIT 10
"""
)

QUERIES: dict[str, str] = {
    "A": QUERY_A,
    "B": QUERY_B,
    "C": QUERY_C,
    "D": QUERY_D,
}


# ---------------------------------------------------------------------------
# HTTP transport
# ---------------------------------------------------------------------------


def run_sparql(endpoint: str, query: str, timeout: float = 60.0) -> dict[str, Any]:
    """POST a SPARQL query and return the parsed JSON results.

    Uses stdlib only (urllib) to avoid pulling in httpx/requests; this
    script must run from a bare ``python`` interpreter without the app's
    virtualenv if needed.
    """
    body = urllib.parse.urlencode({"query": query}).encode("utf-8")
    request = urllib.request.Request(  # noqa: S310 — endpoint is operator-supplied
        endpoint,
        data=body,
        headers={
            "Accept": "application/sparql-results+json",
            "Content-Type": "application/x-www-form-urlencoded",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310
            payload = response.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"SPARQL HTTP {exc.code} from {endpoint}: {detail[:500]}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"SPARQL transport error against {endpoint}: {exc}") from exc
    try:
        return json.loads(payload)
    except json.JSONDecodeError as exc:
        raise RuntimeError(
            f"SPARQL response was not JSON (first 200 chars): {payload[:200]}"
        ) from exc


def _binding_value(row: dict[str, Any], var: str) -> str | None:
    cell = row.get(var)
    if not cell:
        return None
    value = cell.get("value")
    return value if isinstance(value, str) else None


def _binding_datatype(row: dict[str, Any], var: str) -> str | None:
    cell = row.get(var)
    if not cell:
        return None
    return cell.get("datatype")


# ---------------------------------------------------------------------------
# Per-query summaries (one line each)
# ---------------------------------------------------------------------------


def summarise_a(result: dict[str, Any]) -> str:
    rows = result.get("results", {}).get("bindings", [])
    if not rows:
        return "Query A: ZERO estleg:sourceAct triples in the dataset."
    parts: list[str] = []
    for row in rows:
        dt = _binding_value(row, "dt") or "(no datatype = URI / plain literal)"
        ex = _binding_value(row, "ex") or "<no example>"
        n = _binding_value(row, "n") or "?"
        parts.append(f"{n} rows datatype={dt} sample={ex!r}")
    return "Query A: " + "; ".join(parts)


def summarise_b(result: dict[str, Any]) -> str:
    rows = result.get("results", {}).get("bindings", [])
    if not rows:
        return "Query B: ZERO estleg:paragrahv triples."
    samples = [_binding_value(row, "paragrahv") or "" for row in rows]
    with_dot = sum(1 for s in samples if s.endswith("."))
    without_dot = len(samples) - with_dot
    top = samples[0] if samples else ""
    return (
        f"Query B: top-20 paragrahv literals — most-common={top!r} "
        f"({with_dot} with trailing period, {without_dot} without)"
    )


def summarise_c(result: dict[str, Any]) -> str:
    rows = result.get("results", {}).get("bindings", [])
    if not rows:
        return (
            "Query C: ZERO triples for estleg:partOf, estleg:partOfAct, "
            "or any predicate whose local name contains 'part'."
        )
    parts: list[str] = []
    for row in rows:
        p = _binding_value(row, "p") or "?"
        n = _binding_value(row, "n") or "?"
        parts.append(f"{p}={n}")
    return "Query C: " + "; ".join(parts)


def summarise_d(result: dict[str, Any]) -> str:
    rows = result.get("results", {}).get("bindings", [])
    if not rows:
        return "Query D: no provisions found (unexpected)."
    have_act_lit = sum(1 for r in rows if "actLit" in r)
    have_part_of = sum(1 for r in rows if "partOf" in r)
    have_part_of_act = sum(1 for r in rows if "partOfAct" in r)
    return (
        f"Query D: {len(rows)} sample provisions — actLit={have_act_lit} "
        f"partOf={have_part_of} partOfAct={have_part_of_act}"
    )


SUMMARISERS = {
    "A": summarise_a,
    "B": summarise_b,
    "C": summarise_c,
    "D": summarise_d,
}


# ---------------------------------------------------------------------------
# Resolver-rewrite implications
# ---------------------------------------------------------------------------


def _source_act_is_literal(result_a: dict[str, Any]) -> bool | None:
    """Return ``True`` if sourceAct is literal-typed, ``False`` if URI-typed.

    ``None`` is returned only when Query A returned zero rows (unexpected).
    """
    rows = result_a.get("results", {}).get("bindings", [])
    if not rows:
        return None
    # If any row has a datatype URI (e.g. xsd:string) the value is literal.
    # If a row's ``dt`` cell is absent, the bound ``?o`` was either a URI
    # or a literal without a datatype (e.g. a plain literal or langString).
    # We treat any present datatype as a literal signal.
    for row in rows:
        dt = _binding_value(row, "dt")
        if dt:
            return True
    # No datatype anywhere = all sourceAct objects are URIs.
    return False


def _part_predicate_status(result_c: dict[str, Any]) -> tuple[bool, bool, list[str]]:
    """Return (has_partOf, has_partOfAct, extra_part_predicates)."""
    has_part_of = False
    has_part_of_act = False
    extras: list[str] = []
    for row in result_c.get("results", {}).get("bindings", []):
        predicate = _binding_value(row, "p") or ""
        if predicate.endswith("#partOf"):
            has_part_of = True
        elif predicate.endswith("#partOfAct"):
            has_part_of_act = True
        else:
            extras.append(predicate)
    return has_part_of, has_part_of_act, extras


def _paragrahv_form(result_b: dict[str, Any]) -> str:
    rows = result_b.get("results", {}).get("bindings", [])
    if not rows:
        return "UNKNOWN — Query B returned no rows"
    samples = [_binding_value(row, "paragrahv") or "" for row in rows]
    with_dot = sum(1 for s in samples if s.endswith("."))
    total = len(samples)
    if with_dot == total:
        return f'ALWAYS "§ N." (trailing period; top-{total} sample 100% with period)'
    if with_dot == 0:
        return f'ALWAYS "§ N" (no trailing period; top-{total} sample 100% without)'
    return (
        f"MIXED — top-{total} sample: {with_dot} with period, "
        f"{total - with_dot} without (resolver must test BOTH forms)"
    )


def emit_implications(results: dict[str, dict[str, Any]]) -> None:
    print()
    print("=== Resolver-rewrite implications ===")

    # sourceAct shape.
    if "A" in results:
        is_lit = _source_act_is_literal(results["A"])
        if is_lit is None:
            print(
                "- sourceAct shape: UNKNOWN — Query A returned no rows. "
                "Investigate before the rewrite (the resolver currently "
                "assumes sourceAct is a URI reachable from a Law node)."
            )
        elif is_lit:
            print(
                "- sourceAct shape: LITERAL (string, e.g. xsd:string). "
                "Act-half resolution must match the literal title directly "
                '(VALUES ?actLit { "<title>" }) — there is no act URI to '
                "follow from a provision's sourceAct edge."
            )
        else:
            print(
                "- sourceAct shape: URI (objects are IRIs). "
                "Act-half resolution follows the URI and joins to rdfs:label "
                "or estleg:shortName on the Law node."
            )

    # partOf / partOfAct shape.
    if "C" in results:
        has_po, has_poa, extras = _part_predicate_status(results["C"])
        if has_po and has_poa:
            print(
                "- partOf / partOfAct: BOTH present. The section-match "
                "query needs a UNION over both predicates."
            )
        elif has_po:
            print(
                "- partOf / partOfAct: ONLY estleg:partOf is present. "
                "Section-match query needs ONLY the partOf branch — "
                "skip partOfAct (empty UNION arm)."
            )
        elif has_poa:
            print(
                "- partOf / partOfAct: ONLY estleg:partOfAct is present. "
                "Section-match query needs ONLY the partOfAct branch."
            )
        else:
            print(
                "- partOf / partOfAct: NEITHER is present in prod. "
                "Section-match query must NOT include either UNION arm. "
                "The provision -> act link in this corpus is the LITERAL "
                "edge estleg:sourceAct (see Query A) — there is no URI "
                "join between a provision and its parent law."
            )
        if extras:
            print(
                "- Other 'part*' predicates observed (investigate before "
                "the rewrite): " + ", ".join(extras)
            )

    # paragrahv canonical form.
    if "B" in results:
        print(f"- paragrahv canonical form: {_paragrahv_form(results['B'])}")

    print()


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Probe the Estonian Legal Ontology in a Jena Fuseki dataset "
            "to lock down the data shape for the Wave 2 resolver rewrite."
        ),
    )
    parser.add_argument(
        "--jena-url",
        default="http://localhost:3030",
        help=(
            "Base URL of the Fuseki server (default: http://localhost:3030). "
            "For prod, SSH-tunnel first and point this at the local port."
        ),
    )
    parser.add_argument(
        "--dataset",
        default="ontology",
        help='Fuseki dataset name (default: "ontology").',
    )
    parser.add_argument(
        "--query",
        default="all",
        choices=["A", "B", "C", "D", "all"],
        help='Which query to run. "all" runs A, B, C, D in order.',
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=60.0,
        help="HTTP timeout per query, in seconds (default: 60).",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    endpoint = f"{args.jena_url.rstrip('/')}/{args.dataset}/sparql"
    selected: list[str]
    if args.query == "all":
        selected = ["A", "B", "C", "D"]
    else:
        selected = [args.query]

    print(f"# Probing endpoint: {endpoint}")
    print(f"# Queries:          {', '.join(selected)}")
    print()

    results: dict[str, dict[str, Any]] = {}
    for key in selected:
        query = QUERIES[key]
        print("-" * 72)
        print(f"### Query {key}")
        print(query.strip())
        print()
        try:
            result = run_sparql(endpoint, query, timeout=args.timeout)
        except RuntimeError as exc:
            print(f"!! Query {key} failed: {exc}")
            print()
            continue
        results[key] = result
        print("Raw JSON:")
        print(json.dumps(result, indent=2, ensure_ascii=False))
        print()
        print("Summary: " + SUMMARISERS[key](result))
        print()

    if args.query == "all":
        emit_implications(results)
    return 0


if __name__ == "__main__":
    sys.exit(main())
