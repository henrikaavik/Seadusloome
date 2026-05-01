"""Regression: check_org_cost_budget must time out on a stuck advisory lock.

Before the #658 fix, a pre-deploy connection holding
``pg_advisory_xact_lock('cost_budget:<org>')`` could block every new
chat turn for that org indefinitely because the orchestrator's user-msg
persist transaction reused that lock.
"""

from __future__ import annotations

import os
import threading
import time
import uuid

import pytest

from app.chat.rate_limiter import check_org_cost_budget


@pytest.mark.integration
def test_check_org_cost_budget_returns_within_5s_when_lock_is_held():
    """If another connection holds the cost-budget advisory lock,
    ``check_org_cost_budget`` must return (fail-open) within 5 seconds
    instead of hanging forever."""
    if not os.getenv("DATABASE_URL"):
        pytest.skip("integration test — DATABASE_URL not set")

    import psycopg

    org_id = uuid.uuid4()
    holder_started = threading.Event()
    release_holder = threading.Event()

    def hold_lock() -> None:
        with psycopg.connect(os.environ["DATABASE_URL"]) as holder:
            holder.execute(
                "SELECT pg_advisory_xact_lock(hashtextextended('cost_budget:' || %s::text, 0))",
                (str(org_id),),
            )
            holder_started.set()
            while not release_holder.wait(timeout=0.1):
                pass
            holder.rollback()  # release the advisory lock

    thread = threading.Thread(target=hold_lock, daemon=True)
    thread.start()
    assert holder_started.wait(timeout=5.0), "lock-holder thread did not start"

    started = time.monotonic()
    with psycopg.connect(os.environ["DATABASE_URL"]) as conn:
        check_org_cost_budget(org_id, conn=conn)
        conn.rollback()
    elapsed = time.monotonic() - started

    release_holder.set()
    thread.join(timeout=2.0)

    assert elapsed < 5.0, (
        f"check_org_cost_budget hung for {elapsed:.1f}s with a stuck advisory "
        f"lock (must time out and fail-open in <5s)"
    )
