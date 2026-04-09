"""Global pytest fixtures.

The only fixture we need right now disables the background job
worker thread for the entire test session. The worker is started by
``app.main``'s ASGI lifespan hook, which fires the moment a test
instantiates ``starlette.testclient.TestClient(app)``. Without this
guard every test run would spawn a real thread hitting a mocked DB,
causing flaky interactions with the ``unittest.mock.patch`` calls in
``tests/test_jobs_queue.py`` and friends.

Set ``DISABLE_BACKGROUND_WORKER=1`` *before* ``app.main`` is imported
anywhere — autouse session fixtures run after collection, which is
too late (the import at the top of ``tests/test_app.py`` has already
happened), so we also set the env var at module import time below.
"""

from __future__ import annotations

import os

# Set the flag as early as possible — conftest.py is loaded before any
# test module, so this runs before ``from app.main import app`` in any
# test file can trigger the lifespan hook.
os.environ.setdefault("DISABLE_BACKGROUND_WORKER", "1")
