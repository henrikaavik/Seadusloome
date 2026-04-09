"""Postgres-backed background job queue.

Phase 2 introduces a handful of async pipelines (draft parsing, entity
extraction, impact analysis, .docx export) that must not block HTTP
request handlers. Rather than pulling in Celery + Redis we use the
``background_jobs`` Postgres table as the queue and claim rows with
``SELECT ... FOR UPDATE SKIP LOCKED`` so multiple workers share a
single DB safely.

Public exports:
    - ``Job``         dataclass mirror of the ``background_jobs`` row
    - ``JobQueue``    high-level wrapper exposing enqueue/claim/mark_*
"""

from app.jobs.queue import Job, JobQueue

__all__ = ["Job", "JobQueue"]
