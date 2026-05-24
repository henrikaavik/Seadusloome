"""Tests for the standalone worker mode (#348).

Covers:
    - ``app.jobs.registry.register_all_handlers`` populates the same
      handler registry that the inproc worker uses.
    - Importing ``scripts.run_worker`` does NOT pull in FastHTML /
      Starlette / ``app.main`` (the whole point of running the worker
      in a separate, lighter container).
    - ``app.main.lifespan`` honours ``WORKER_MODE``: skips the worker
      thread when set to ``standalone``, starts it when set to
      ``inproc`` (and the legacy ``DISABLE_BACKGROUND_WORKER`` guard
      keeps tests from spawning anything real).
    - ``scripts.run_worker.main`` refuses to start unless
      ``WORKER_MODE=standalone``.
"""

from __future__ import annotations

import importlib
import os
import sys
from unittest.mock import MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# register_all_handlers
# ---------------------------------------------------------------------------


class TestRegisterAllHandlers:
    """The shared handler-wiring entry point must register every handler."""

    def test_register_all_handlers_populates_registry(self):
        """All known job_types appear in ``_HANDLERS`` after the call."""
        from app.jobs import registry
        from app.jobs.worker import _HANDLERS

        registry.register_all_handlers()

        # Phase 2 document pipeline handlers.
        assert "parse_draft" in _HANDLERS
        assert "extract_entities" in _HANDLERS
        assert "analyze_impact" in _HANDLERS
        assert "export_report" in _HANDLERS
        assert "draft_cleanup" in _HANDLERS

        # Phase 3 drafter handlers.
        assert "drafter_clarify" in _HANDLERS
        assert "drafter_research" in _HANDLERS
        assert "drafter_structure" in _HANDLERS
        assert "drafter_draft" in _HANDLERS
        assert "drafter_regenerate_clause" in _HANDLERS

    def test_register_all_handlers_is_idempotent(self):
        """Calling twice does not raise and leaves the same callables."""
        from app.jobs import registry
        from app.jobs.worker import _HANDLERS

        registry.register_all_handlers()
        first_snapshot = dict(_HANDLERS)

        registry.register_all_handlers()
        second_snapshot = dict(_HANDLERS)

        # Same job_types, same handler functions.
        assert set(first_snapshot.keys()) == set(second_snapshot.keys())
        for job_type in first_snapshot:
            assert first_snapshot[job_type] is second_snapshot[job_type]


# ---------------------------------------------------------------------------
# Standalone import does not drag in FastHTML
# ---------------------------------------------------------------------------


