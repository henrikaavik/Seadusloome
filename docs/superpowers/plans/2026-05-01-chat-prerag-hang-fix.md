# Chat Pre-RAG Hang Fix Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Stop user messages from being silently lost when the production chat WebSocket hangs by (a) decoupling the user-message persist from the org-cost-budget advisory lock, (b) bounding every pre-stream DB call with a deadline that surfaces as a user-visible error, and (c) keeping the WS alive with a server-side heartbeat. Closes #658 (and largely resolves #655 as a side effect).

**Architecture:** Today, `ChatOrchestrator.handle_message` runs six unguarded sync `with get_connection() as conn:` blocks before the LLM stream loop, and the user-message INSERT lives inside a transaction that takes `pg_advisory_xact_lock('cost_budget:<org>')`. A pre-deploy connection still holding that lock blocks every new turn for the org — the persist never commits, the WS keeps "thinking" past 215s, and on reload the user message is gone. The 120s `asyncio.wait_for` only wraps `_run_stream_loop()` (line 902), so it does not protect the pre-stream phase. Fix: persist the user message in a tiny standalone transaction first (no lock), then run the budget check separately with `lock_timeout` and `pg_try_advisory_xact_lock` so a stuck lock fails fast and fail-open. Wrap each pre-stream sync DB call in `asyncio.to_thread(... )` + `asyncio.wait_for(timeout=...)` so a stalled pool surfaces as a friendly error event instead of a silent hang. Add a server-side WS heartbeat task (every 25s) for the connection lifetime so NAT idle timeouts and proxy idle-cuts can't silently kill the socket. Add step-level `logger.info` so future hangs are visible in logs.

**Tech Stack:** Python 3.13, FastHTML, asyncio, psycopg (sync), PostgreSQL 18, pytest. No new dependencies.

**Out of scope (explicit non-goals):**
- #660 (sync `_persist_partial_assistant` in cancel handlers) — separate follow-up PR; trivially small, decouple from this hot fix.
- #655 client-side "Peatamine…" UI feedback — the underlying cancellation issue is largely fixed by this PR (no more uninterruptible sync blocks); the UX polish for the Peata button is a follow-up.
- Replacing sync psycopg with async psycopg (`psycopg.AsyncConnection`) — large refactor, out of scope. We use `asyncio.to_thread` instead, which is the smallest correct change.
- #654 / #663 chat-list HTMX feedback — separate scope (different files, different bug).

---

## Background — What is actually wrong

Confirmed via two parallel investigations of `app/chat/orchestrator.py` and `app/chat/rate_limiter.py`:

| # | Step | File:line | Sync I/O? | Wrapped in wait_for? | Can hang on stuck pool? |
|---|---|---|---|---|---|
| 0 | rate-limit check | `orchestrator.py:540` (`check_message_rate`) → `rate_limiter.py:97-101` | yes (sync `with get_connection()`) | no | yes |
| 1 | load conversation | `orchestrator.py:550` | yes | no | yes |
| 2 | list messages history | `orchestrator.py:583` | yes | no | yes |
| 3a | budget check (advisory lock) | `orchestrator.py:612` → `rate_limiter.py:156-159` (`pg_advisory_xact_lock`) | yes | no | **YES — primary suspect** |
| 3b | persist user message | `orchestrator.py:613` (inside same tx as 3a) | yes | no | yes (blocked by 3a) |
| 3c | commit | `orchestrator.py:614` | yes | no | yes (blocked by 3a) |

After step 3c, RAG retrieval (line 650) and the LLM stream loop (line 902) ARE bounded by `asyncio.wait_for` — those are not the source of #658.

**Smoking gun:** `pg_advisory_xact_lock` at `rate_limiter.py:157` blocks indefinitely. The user-msg INSERT is inside the same transaction. A pre-deploy connection still holding the lock for that org makes EVERY new turn hang at the persist step; on reload the user sees an empty conversation because the INSERT never committed.

---

## File Structure

| File | Action | Reason |
|---|---|---|
| `app/chat/rate_limiter.py` | Modify | Add `lock_timeout` + `pg_try_advisory_xact_lock` fallback to `check_org_cost_budget` so a stuck lock fails fast |
| `app/chat/orchestrator.py` | Modify | Decouple persist from budget tx; wrap pre-stream DB calls in `asyncio.to_thread` + `asyncio.wait_for`; add step-level `logger.info`; add WS heartbeat |
| `app/chat/websocket.py` | Modify | Spawn heartbeat task at WS connect; cancel on disconnect |
| `tests/test_chat_orchestrator_prerag_hang.py` | Create | Regression test: mock retriever to hang; assert user msg is persisted before the hang and an error event is emitted. (Project tests live at `tests/test_chat_*.py` — flat layout, no `tests/chat/` subdir.) |
| `tests/test_chat_rate_limiter_lock_timeout.py` | Create | Unit test: budget check returns fail-open within bounded time when lock is held |
| `tests/test_chat_websocket_heartbeat.py` | Create | Smoke test: WS connection emits ping events at the configured interval |

---

## Task 1: Branch + baseline verification

**Files:** none (env setup only).

- [ ] **Step 1: Confirm we're on the right branch**

```bash
cd /Users/henrikaavik/progemoge/Seadusloome
git branch --show-current
```

Expected: `fix/658-chat-prerag-hang`. If not, run `git switch -c fix/658-chat-prerag-hang main`.

- [ ] **Step 2: Confirm baseline file state matches plan assumptions**

```bash
grep -n "check_org_cost_budget\|create_message\|pg_advisory_xact_lock" app/chat/orchestrator.py app/chat/rate_limiter.py | head -10
```

