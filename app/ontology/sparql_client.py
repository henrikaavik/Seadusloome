"""SPARQL client for querying Apache Jena Fuseki."""

from __future__ import annotations

import logging
import os
import re

import httpx

logger = logging.getLogger(__name__)

# Default connection settings (from jena_loader.py conventions)
_DEFAULT_JENA_URL = "http://localhost:3030"
_DEFAULT_JENA_DATASET = "ontology"


def _sanitize_sparql_value(value: str) -> str:
    """Escape special characters for safe inclusion in SPARQL string literals.

    This escapes backslashes, quotes, and newlines so the value is safe
    inside a SPARQL double-quoted string literal.
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

    def _execute(self, sparql: str) -> dict:  # type: ignore[type-arg]
        """Send a SPARQL query and return the raw JSON response dict."""
        try:
            response = httpx.post(
                self.endpoint,
                data={"query": sparql},
                headers={"Accept": "application/sparql-results+json"},
                timeout=self.timeout,
            )
            response.raise_for_status()
            return response.json()  # type: ignore[no-any-return]
        except httpx.ConnectError:
            logger.exception("Cannot connect to Jena Fuseki at %s", self.endpoint)
            return {"results": {"bindings": []}}
        except httpx.TimeoutException:
            logger.exception("SPARQL query timed out (%ss)", self.timeout)
            return {"results": {"bindings": []}}
        except httpx.HTTPStatusError:
            logger.exception("SPARQL query returned HTTP error")
            return {"results": {"bindings": []}}
        except httpx.HTTPError:
            logger.exception("SPARQL query failed")
            return {"results": {"bindings": []}}

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
        """
        if bindings:
            sparql = self._inject_bindings(sparql, bindings)
        if uri_bindings:
            sparql = self._inject_uri_bindings(sparql, uri_bindings)
        raw = self._execute(sparql)
        return _extract_bindings(raw)

    def ask(self, sparql: str) -> bool:
        """Execute a SPARQL ASK query and return the boolean result."""
        try:
            response = httpx.post(
                self.endpoint,
                data={"query": sparql},
                headers={"Accept": "application/sparql-results+json"},
                timeout=self.timeout,
            )
            response.raise_for_status()
            data = response.json()
            return bool(data.get("boolean", False))
        except httpx.HTTPError:
            logger.exception("SPARQL ASK query failed")
            return False

    def count(self, sparql: str) -> int:
        """Execute a SPARQL SELECT query that returns a single count value.

        Expects the query to project a ``?count`` variable.
        """
        rows = self.query(sparql)
        if rows and "count" in rows[0]:
            try:
                return int(rows[0]["count"])
            except (ValueError, TypeError):
                return 0
        return 0