class TestStandaloneImportIsolation:
    """``scripts.run_worker`` MUST be importable without ``app.main``.

    The whole point of standalone mode is that the worker container can
    skip FastHTML / Starlette / uvicorn and stay lean. A regression
    here (e.g. someone adds ``from app.main import ...`` to the script)
    would defeat the purpose silently.
    """

    def test_run_worker_import_does_not_load_app_main(self):
        """Importing the entrypoint must not transitively import ``app.main``."""
        # Make sure we're testing a fresh import of run_worker.
        for mod in ("scripts.run_worker", "app.main"):
            sys.modules.pop(mod, None)

        # Import scripts.run_worker only — nothing else web-flavoured.
        importlib.import_module("scripts.run_worker")

        assert "app.main" not in sys.modules, (
            "scripts.run_worker must NOT transitively import app.main; "
            "found it in sys.modules after the import."
        )

    def test_run_worker_import_does_not_load_starlette_apps(self):
        """No Starlette ``Starlette`` app instance should get built."""
        for mod_name in ("scripts.run_worker", "app.main"):
            sys.modules.pop(mod_name, None)

        importlib.import_module("scripts.run_worker")

        # ``starlette.applications`` itself is a library module that
        # other parts of the project (e.g. ``fasthtml.common``) import.
        # The guard we actually care about is that the FastHTML *app
        # module* (which actually constructs the routes and middleware
        # stack) is not loaded as a side effect of importing the
        # standalone worker.
        assert "app.main" not in sys.modules

    def test_register_all_handlers_does_not_load_route_modules(self):
        """Standalone worker startup must NOT import any ``app.docs`` route module.

        The standalone worker calls
        :func:`app.jobs.registry.register_all_handlers` to populate the
        ``@register_handler`` dispatch table. That call imports
        ``app.docs`` (the package) so the handler modules' module-level
        decorator side effects fire — but it must NOT pull in the
        route-registration modules (``app.docs.routes`` /
        ``app.docs.report_routes`` / submodules of ``app.docs.routes``).
        Route modules drag in the entire FastHTML route surface; the
        whole point of standalone mode is to keep the worker container
        framework-free at startup.

        Regression guard for the #348 PR review finding: previously
        ``app/docs/__init__.py`` re-exported ``register_draft_routes`` /
        ``register_report_routes`` from their submodules, so any worker
        startup that imported ``app.docs`` transitively pulled in
        ``app.docs.routes`` (and everything below it) as a side effect.
        """
        # Force a clean import so the assertion reflects what a fresh
        # standalone-worker process would do. The route submodules may
        # have been imported by a sibling test that exercises web
        # routes, so we drop every plausible entry first.
        prefixes_to_drop = (
            "app.docs",
            "app.main",
            "app.jobs.registry",
            "scripts.run_worker",
        )
        for key in list(sys.modules.keys()):
            if any(key == p or key.startswith(p + ".") for p in prefixes_to_drop):
                del sys.modules[key]

        from app.jobs.registry import register_all_handlers

        register_all_handlers()

        # Route-registration modules must NOT be loaded.
        leaked_route_modules = sorted(
            mod
            for mod in sys.modules
            if mod == "app.docs.routes"
            or mod.startswith("app.docs.routes.")
            or mod == "app.docs.report_routes"
            or mod == "app.docs.websocket"
            or mod == "app.docs.ws_export_progress"
        )
        assert not leaked_route_modules, (
            "register_all_handlers() must not transitively import any "
            f"app.docs route module, but these leaked into sys.modules: "
            f"{leaked_route_modules}. The standalone worker container "
            "should stay framework-free at startup."
        )


# ---------------------------------------------------------------------------
# main.py lifespan honours WORKER_MODE
# ---------------------------------------------------------------------------


class TestLifespanRespectsWorkerMode:
    """The ASGI lifespan must gate worker startup on ``WORKER_MODE``."""

    def test_lifespan_skips_worker_when_standalone(self):
        """``WORKER_MODE=standalone`` must NOT spawn the worker thread.

        The archive-warning scheduler MUST still start in the web
        lifespan even when the worker runs standalone, because the web
        process is the canonical singleton host for the daily scan
        (``scripts/run_worker.py`` deliberately does not start it — see
        the "Why not also start the archive-warning scheduler here?"
        docstring there). Skipping the scheduler in standalone mode
        would silently drop the 90-day draft auto-archive compliance
        feature on split-process deployments.
        """
        import asyncio

        from app import main as app_main

        # Bypass the DISABLE_BACKGROUND_WORKER short-circuit so we
        # actually reach the WORKER_MODE branch.
        with (
            patch.dict(
                os.environ,
                {"DISABLE_BACKGROUND_WORKER": "0", "WORKER_MODE": "standalone"},
                clear=False,
            ),
            patch("app.jobs.worker.start_worker_thread") as mock_start_worker,
            patch(
                "app.jobs.archive_warning.start_archive_warning_scheduler"
            ) as mock_start_scheduler,
        ):
            mock_start_scheduler.return_value = MagicMock()

            async def _drive() -> None:
                gen = app_main.lifespan(MagicMock())
                # Run the startup half of the generator only.
                await gen.__anext__()
                # Close cleanly so the finally-block fires without raising.
                await gen.aclose()

            asyncio.run(_drive())

            # Worker thread must NOT start (standalone container owns it).
            mock_start_worker.assert_not_called()
            # Scheduler MUST start (web process is its canonical host).
            mock_start_scheduler.assert_called_once()

    def test_lifespan_starts_worker_when_inproc(self):
        """``WORKER_MODE=inproc`` (default) must spawn the worker thread."""
        import asyncio

        from app import main as app_main

        # Patch the start_* helpers so the test never spawns real
        # threads against a mocked DB. We also nullify the
        # DISABLE_BACKGROUND_WORKER flag set by conftest so the
        # lifespan reaches the WORKER_MODE branch.
        with (
            patch.dict(
                os.environ,
                {"DISABLE_BACKGROUND_WORKER": "0", "WORKER_MODE": "inproc"},
                clear=False,
            ),
            patch("app.jobs.worker.start_worker_thread") as mock_start_worker,
            patch(
                "app.jobs.archive_warning.start_archive_warning_scheduler"
            ) as mock_start_scheduler,
            patch("app.jobs.registry.register_all_handlers") as mock_register,
        ):
            # ``start_worker_thread`` normally returns a Thread; the
            # finally block joins it. A MagicMock with .join is enough.
            mock_thread = MagicMock()
            mock_start_worker.return_value = mock_thread
            mock_start_scheduler.return_value = mock_thread

            async def _drive() -> None:
                gen = app_main.lifespan(MagicMock())
                await gen.__anext__()
                await gen.aclose()

            asyncio.run(_drive())

            mock_register.assert_called_once()
            mock_start_worker.assert_called_once()
            mock_start_scheduler.assert_called_once()