Expected output includes:
- `app/chat/orchestrator.py:612:                    check_org_cost_budget(org_id, conn=conn)`
- `app/chat/orchestrator.py:613:                create_message(conn, conversation_id, "user", user_message)`
- `app/chat/rate_limiter.py:157:                "SELECT pg_advisory_xact_lock(hashtextextended('cost_budget:' || %s::text, 0))",`

If lines have shifted (someone landed a fix in flight), pause and reconcile before continuing.

- [ ] **Step 3: Run baseline test pass**

```bash
uv run pytest tests/chat -x --tb=short 2>&1 | tail -10
```

Record the pass count. This is the floor we cannot regress below.

- [ ] **Step 4: Run baseline lint**

```bash
uv run ruff check app/chat
uv run pyright app/chat
```

Expected: clean. If anything is dirty before we touch code, address or note it.

No commit yet — proceed.

---

## Task 2: Add `lock_timeout` + non-blocking fallback to `check_org_cost_budget`

**Files:**
- Modify: `app/chat/rate_limiter.py:152-179`
- Test: `tests/test_chat_rate_limiter_lock_timeout.py` (create — project uses flat `tests/test_chat_*.py` layout)

**Why:** Today the advisory lock is unbounded — a stuck lock blocks the budget check forever. We add `SET LOCAL lock_timeout = '3s'` so a stuck lock raises `psycopg.errors.LockNotAvailable` after 3 seconds. The function already does fail-open on errors (line 176-179), so a timeout means the budget check is skipped (acceptable: the worst case is a momentary over-budget transaction; the next turn will still see the correct sum).

- [ ] **Step 1: Write the failing test**

Create `tests/test_chat_rate_limiter_lock_timeout.py`:

```python
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
            # Hold the lock until the test signals release. Sleep in
            # short chunks so we notice the release promptly.
            while not release_holder.wait(timeout=0.1):
                pass
            holder.rollback()  # release the advisory lock

    thread = threading.Thread(target=hold_lock, daemon=True)
    thread.start()
    assert holder_started.wait(timeout=5.0), "lock-holder thread did not start"

    started = time.monotonic()
    with psycopg.connect(os.environ["DATABASE_URL"]) as conn:
        # Should NOT hang — the function must time out the lock and
        # fail-open quickly.
        check_org_cost_budget(org_id, conn=conn)
        conn.rollback()
    elapsed = time.monotonic() - started

    release_holder.set()
    thread.join(timeout=2.0)

    assert elapsed < 5.0, (
        f"check_org_cost_budget hung for {elapsed:.1f}s with a stuck advisory "
        f"lock (must time out and fail-open in <5s)"
    )
```

- [ ] **Step 2: Run, verify fail (or skip if no DB)**

```bash
uv run pytest tests/test_chat_rate_limiter_lock_timeout.py -v
```

Expected: FAIL (the test will hang for >5s and then assert) or SKIP if no `DATABASE_URL`. If it SKIPs, document this for manual CI verification before merge.

- [ ] **Step 3: Apply the lock-timeout fix**

In `app/chat/rate_limiter.py`, find the `try:` block at line 152. Replace the `if conn is not None:` branch with:

```python
        if conn is not None:
            # Bound the advisory-lock acquire so a stuck pre-deploy
            # connection holding the lock can't hang the whole turn
            # (#658). On lock_timeout, psycopg raises LockNotAvailable
            # which the outer except catches and we fail-open.
            conn.execute("SET LOCAL lock_timeout = '3s'")
            # Serialise concurrent budget checks for this org inside the
            # caller's transaction. The lock is released on commit/rollback.
            conn.execute(
                "SELECT pg_advisory_xact_lock(hashtextextended('cost_budget:' || %s::text, 0))",
                (str(org_id),),
            )
            row = conn.execute(
                "SELECT COALESCE(SUM(cost_usd), 0) FROM llm_usage "
                "WHERE org_id = %s "
                "AND created_at >= date_trunc('month', now())",
                (str(org_id),),
            ).fetchone()
            total_cost = float(row[0]) if row else 0.0
```

The existing `except Exception:` block at line 176-179 already logs and fails open, so `LockNotAvailable` is handled.

Also extend the docstring `**TOCTOU race window and mitigation**` paragraph to add at the end:

```rst
    The advisory-lock acquire is bounded by ``SET LOCAL lock_timeout =
    '3s'``: if a stale connection holds the lock for longer than that,
    psycopg raises ``LockNotAvailable``, the outer ``except`` catches
    it, and we fail-open for this turn (the org will simply have a
    momentary unmetered spend; the next turn will see the corrected
    sum). Trade-off: brief budget-check skew during lock contention vs.
    indefinite hangs that lose user data (#658).
```

- [ ] **Step 4: Run, verify pass**

```bash
uv run pytest tests/test_chat_rate_limiter_lock_timeout.py -v
```

Expected: PASS in ~3-4 seconds.

- [ ] **Step 5: Run lint**

```bash
uv run ruff check app/chat/rate_limiter.py tests/chat/test_rate_limiter_lock_timeout.py
uv run pyright app/chat/rate_limiter.py
```

Expected: clean.

- [ ] **Step 6: Commit**

