"""SPARQL client for querying Apache Jena Fuseki."""

from __future__ import annotations

import logging
import os
import re
import threading
import time

import httpx

from app.metrics import record_metric

logger = logging.getLogger(__name__)

# Default connection settings (from jena_loader.py conventions)
_DEFAULT_JENA_URL = "http://localhost:3030"
_DEFAULT_JENA_DATASET = "ontology"


# ---------------------------------------------------------------------------
# Shared httpx.Client (connection pooling)
# ---------------------------------------------------------------------------
#
# Every ``SparqlClient`` instance (there are ~30 short-lived ones across the
# app — one per analyysikeskus helper call, etc.) routes its HTTP through a
# single process-wide ``httpx.Client``.  A bare ``httpx.post`` opens and tears
# down a fresh TCP+TLS connection on every call; a pooled client keeps
# keep-alive connections to Fuseki warm, which matters under the 5–50
# concurrent-user load and the multi-query Analüüsikeskus pages.
#
# Lazy-init singleton (mirrors the ``ClaudeProvider`` / ``VoyageProvider`` /
# resolver pattern documented in CLAUDE.md): the client is constructed on the
# first real request behind a thread-safe lock, so importing this module never
# opens sockets and tests that never hit the network never pay for one.

# Bound the pool so a burst of concurrent SPARQL calls can't exhaust file
# descriptors against a single Fuseki host.  ``max_connections`` is the hard
# ceiling; ``max_keepalive_connections`` is how many idle connections we keep
# warm between requests.
_HTTP_LIMITS = httpx.Limits(max_connections=20, max_keepalive_connections=10)

# Overall ceiling for a single request when a caller does not pass its own
# per-instance timeout.  Individual ``SparqlClient`` instances override this
# per-request via ``self.timeout`` (see :meth:`SparqlClient._execute`).
_DEFAULT_TIMEOUT = 30.0

_shared_client: httpx.Client | None = None
_shared_client_lock = threading.Lock()


def _get_shared_http_client() -> httpx.Client:
    """Return the process-wide pooled :class:`httpx.Client`, building it once.

    Double-checked locking so the first concurrent burst of SPARQL calls
    constructs exactly one client.  The client carries connection-pool
    limits and a default timeout; per-request timeouts still win when a
    caller passes one to :meth:`httpx.Client.post`.
    """
    global _shared_client
    if _shared_client is not None:
        return _shared_client
    with _shared_client_lock:
        if _shared_client is None:
            _shared_client = httpx.Client(
                limits=_HTTP_LIMITS,
                timeout=httpx.Timeout(_DEFAULT_TIMEOUT),
            )
    return _shared_client


def close_shared_http_client() -> None:
    """Close and drop the shared client (process shutdown / test teardown).

    Safe to call when no client was ever built — it is a no-op in that
    case.  The next call to :func:`_get_shared_http_client` rebuilds a
    fresh pool.
    """
    global _shared_client
    with _shared_client_lock:
        client, _shared_client = _shared_client, None
    if client is not None:
        client.close()


def _sanitize_sparql_value(value: str) -> str:
    """Escape special characters for safe inclusion in SPARQL string literals.

    This escapes backslashes, quotes, and newlines so the value is safe
    inside a SPARQL double-quoted string literal.

    .. warning::

        This function must **NOT** be used for URI contexts (``<...>`` or
        prefixed names like ``estleg:...``).  Characters such as ``<``,
        ``>``, ``{``, ``}``, and spaces are not escaped here and can break
        out of URI delimiters, enabling SPARQL injection.  For URI bindings
        use :meth:`SparqlClient._inject_uri_bindings` which validates
        against a strict character allowlist.
    """
    value = value.replace("\\", "\\\\")
    value = value.replace('"', '\\"')
    value = value.replace("\n", "\\n")
    value = value.replace("\r", "\\r")
    return value


def _extract_bindings(raw: dict) -> list[dict[str, str]]:  # type: ignore[type-arg]
    """Extract clean dicts from SPARQL JSON results.

    SPARQL JSON results have the shape::

        {"results": {"bindings": [{"var": {"type": "...", "value": "..."}, ...}, ...]}}

    This function returns a list of ``{"var": "value", ...}`` dicts.
    """
    bindings = raw.get("results", {}).get("bindings", [])
    rows: list[dict[str, str]] = []
    for binding in bindings:
        row: dict[str, str] = {}
        for var, info in binding.items():
            row[var] = info.get("value", "")
        rows.append(row)
    return rows