# ---------------------------------------------------------------------------
# scripts.run_worker.main guard
# ---------------------------------------------------------------------------


class TestRunWorkerMainGuard:
    """``main()`` must refuse to run when ``WORKER_MODE != "standalone"``."""

    def test_main_exits_when_worker_mode_is_inproc(self):
        """Default inproc value should trigger a clear exit(1)."""
        # Drop any cached import so the lazy ``from app.config import ...``
        # inside main() picks up the patched env var.
        sys.modules.pop("scripts.run_worker", None)
        from scripts import run_worker

        with patch.dict(os.environ, {"WORKER_MODE": "inproc"}, clear=False):
            with pytest.raises(SystemExit) as excinfo:
                run_worker.main()

        # SystemExit code 1 — operator forgot to set WORKER_MODE=standalone.
        assert excinfo.value.code == 1

    def test_main_exits_on_invalid_worker_mode(self):
        """Unknown values surface via ``get_worker_mode``'s ValueError."""
        sys.modules.pop("scripts.run_worker", None)
        from scripts import run_worker

        with patch.dict(os.environ, {"WORKER_MODE": "bogus"}, clear=False):
            with pytest.raises(SystemExit) as excinfo:
                run_worker.main()

        assert excinfo.value.code == 1

    def test_main_proceeds_when_standalone(self):
        """``WORKER_MODE=standalone`` must pass the guard and reach the worker loop.

        We don't actually want to spin a real worker against a real DB
        in a unit test, so patch ``JobWorker.run_forever`` to a no-op
        and assert it was invoked. The signal-handler installation is
        a side effect we accept (the test process is short-lived).
        """
        sys.modules.pop("scripts.run_worker", None)
        from scripts import run_worker

        with (
            patch.dict(os.environ, {"WORKER_MODE": "standalone"}, clear=False),
            patch("app.jobs.worker.JobWorker.run_forever") as mock_run_forever,
            patch("app.jobs.registry.register_all_handlers") as mock_register,
        ):
            with pytest.raises(SystemExit) as excinfo:
                run_worker.main()

            assert excinfo.value.code == 0
            mock_register.assert_called_once()
            mock_run_forever.assert_called_once()


# ---------------------------------------------------------------------------
# get_worker_mode config helper
# ---------------------------------------------------------------------------


class TestGetWorkerMode:
    """``app.config.get_worker_mode`` is the single source of truth."""

    def test_defaults_to_inproc(self):
        """Unset env var means inproc (historical behaviour)."""
        from app.config import get_worker_mode

        # Use a context manager to ensure we restore the original env.
        original = os.environ.pop("WORKER_MODE", None)
        try:
            assert get_worker_mode() == "inproc"
        finally:
            if original is not None:
                os.environ["WORKER_MODE"] = original

    def test_accepts_standalone(self):
        from app.config import get_worker_mode

        with patch.dict(os.environ, {"WORKER_MODE": "standalone"}, clear=False):
            assert get_worker_mode() == "standalone"

    def test_normalises_case_and_whitespace(self):
        from app.config import get_worker_mode

        with patch.dict(os.environ, {"WORKER_MODE": "  Standalone  "}, clear=False):
            assert get_worker_mode() == "standalone"

    def test_raises_on_unknown_value(self):
        from app.config import get_worker_mode

        with patch.dict(os.environ, {"WORKER_MODE": "bogus"}, clear=False):
            with pytest.raises(ValueError, match="WORKER_MODE"):
                get_worker_mode()