```bash
git add app/chat/rate_limiter.py tests/test_chat_rate_limiter_lock_timeout.py
git commit -m "fix(chat): bound cost-budget advisory lock with 3s lock_timeout (#658)

A stale pre-deploy connection holding pg_advisory_xact_lock for an org
could block every new chat turn for that org indefinitely. Add
SET LOCAL lock_timeout = '3s' so the lock acquire fails fast; the
outer except already fails open. Adds integration test that holds
the lock from a second connection and asserts the budget check
returns within 5s.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

---

## Task 3: Decouple user-msg persist from the budget transaction

**Files:**
- Modify: `app/chat/orchestrator.py:603-629` (step 3 block)
- Test: `tests/test_chat_orchestrator_prerag_hang.py` (create — fleshed out in Task 6)

**Why:** Even with `lock_timeout`, persisting the user message inside the budget transaction means a budget-tx error rolls back the message. We want the user message saved unconditionally so it survives any downstream failure. The TOCTOU window for billing is documented to be acceptable when the lock fails open (already true today on errors).

New step ordering (replaces lines 603-629):
- 3a. Persist user message in its OWN transaction (no advisory lock, no budget check). Bounded by `asyncio.wait_for(5s)`.
- 3b. Check budget in a separate short-lived transaction (advisory lock retained, now bounded by `lock_timeout` from Task 2). On `CostBudgetExceededError`, the user-msg row is already saved — emit error and return.

- [ ] **Step 1: Replace the step-3 block in `app/chat/orchestrator.py`**

Find lines 603-629. Replace with:

```python
        # 3a. Persist the user message FIRST in its own transaction (#658).
        # Decoupled from the budget check so a stuck pg_advisory_xact_lock
        # or any downstream failure cannot lose user input. Bounded by a
        # 5s deadline so a stalled pool surfaces as an error instead of a
        # silent hang. The persist runs in a thread because psycopg is
        # sync; ``asyncio.to_thread`` keeps the event loop responsive.
        logger.info(
            "chat.handle_message step=3a-persist-user conv=%s user=%s",
            conversation_id,
            user_id,
        )
        try:
            await asyncio.wait_for(
                asyncio.to_thread(_persist_user_message, conversation_id, user_message),
                timeout=_PRE_STREAM_DB_TIMEOUT_SECONDS,
            )
        except TimeoutError:
            logger.warning(
                "User-message persist timed out after %.1fs for conv=%s",
                _PRE_STREAM_DB_TIMEOUT_SECONDS,
                conversation_id,
            )
            try:
                await _safe_send(
                    send,
                    {
                        "type": "error",
                        "message": "Andmebaas vastab aeglaselt. Palun proovi uuesti.",
                    },
                )
            except WebSocketSendTimeout:
                pass
            return
        except Exception:
            logger.exception("Failed to persist user message for conv=%s", conversation_id)
            try:
                await _safe_send(
                    send, {"type": "error", "message": "Sõnumi salvestamine ebaõnnestus."}
                )
            except WebSocketSendTimeout:
                pass
            return

        # 3b. Budget check in a separate transaction. The user message is
        # already safely persisted; if the budget is over the user simply
        # sees an error (and can see what they tried to ask on reload).
        if org_id:
            logger.info(
                "chat.handle_message step=3b-budget conv=%s org=%s",
                conversation_id,
                org_id,
            )
            try:
                await asyncio.wait_for(
                    asyncio.to_thread(_check_budget_in_own_tx, org_id),
                    timeout=_PRE_STREAM_DB_TIMEOUT_SECONDS,
                )
            except CostBudgetExceededError as exc:
                try:
                    await _safe_send(send, {"type": "error", "message": str(exc)})
                except WebSocketSendTimeout:
                    pass
                return
            except TimeoutError:
                logger.warning(
                    "Budget check timed out after %.1fs for conv=%s; failing open",
                    _PRE_STREAM_DB_TIMEOUT_SECONDS,
                    conversation_id,
                )
                # Fail open — user message already persisted; better to
                # let this turn through than to lose data.
            except Exception:
                logger.exception("Budget check failed for conv=%s", conversation_id)
                # Fail open
```

- [ ] **Step 2: Add the two helper functions and the new constant near the top of `app/chat/orchestrator.py`**

After line 96 (where `_TURN_DEADLINE_SECONDS` is defined), add:

```python
# #658 — pre-stream DB-call deadline. Each sync DB op (rate check,
# conversation load, history load, user-msg persist, budget check)
# is wrapped in ``asyncio.wait_for(... , _PRE_STREAM_DB_TIMEOUT_SECONDS)``
# so a stalled connection pool surfaces as a friendly error event
# instead of an indefinite "thinking" spinner. 8 seconds is generous
# for a healthy DB and short enough that the user notices.
_PRE_STREAM_DB_TIMEOUT_SECONDS = 8.0

# #658 — server-side WebSocket heartbeat cadence. Emit a tiny ping
# event every N seconds for the lifetime of the connection so NAT
# idle timeouts and proxy idle-cuts can't silently kill the socket
# during long RAG / LLM rounds.
_WS_HEARTBEAT_INTERVAL_SECONDS = 25.0
```

After the existing module-level helpers (find a good spot just above `class ChatOrchestrator:` — likely around line 460 in the current file; locate via `grep -n "^class ChatOrchestrator" app/chat/orchestrator.py`), add the two helper functions:

```python
def _persist_user_message(conversation_id: UUID, user_message: str) -> None:
    """Insert the user message in a tiny standalone transaction.

    Decoupled from the budget transaction (#658) so a stuck advisory
    lock cannot lose user input. Runs in a thread via
    ``asyncio.to_thread`` because psycopg is sync.
    """
    with get_connection() as conn:
        create_message(conn, conversation_id, "user", user_message)
        conn.commit()


