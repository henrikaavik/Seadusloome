"""Pin the Analüüsikeskus route table across the #860 routes/ split.

Before #860, ``app/analyysikeskus/routes.py`` was a single 6.9k-line module
whose :func:`register_analyysikeskus_routes` registered twelve routes. #860
split it into a ``routes/`` package (one submodule per workflow, mirroring
``app/docs/routes/``). The external contract — the exact set of
``(methods, path) → handler`` registrations and their order — MUST stay
byte-for-byte identical.

This test captures the registration against a recording stub of the
FastHTML route decorator and asserts it equals the table pinned below as a
literal. The literal was captured from ``origin/main``'s monolith *before*
the split (see the PR description for the capture snippet); any future
addition/removal/reorder of a route fails this test on purpose so the change
is deliberate.
"""

from __future__ import annotations

from app.analyysikeskus import register_analyysikeskus_routes

# The frozen route table — captured from the pre-#860 monolith's
# register_analyysikeskus_routes(rt) against a recording stub. Each entry is
# (sorted-methods-tuple, path, handler-__name__). Order matters: it mirrors
# the registration order in the package __init__'s register function.
_EXPECTED_ROUTES: list[tuple[tuple[str, ...], str, str]] = [
    (("GET",), "/analyysikeskus", "analyysikeskus_page"),
    (("GET",), "/analyysikeskus/normi-mojuahel", "normi_mojuahel_page"),
    (("GET",), "/analyysikeskus/el-ulevott", "el_ulevott_page"),
    (("GET",), "/analyysikeskus/sanktsioonid", "sanktsioonid_page"),
    (("GET",), "/analyysikeskus/kohtupraktika", "kohtupraktika_page"),
    (("GET",), "/analyysikeskus/halduskoormus", "halduskoormus_page"),
    (("GET",), "/analyysikeskus/padevused", "padevused_page"),
    (("GET",), "/analyysikeskus/ajalugu", "ajalugu_page"),
    (("GET",), "/analyysikeskus/sarnasus", "sarnasus_page"),
    (("GET",), "/analyysikeskus/moju-poliitikamottest", "moju_poliitikamottest_page"),
    (("POST",), "/analyysikeskus/moju-poliitikamottest/extract", "moju_poliitikamottest_extract"),
    (("POST",), "/analyysikeskus/moju-poliitikamottest/analyze", "moju_poliitikamottest_analyze"),
]


class _RecordingRt:
    """A stand-in for FastHTML's ``rt`` decorator that records registrations."""

    def __init__(self) -> None:
        self.calls: list[tuple[tuple[str, ...], str, str]] = []

    def __call__(self, path: str, methods: list[str] | None = None, **_kwargs):  # type: ignore[no-untyped-def]
        def decorator(fn):  # type: ignore[no-untyped-def]
            method_tuple = tuple(sorted(methods or ["GET"]))
            self.calls.append((method_tuple, path, getattr(fn, "__name__", "?")))
            return fn

        return decorator


def _capture() -> list[tuple[tuple[str, ...], str, str]]:
    rt = _RecordingRt()
    register_analyysikeskus_routes(rt)
    return rt.calls


def test_route_table_matches_pinned_literal() -> None:
    """The split package registers exactly the pre-#860 route table, in order."""
    assert _capture() == _EXPECTED_ROUTES


def test_route_count_is_twelve() -> None:
    """A guard so a dropped registration is caught even if the literal drifts."""
    assert len(_capture()) == 12


def test_no_duplicate_paths_per_method() -> None:
    """No ``(method, path)`` pair is registered twice (would shadow a handler)."""
    seen: set[tuple[tuple[str, ...], str]] = set()
    for methods, path, _fn in _capture():
        key = (methods, path)
        assert key not in seen, f"duplicate registration for {key}"
        seen.add(key)


def test_every_handler_is_unique() -> None:
    """Each route maps to a distinct handler function name."""
    handlers = [fn for _m, _p, fn in _capture()]
    assert len(handlers) == len(set(handlers)), "a handler is registered for >1 route"
