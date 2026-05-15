"""Smoke tests that run against live infrastructure when available.

Tests in this package are marked ``@pytest.mark.smoke`` and skip cleanly
when their required service (Jena, Postgres, etc.) is not reachable. They
exist to spot-check the system against real corpus data after a deploy
or a refactor that touches the data-access layer.

To run only the smoke tests::

    pytest -m smoke

To skip them in normal CI runs (the default), no extra config is needed
because they all skip themselves when the live service is missing.
"""