def _check_budget_in_own_tx(org_id: UUID | str) -> None:
    """Run the per-org cost-budget check in its own short-lived tx.

    The advisory lock taken inside ``check_org_cost_budget`` is bounded
    by ``SET LOCAL lock_timeout = '3s'`` (set inside the function),
    so a stuck lock raises ``LockNotAvailable`` which the function's
    own except catches and fails open. Raises
    :class:`CostBudgetExceededError` if the org is over budget.
    """
    with get_connection() as conn:
        check_org_cost_budget(org_id, conn=conn)
        conn.commit()
```

Confirm `UUID` is already imported (yes — used in `handle_message` signature). Confirm `create_message` is already imported. Confirm `get_connection` is already imported. If pyright flags missing imports, add them (they should already be present).

- [ ] **Step 3: Drop the old step-3 block's local helpers / comments**

The replacement block at Step 1 has its own logger.info calls and uses the new helper. Make sure no remnants of the OLD step-3 code remain (the `with get_connection() as conn:` from old line 610-614 should be entirely replaced by the new flow). Verify with:

```bash
grep -n "check_org_cost_budget(org_id, conn=conn)" app/chat/orchestrator.py
```

Expected: only one match, and that should be inside `_check_budget_in_own_tx`. If two matches remain, the old block is still there — delete it.

- [ ] **Step 4: Run the existing chat orchestrator tests**

```bash
uv run pytest tests/test_chat_*.py -x --tb=short 2>&1 | tail -20
```

Expected: PASS. If tests reference the old step-3 transaction shape (one big tx for budget+persist), they need to be updated — but the tests at this layer should be mostly behavioural ("user message ends up in DB" / "CostBudgetExceededError emits error event") which the new code preserves.

- [ ] **Step 5: Lint**

```bash
uv run ruff check app/chat/orchestrator.py
uv run pyright app/chat/orchestrator.py
```

Expected: clean.

- [ ] **Step 6: Commit**

```bash
git add app/chat/orchestrator.py
git commit -m "fix(chat): decouple user-msg persist from budget tx, bound pre-stream DB ops (#658)

The user-message INSERT lived inside the same transaction as
check_org_cost_budget's pg_advisory_xact_lock — a stuck lock blocked
the persist forever and on reload the user's message was gone.

Now: persist user message in its own tiny tx, then run the budget
check in a separate short-lived tx (advisory lock now bounded by
lock_timeout from the previous commit). Each step is wrapped in
asyncio.wait_for(8s) + asyncio.to_thread so the event loop stays
responsive and a stalled pool surfaces as an error event.

Step-level logger.info added so future hangs are visible in logs.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

---

## Task 4: Bound the rate-limit / conversation / history loads with `wait_for`

**Files:**
- Modify: `app/chat/orchestrator.py:534-589` (steps 0-2)

**Why:** Steps 0, 1, 2 still use sync `with get_connection()` blocks with no deadline. A stuck pool here also hangs the turn before we ever reach the persist. Same mechanical fix.

- [ ] **Step 1: Wrap step 0 (rate check) in `asyncio.to_thread` + `asyncio.wait_for`**

Find lines 534-546. Replace with:

```python
        # 0. Per-user message rate limit (cheap pre-check, fail-open on DB
        # error). Bounded by _PRE_STREAM_DB_TIMEOUT_SECONDS via
        # asyncio.wait_for so a stuck pool can't hang the turn here (#658).
        logger.info(
            "chat.handle_message step=0-rate-check conv=%s user=%s",
            conversation_id,
            user_id,
        )
        try:
            if user_id:
                await asyncio.wait_for(
                    asyncio.to_thread(check_message_rate, user_id),
                    timeout=_PRE_STREAM_DB_TIMEOUT_SECONDS,
                )
        except RateLimitExceededError as exc:
            try:
                await _safe_send(send, {"type": "error", "message": str(exc)})
            except WebSocketSendTimeout:
                pass
            return
        except TimeoutError:
            logger.warning(
                "Rate-limit check timed out after %.1fs for user=%s; failing open",
                _PRE_STREAM_DB_TIMEOUT_SECONDS,
                user_id,
            )
            # Fail open — let the turn through; a stuck rate-limit DB
            # call shouldn't block a paying user from chatting.
```

- [ ] **Step 2: Wrap step 1 (conversation load)**

Find lines 548-579 (the conversation-load + access-control block). Replace the load itself:

```python
        # 1. Load conversation. Bounded for the same reason as step 0.
        logger.info(
            "chat.handle_message step=1-load-conv conv=%s",
            conversation_id,
        )
        try:
            conversation = await asyncio.wait_for(
                asyncio.to_thread(_load_conversation, conversation_id),
                timeout=_PRE_STREAM_DB_TIMEOUT_SECONDS,
            )
        except TimeoutError:
            logger.warning(
                "Conversation load timed out after %.1fs for conv=%s",
                _PRE_STREAM_DB_TIMEOUT_SECONDS,
                conversation_id,
            )
            try:
                await _safe_send(
                    send,
                    {
                        "type": "error",
                        "message": "Andmebaas vastab aeglaselt. Palun proovi uuesti.",
                    },
                )
            except WebSocketSendTimeout:
                pass
            return
        except Exception:
            logger.exception("Failed to load conversation %s", conversation_id)
            try:
                await _safe_send(
                    send, {"type": "error", "message": "Vestluse laadimine ebaõnnestus."}
                )
            except WebSocketSendTimeout:
                pass
            return
```

Keep the existing `if conversation is None:` and `if not can_access_conversation(...)` checks unchanged after the load.

Add the helper near the others (just above `class ChatOrchestrator`):

```python
def _load_conversation(conversation_id: UUID):
    """Load a conversation row in a sync `with get_connection()` block."""
    with get_connection() as conn:
        return get_conversation(conn, conversation_id)
```