class SparqlClient:
    """Client for executing SPARQL queries against Jena Fuseki.

    Parameters
    ----------
    jena_url:
        Base URL for the Fuseki server.  Defaults to ``JENA_URL`` env var.
    dataset:
        Dataset name.  Defaults to ``JENA_DATASET`` env var.
    timeout:
        Request timeout in seconds.
    """

    def __init__(
        self,
        jena_url: str | None = None,
        dataset: str | None = None,
        timeout: float = 30.0,
    ) -> None:
        self.jena_url = jena_url or os.environ.get("JENA_URL", _DEFAULT_JENA_URL)
        self.dataset = dataset or os.environ.get("JENA_DATASET", _DEFAULT_JENA_DATASET)
        self.timeout = timeout

    @property
    def endpoint(self) -> str:
        """SPARQL query endpoint URL."""
        return f"{self.jena_url}/{self.dataset}/sparql"

    def _execute(
        self,
        sparql: str,
        *,
        on_error: str = "swallow",
        operation: str = "query",
    ) -> dict:  # type: ignore[type-arg]
        """Send a SPARQL query and return the raw JSON response dict.

        Parameters
        ----------
        sparql:
            The SPARQL query string.
        on_error:
            How to handle httpx-level failures (connection refused,
            timeout, HTTP 5xx).  Two modes:

            * ``"swallow"`` (default, legacy behaviour) — log and return
              an empty ``{"results": {"bindings": []}}`` dict so callers
              get an empty result list and can keep working.  This is
              the right default for read-only display paths where a
              dead Jena should degrade gracefully.

            * ``"raise"`` — re-raise the underlying :class:`httpx.HTTPError`
              so the caller can distinguish "Jena returned 0 rows" from
              "Jena was unreachable / timed out".  Used by code that
              caches the result and must avoid poisoning the cache on
              transient outages (e.g. the resolver's abbreviation-map
              warm-up — see ``app/docs/reference_resolver.py``).
        operation:
            Label recorded on the ``sparql_query_ms`` metric to attribute
            the call to ``"query"``, ``"ask"``, or ``"count"``.  Kept
            deliberately low-cardinality — the SPARQL text and URI
            parameters are never recorded.
        """
        start = time.perf_counter()
        status = "error"
        try:
            response = _get_shared_http_client().post(
                self.endpoint,
                data={"query": sparql},
                headers={"Accept": "application/sparql-results+json"},
                timeout=self.timeout,
            )
            response.raise_for_status()
            result: dict = response.json()  # type: ignore[type-arg]
            status = "ok"
            return result
        except httpx.ConnectError:
            logger.exception("Cannot connect to Jena Fuseki at %s", self.endpoint)
            if on_error == "raise":
                raise
            return {"results": {"bindings": []}}
        except httpx.TimeoutException:
            logger.exception("SPARQL query timed out (%ss)", self.timeout)
            if on_error == "raise":
                raise
            return {"results": {"bindings": []}}
        except httpx.HTTPStatusError:
            logger.exception("SPARQL query returned HTTP error")
            if on_error == "raise":
                raise
            return {"results": {"bindings": []}}
        except httpx.HTTPError:
            logger.exception("SPARQL query failed")
            if on_error == "raise":
                raise
            return {"results": {"bindings": []}}
        finally:
            duration_ms = (time.perf_counter() - start) * 1000
            record_metric(
                "sparql_query_ms",
                round(duration_ms, 2),
                {"operation": operation, "status": status},
            )

    def _inject_bindings(self, sparql: str, bindings: dict[str, str]) -> str:
        """Inject variable bindings using a SPARQL VALUES clause.

        Adds a ``VALUES`` block at the end of the WHERE clause (before the
        closing ``}``).  Values are properly escaped for SPARQL string
        literals.  This is safer than string interpolation or f-strings.
        """
        if not bindings:
            return sparql

        values_parts: list[str] = []
        for var, val in bindings.items():
            clean_var = re.sub(r"[^a-zA-Z0-9_]", "", var)
            escaped = _sanitize_sparql_value(val)
            values_parts.append(f'  VALUES ?{clean_var} {{ "{escaped}" }}')

        values_block = "\n".join(values_parts)

        # Insert VALUES clause(s) before the last closing brace of the query.
        # We find the last '}' and insert the block before it.
        last_brace = sparql.rfind("}")
        if last_brace == -1:
            return sparql
        return sparql[:last_brace] + "\n" + values_block + "\n" + sparql[last_brace:]

    def _inject_uri_bindings(self, sparql: str, bindings: dict[str, str]) -> str:
        """Inject URI variable bindings using a SPARQL VALUES clause.

        Like :meth:`_inject_bindings` but wraps values in ``<...>`` for
        URI terms instead of ``"..."`` for string literals.  Each URI is
        validated against a strict allowlist before injection.

        Raises ``ValueError`` if any URI contains characters that could
        break out of the ``<...>`` context (angle brackets, braces, etc.).
        """
        if not bindings:
            return sparql

        safe_uri_re = re.compile(r"^https?://[A-Za-z0-9./:_#\-]{1,512}$")
        values_parts: list[str] = []
        for var, val in bindings.items():
            clean_var = re.sub(r"[^a-zA-Z0-9_]", "", var)
            if not safe_uri_re.fullmatch(val):
                raise ValueError(f"Unsafe URI rejected for SPARQL binding: {val!r}")
            values_parts.append(f"  VALUES ?{clean_var} {{ <{val}> }}")

        values_block = "\n".join(values_parts)

        last_brace = sparql.rfind("}")
        if last_brace == -1:
            return sparql
        return sparql[:last_brace] + "\n" + values_block + "\n" + sparql[last_brace:]

    def query(
        self,
        sparql: str,
        bindings: dict[str, str] | None = None,
        uri_bindings: dict[str, str] | None = None,
        *,
        on_error: str = "swallow",
        operation: str = "query",
    ) -> list[dict[str, str]]:
        """Execute a SPARQL SELECT query and return a list of result dicts.

        Each dict maps variable names to their string values.

        Parameters
        ----------
        sparql:
            The SPARQL SELECT query string.
        bindings:
            Optional string variable bindings injected via VALUES clause.
        uri_bindings:
            Optional URI variable bindings injected via VALUES clause with
            ``<uri>`` syntax.  Validates each URI against a strict pattern.
        on_error:
            How to handle httpx-level failures (connection refused,
            timeout, HTTP 5xx). ``"swallow"`` (default) returns an empty
            list and logs; ``"raise"`` re-raises the underlying
            :class:`httpx.HTTPError`. Callers that cache results and need
            to distinguish "Jena returned 0 rows" from "Jena was down"
            should pass ``on_error="raise"`` so a transient failure
            does not permanently poison the cache. See
            :func:`_execute` for the full semantics.
        operation:
            Forwarded to :meth:`_execute` for metric labelling.  Defaults
            to ``"query"``; :meth:`count` overrides it to ``"count"``.
        """
        if bindings:
            sparql = self._inject_bindings(sparql, bindings)
        if uri_bindings:
            sparql = self._inject_uri_bindings(sparql, uri_bindings)
        raw = self._execute(sparql, on_error=on_error, operation=operation)
        return _extract_bindings(raw)

    def ask(self, sparql: str) -> bool:
        """Execute a SPARQL ASK query and return the boolean result.

        Routed through :meth:`_execute` so the call is counted in the
        ``sparql_query_ms`` metric with ``operation="ask"``.  Network
        failures are swallowed (logged + ``False``) to preserve the
        legacy contract.
        """
        data = self._execute(sparql, on_error="swallow", operation="ask")
        return bool(data.get("boolean", False))

    def count(self, sparql: str, *, on_error: str = "swallow") -> int:
        """Execute a SPARQL SELECT query that returns a single count value.

        Expects the query to project a ``?count`` variable.

        Parameters
        ----------
        on_error:
            Forwarded to :meth:`query` / :meth:`_execute`.  ``"swallow"``
            (the default, legacy behaviour) means a dead Jena yields
            ``0`` — convenient for display paths that can tolerate a
            zero count.  ``"raise"`` re-raises the underlying
            :class:`httpx.HTTPError` so a caller can tell "Jena says
            there are 0 rows" apart from "Jena was unreachable" and
            avoid rendering an outage as a truthful ``total: 0`` (which
            misleads pagination — see ``app/explorer/routes.py``).

        Note that a genuine empty result (Jena reachable, zero rows)
        still returns ``0`` under ``on_error="raise"`` — only transport
        failures propagate, never an empty binding set.
        """
        rows = self.query(sparql, operation="count", on_error=on_error)
        if rows and "count" in rows[0]:
            try:
                return int(rows[0]["count"])
            except (ValueError, TypeError):
                return 0
        return 0