- [ ] **Step 3: Wrap step 2 (message history)**

Find lines 581-589. Replace with:

```python
        # 2. Load message history. Bounded; on error or timeout we
        # proceed with empty history so the turn can still happen.
        logger.info(
            "chat.handle_message step=2-load-history conv=%s",
            conversation_id,
        )
        try:
            history = await asyncio.wait_for(
                asyncio.to_thread(_load_history, conversation_id),
                timeout=_PRE_STREAM_DB_TIMEOUT_SECONDS,
            )
        except TimeoutError:
            logger.warning(
                "History load timed out after %.1fs for conv=%s; using empty",
                _PRE_STREAM_DB_TIMEOUT_SECONDS,
                conversation_id,
            )
            history = []
        except Exception:
            logger.exception("Failed to load messages for conv=%s", conversation_id)
            history = []

        history_length_before = len(history)
```

Add the helper:

```python
def _load_history(conversation_id: UUID):
    """Load message history for a conversation."""
    with get_connection() as conn:
        return list_messages(conn, conversation_id)
```

- [ ] **Step 4: Run existing tests**

```bash
uv run pytest tests/test_chat_*.py -x --tb=short 2>&1 | tail -20
```

Expected: PASS. The new code paths preserve the existing fail-open behaviour for steps 0-2 errors; only the timeout branches are new.

- [ ] **Step 5: Lint**

```bash
uv run ruff check app/chat/orchestrator.py
uv run pyright app/chat/orchestrator.py
```

Expected: clean.

- [ ] **Step 6: Commit**

```bash
git add app/chat/orchestrator.py
git commit -m "fix(chat): wrap pre-stream rate/conv/history loads with wait_for + to_thread (#658)

Steps 0-2 (rate-limit check, conversation load, history load) used
sync 'with get_connection()' blocks with no deadline. A stuck pool
hung the turn before we even reached the persist step. Now each is
wrapped in asyncio.wait_for(8s) + asyncio.to_thread; on timeout we
fail open (rate, history) or surface a friendly error (conv load).

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

---

## Task 5: Server-side WebSocket heartbeat

**Files:**
- Modify: `app/chat/websocket.py` — start a heartbeat task at WS connect, cancel on disconnect
- Modify: `app/chat/orchestrator.py` — emit one `{"type": "ping"}` event constant + helper (export so the WS layer can use it)
- Test: `tests/test_chat_websocket_heartbeat.py` (create)

**Why:** Production WS hangs above showed no events emitted between the user's send and the eventual timeout. NAT and proxy layers can drop idle TCP. A 25s heartbeat keeps the path warm and gives the client unambiguous "server is alive" evidence; the existing client log shows the close event will trigger reconnect logic.

- [ ] **Step 1: Write the smoke test first**

Create `tests/test_chat_websocket_heartbeat.py`:

```python
"""WebSocket heartbeat — server emits {'type': 'ping'} every ~25s."""
from __future__ import annotations

import asyncio
import json

import pytest

from app.chat.websocket import _start_heartbeat, _WS_HEARTBEAT_INTERVAL_SECONDS


@pytest.mark.asyncio
async def test_heartbeat_emits_ping_on_interval(monkeypatch):
    """The heartbeat task emits at least one ping per interval and
    exits cleanly on cancel."""
    # Override the interval so the test is fast.
    monkeypatch.setattr(
        "app.chat.websocket._WS_HEARTBEAT_INTERVAL_SECONDS", 0.05
    )

    received: list[dict] = []

    async def fake_send(payload: str) -> None:
        received.append(json.loads(payload))

    task = _start_heartbeat(fake_send)
    try:
        await asyncio.sleep(0.18)  # allow ~3 ticks
    finally:
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    pings = [e for e in received if e.get("type") == "ping"]
    assert len(pings) >= 2, f"expected at least 2 pings in 180ms, got {len(pings)}: {received}"


@pytest.mark.asyncio
async def test_heartbeat_swallows_send_errors(monkeypatch):
    """Heartbeat survives a transient send failure without crashing."""
    monkeypatch.setattr(
        "app.chat.websocket._WS_HEARTBEAT_INTERVAL_SECONDS", 0.02
    )

    calls: list[int] = []

    async def flaky_send(payload: str) -> None:
        calls.append(len(calls))
        if len(calls) == 2:
            raise RuntimeError("transient")

    task = _start_heartbeat(flaky_send)
    try:
        await asyncio.sleep(0.1)
    finally:
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    assert len(calls) >= 3, "heartbeat must keep ticking after a send error"
```

- [ ] **Step 2: Run, verify fail (heartbeat helper does not exist yet)**

```bash
uv run pytest tests/test_chat_websocket_heartbeat.py -v
```

Expected: FAIL with `ImportError` for `_start_heartbeat`.

- [ ] **Step 3: Implement the heartbeat helper in `app/chat/websocket.py`**

Near the top of `app/chat/websocket.py`, after the existing imports but before `async def on_connect`, add:

```python
# #658 — server-side WS heartbeat. Emit a tiny ping every N seconds for
# the lifetime of the connection so NAT idle timeouts / proxy idle-cuts
# can't silently kill the socket during long RAG / LLM rounds.
_WS_HEARTBEAT_INTERVAL_SECONDS = 25.0


def _start_heartbeat(send: Any) -> asyncio.Task[None]:
    """Spawn a background task that emits ``{"type": "ping"}`` every
    ``_WS_HEARTBEAT_INTERVAL_SECONDS``.

    Returns the task so the caller can cancel it on disconnect. Send
    errors are logged at DEBUG and swallowed — the task keeps ticking
    so a transient send failure doesn't take down the heartbeat.
    """

    async def _beat() -> None:
        while True:
            try:
                await asyncio.sleep(_WS_HEARTBEAT_INTERVAL_SECONDS)
                await send(json.dumps({"type": "ping"}))
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.debug("WS heartbeat send failed (continuing)", exc_info=True)

    task = asyncio.create_task(_beat())
    return task
```

(`asyncio` and `json` are already imported at the top of `websocket.py`; `logger` already exists.)

- [ ] **Step 4: Wire the heartbeat into the WS handshake**

Find the `on_connect` and `on_disconnect` functions. They are simple now and don't have access to a per-connection state dict. The cleanest hook is inside `_ws_handler` (the wrapper at line ~298): start the heartbeat once per connection in the `_per_send_tasks` registry. Or — simplest — start it at the top of `ws_chat` for the duration of one message.

Actually the cleanest place is in `register_chat_ws_routes` where the `_per_send_tasks` dict is set up. Read the current shape:

```bash
sed -n '258,402p' app/chat/websocket.py
```

Now patch `register_chat_ws_routes` to:

(a) Add a per-connection heartbeat-task registry alongside `_per_send_tasks` / `_active_orchestrator_tasks`.

(b) Modify `on_connect` (visible at line 40) to start a heartbeat task for the connection.

(c) Modify `on_disconnect` (line 46) to cancel the heartbeat task.

The simplest implementation that actually works given the FastHTML ws decorator API uses an `asyncio.Lock`-protected per-process dict keyed by the `send` callable's `id()` (since the ws handler is given the `send` directly with no per-connection id). Read how `_per_send_tasks` is populated and follow that pattern verbatim.

```bash
grep -n "_per_send_tasks\|_active_orchestrator_tasks\|on_connect\|on_disconnect" app/chat/websocket.py
```

Use the same lifecycle pattern. Concretely, add a module-level dict `_heartbeat_tasks: dict[int, asyncio.Task[None]] = {}` near the other `_per_send_tasks`, and in `on_connect`:

```python
async def on_connect(send: Any) -> None:
    """Called when a WebSocket client connects to /ws/chat."""
    logger.info("Chat WS client connected")
    await send(json.dumps({"type": "connected"}))
    _heartbeat_tasks[id(send)] = _start_heartbeat(send)
```

…and in `on_disconnect`:

```python
async def on_disconnect(send: Any) -> None:
    """Called when a WebSocket client disconnects."""
    logger.info("Chat WS client disconnected")
    task = _heartbeat_tasks.pop(id(send), None)
    if task is not None:
        task.cancel()
        try:
            await task
        except (asyncio.CancelledError, Exception):
            pass
```

If the lifecycle pattern in `register_chat_ws_routes` already uses different keys (e.g. wraps `send` so `id()` differs between connect/disconnect), follow whatever pattern `_per_send_tasks` uses verbatim.

- [ ] **Step 5: Update the client to NOT log/display ping events**

Open `app/static/js/chat.js`. Find the WS `onmessage` handler. The ping event must be silently dropped — it has no UI effect. Look for the existing event-type switch (likely a `switch (data.type)` or similar) and add:

```js
        case 'ping':
            // Server-side heartbeat — keeps the WS open through NAT
            // idle timeouts. No UI effect.
            return;
```

Locate the switch with:

```bash
grep -n "data\.type\|event\.type" app/static/js/chat.js | head -20
```

Add the case to whichever existing structure handles event dispatch.

- [ ] **Step 6: Run heartbeat tests**

```bash
uv run pytest tests/test_chat_websocket_heartbeat.py -v
```

Expected: PASS.

- [ ] **Step 7: Run full chat test suite**

```bash
uv run pytest tests/chat -x --tb=short 2>&1 | tail -10
```

Expected: PASS at the same count or higher.

- [ ] **Step 8: Lint**

```bash
uv run ruff check app/chat tests/chat/test_chat_websocket_heartbeat.py
uv run pyright app/chat
```

Expected: clean.

- [ ] **Step 9: Commit**

```bash
git add app/chat/websocket.py app/static/js/chat.js tests/test_chat_websocket_heartbeat.py
git commit -m "feat(chat): server-side WS heartbeat (25s ping) (#658)

Adds {'type': 'ping'} every 25s for the connection lifetime so NAT
idle timeouts and proxy idle-cuts can't silently kill the socket
during long RAG / LLM rounds. Client-side: dropped silently — no UI
effect. Includes unit tests for cadence and error resilience.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

---

## Task 6: Regression test — orchestrator with hanging RAG must persist user msg

**Files:**
- Create: `tests/test_chat_orchestrator_prerag_hang.py`

**Why:** The whole point of #658. Future regressions of "user msg lost on reload when RAG hangs" must be caught by an automated test, not by users in production.

- [ ] **Step 1: Write the regression test**

```python
"""Regression: ChatOrchestrator must persist the user message before
RAG retrieval, and an unresponsive retriever must NOT lose user input.

Closes #658.
"""

from __future__ import annotations

import asyncio
import os
import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


@pytest.mark.asyncio
@pytest.mark.integration
async def test_user_message_persists_when_retriever_hangs():
    """If the embedder/retriever hangs forever, the user's message
    must already be in the DB by the time RAG starts, and the turn
    must surface a timeout error instead of silently swallowing the
    message."""
    if not os.getenv("DATABASE_URL"):
        pytest.skip("integration test — DATABASE_URL not set")

    from app.chat.orchestrator import ChatOrchestrator
    from app.db import get_connection

    # Bootstrap a conversation owned by a synthetic user.
    user_id = uuid.uuid4()
    org_id = uuid.uuid4()
    conv_id = uuid.uuid4()

    with get_connection() as conn:
        conn.execute(
            "INSERT INTO orgs (id, name) VALUES (%s, %s) ON CONFLICT DO NOTHING",
            (org_id, "test-org-658"),
        )
        conn.execute(
            "INSERT INTO users (id, email, password_hash, full_name, role, org_id) "
            "VALUES (%s, %s, %s, %s, %s, %s)",
            (user_id, f"user-{user_id}@example.com", "x", "Test", "drafter", org_id),
        )
        conn.execute(
            "INSERT INTO conversations (id, user_id, org_id, title) "
            "VALUES (%s, %s, %s, %s)",
            (conv_id, user_id, org_id, "test conv 658"),
        )
        conn.commit()

    # Build a hanging retriever — its embed() never returns.
    hanging_retriever = MagicMock()

    async def _never_returns(*args, **kwargs):
        await asyncio.sleep(3600)

    hanging_retriever.retrieve = AsyncMock(side_effect=_never_returns)

    orchestrator = ChatOrchestrator(MagicMock())
    # Patch _get_retriever on the instance so RAG is attempted but stalls.
    with patch.object(orchestrator, "_get_retriever", return_value=hanging_retriever):
        events: list[dict] = []

        async def collect(event):
            events.append(event)

        # The RAG retrieve has its own 15s deadline; we expect the
        # orchestrator to either time out RAG (proceed without context)
        # or, if the LLM call also hangs, hit the 120s turn deadline.
        # Either way the user message must already be in the DB.
        async def run_with_short_deadline():
            try:
                await asyncio.wait_for(
                    orchestrator.handle_message(
                        conv_id,
                        "Tere, see on test 658.",
                        {"id": str(user_id), "org_id": str(org_id)},
                        collect,
                    ),
                    # Just enough to clear the pre-stream phase + RAG
                    # timeout (15s). Below the 120s turn deadline.
                    timeout=20.0,
                )
            except TimeoutError:
                pass  # Expected if the LLM mock isn't wired

    # Verify: user message IS in the DB regardless of what happened
    # downstream. This is the data-loss guarantee #658 ships.
    with get_connection() as conn:
        row = conn.execute(
            "SELECT COUNT(*) FROM messages WHERE conversation_id = %s AND role = 'user'",
            (conv_id,),
        ).fetchone()
    assert row is not None
    assert row[0] == 1, (
        f"User message must persist even when RAG hangs; got {row[0]} "
        f"messages instead. Events emitted: {[e.get('type') for e in events]}"
    )

    # Cleanup
    with get_connection() as conn:
        conn.execute("DELETE FROM messages WHERE conversation_id = %s", (conv_id,))
        conn.execute("DELETE FROM conversations WHERE id = %s", (conv_id,))
        conn.execute("DELETE FROM users WHERE id = %s", (user_id,))
        conn.execute("DELETE FROM orgs WHERE id = %s", (org_id,))
        conn.commit()
```

- [ ] **Step 2: Run, verify pass**

```bash
uv run pytest tests/test_chat_orchestrator_prerag_hang.py -v
```

Expected: PASS in ~16-21 seconds (RAG times out at 15s; ws ping helper does its thing). If SKIP because no `DATABASE_URL`, document for CI.

If the test fails because the orchestrator's LLM mock raises early (e.g. `MagicMock` doesn't have `.astream`), patch out the LLM call too — the test only cares that the user-msg row exists in the DB after the orchestrator returns.

- [ ] **Step 3: Lint**

```bash
uv run ruff check tests/test_chat_orchestrator_prerag_hang.py
```

Expected: clean.

- [ ] **Step 4: Commit**

```bash
git add tests/test_chat_orchestrator_prerag_hang.py
git commit -m "test(chat): regression test for #658 — user msg persists when RAG hangs

Spins up a synthetic conversation, patches the retriever to hang
forever, runs the orchestrator with a short outer deadline, then
verifies the user-message row is in the DB. This is the data-loss
guarantee the rest of this PR ships.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

---

## Task 7: Update top-of-file orchestrator docstring + step-3 comment

**Files:**
- Modify: `app/chat/orchestrator.py` — module docstring + `handle_message` step-3 comment.

**Why:** The module docstring at line ~14-30 references the old design ("budget check inside the same transaction as create_message"). Other contributors will be confused if the comment lies. Also tighten the step-3 comment that we left in Task 3.

- [ ] **Step 1: Find the module docstring**

```bash
sed -n '1,40p' app/chat/orchestrator.py
```

- [ ] **Step 2: Update the relevant lines** to reflect the new design

Find any sentence describing the old budget-and-persist-in-one-tx pattern and replace with a sentence describing the new flow: "the user message is persisted in its own tiny transaction first, then the budget check runs separately with `lock_timeout` defending against stuck advisory locks (#658)."

- [ ] **Step 3: Lint**

```bash
uv run ruff check app/chat/orchestrator.py
```

Expected: clean.

- [ ] **Step 4: Commit**

```bash
git add app/chat/orchestrator.py
git commit -m "docs(chat): update orchestrator docstring to reflect decoupled persist (#658)

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

---

## Task 8: Full verification + push + PR

**Files:** none.

- [ ] **Step 1: Full ruff**

```bash
uv run ruff check .
uv run ruff format --check .
```

Expected: clean.

- [ ] **Step 2: Full pyright**

```bash
uv run pyright
```

Expected: clean.

- [ ] **Step 3: Full pytest**

```bash
uv run pytest --tb=short 2>&1 | tail -15
```

Expected: pre-task baseline + new tests, all PASS.

- [ ] **Step 4: Manual smoke (browser, optional but recommended)**

```bash
uv run python -m app.main &
APP_PID=$!
# Open http://127.0.0.1:5001/chat in browser
# - Send a message
# - Open DevTools → Network → WS → confirm "ping" frames every ~25s
# - In another terminal, hold the cost-budget advisory lock manually:
#     psql $DATABASE_URL -c "BEGIN; SELECT pg_advisory_xact_lock(hashtextextended('cost_budget:<your-org-uuid>', 0));"
#   then send a chat message — should error within ~3-8s instead of hanging
# - Release the lock with: \q in psql
kill $APP_PID
```

- [ ] **Step 5: Push + create PR**

```bash
git push -u origin fix/658-chat-prerag-hang
gh pr create --title "Fix #658: chat user-msg lost when pre-RAG DB calls hang" --body "$(cat <<'EOF'
## Summary

Fixes the production data-loss bug where chat WebSocket turns hang past 215s with no error and user messages disappear on reload.

Root cause: the user-message INSERT lived inside the same transaction as `check_org_cost_budget`'s `pg_advisory_xact_lock`. A pre-deploy connection still holding that lock blocked every new turn for that org indefinitely.

## Changes

- **`app/chat/rate_limiter.py`**: bound the advisory-lock acquire with `SET LOCAL lock_timeout = '3s'`. A stuck lock now raises `LockNotAvailable` and the existing fail-open path takes over.
- **`app/chat/orchestrator.py`**: persist user message in its OWN transaction first, then run the budget check separately. Wrap each pre-stream sync DB call (rate check, conv load, history load, persist, budget) in `asyncio.wait_for(8s)` + `asyncio.to_thread(...)` so a stalled pool surfaces as a friendly error event instead of an indefinite spinner. Add step-level `logger.info` for diagnostics.
- **`app/chat/websocket.py`**: emit `{"type": "ping"}` every 25s for the connection lifetime so NAT idle timeouts and proxy idle-cuts can't silently kill the socket during long RAG / LLM rounds.
- **`app/static/js/chat.js`**: drop ping events silently (no UI effect).

## Tests added

- `tests/chat/test_rate_limiter_lock_timeout.py` — holds the cost-budget advisory lock from a second connection and asserts the budget check returns within 5s.
- `tests/chat/test_chat_websocket_heartbeat.py` — asserts heartbeat cadence + resilience to transient send errors.
- `tests/chat/test_orchestrator_prerag_hang.py` — patches the retriever to hang forever; asserts the user-message row IS in the DB by the time the orchestrator returns. This is the data-loss regression guard.

## Out of scope

- #660 (sync psycopg in `_persist_partial_assistant` cancel handlers) — separate follow-up.
- #655 client "Peatamine…" UI feedback — the underlying cancellation issue is largely fixed by removing uninterruptible sync blocks; the UX polish is a follow-up.
- #654 / #663 chat-list HTMX feedback — different files, different bug.

## Test plan

- [ ] CI: ruff + pyright + pytest all green
- [ ] Manual: hold cost-budget advisory lock from psql; send chat message → errors within ~3-8s instead of hanging
- [ ] Manual: open DevTools WS view; observe `ping` frames every ~25s during a long RAG round
- [ ] Manual on staging: send a message during a deliberate Voyage-AI block (firewall block) → user msg appears in DB on reload, error event shown to user

🤖 Generated with [Claude Code](https://claude.com/claude-code)
EOF
)"
```

- [ ] **Step 6: Return the PR URL** to the user.

---

## Self-Review

- **Spec coverage:** Every symptom in #658 is mapped to a task: silent hang past 215s (Task 4 + Task 3 + Task 5), user msg lost on reload (Task 3 — persist outside lock), no error event (timeout branches in Tasks 3-4), 120s deadline not firing (Task 4 — pre-stream now has its own 8s deadline per call, no longer waits on the 120s wrapper that only covered the LLM stream).
- **Placeholder scan:** No "TBD" / "appropriate" / "similar to". Each step shows code or exact commands.
- **Type consistency:** `_persist_user_message(conversation_id: UUID, user_message: str) -> None`, `_check_budget_in_own_tx(org_id: UUID | str) -> None`, `_load_conversation(conversation_id: UUID)`, `_load_history(conversation_id: UUID)`, `_start_heartbeat(send: Any) -> asyncio.Task[None]`. All consistent across tasks.
- **Risk: client-side ping handling.** Task 5 step 5 modifies `app/static/js/chat.js` blindly using a `case 'ping':` line. If chat.js doesn't use a switch (it might use `if/else` or an event-name registry), the patcher must read the actual file and adapt. The instruction in step 5 step 5 says "add the case to whichever existing structure handles event dispatch" — but the engineer must verify before editing.
- **Risk: tests need real Postgres.** Tasks 2 + 6 are integration tests that skip without `DATABASE_URL`. CI must run them with a real DB or the regression guard is paper-thin. Mitigate: confirm the existing CI workflow (`lint-and-test` job) sets `DATABASE_URL` before merge.
- **Risk: heartbeat lifecycle keying by `id(send)`.** If FastHTML wraps `send` such that `id(send)` differs between connect and disconnect, the heartbeat task leaks. Task 5 step 4 instructs the engineer to verify the existing `_per_send_tasks` keying pattern and follow it verbatim — not paraphrase it.
- **Untested locally:** This plan was written against the file at HEAD on `main` (913c0cd). If files have shifted between plan-write and plan-execute, line numbers in step "find" instructions may need adjustment; use the `grep`s in Task 1 step 2 as ground truth.
