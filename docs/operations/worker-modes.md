# Background worker modes (`WORKER_MODE`)

Issue [#348](https://github.com/henrikaavik/seadusloome/issues/348). Lets the
`JobWorker` that drains the `background_jobs` Postgres table run either in
the FastHTML web container (default) or in a separate container.

## Modes

### `inproc` — default

Workers run as a daemon thread inside the FastHTML app process, spawned
by the ASGI lifespan hook in `app/main.py`.

- Simplest to operate. Nothing extra to deploy.
- The right choice for local dev (`uv run app/main.py`), CI, and any
  production deployment where one web container is enough.
- Set explicitly via `WORKER_MODE=inproc` or leave unset.

### `standalone`

Workers run in a separate process (typically a second Coolify container)
launched via `scripts/run_worker.py`. The web container's lifespan
recognises `WORKER_MODE=standalone` and **skips** spawning the in-process
worker, so jobs are not double-claimed.

Use this mode when:

- Scaling the web tier beyond a single container (each replica would
  otherwise spawn its own worker thread).
- Background job CPU / memory is dominating the web container's
  resource envelope.
- You want to deploy worker code without bouncing the web tier (or
  vice versa).

## Switching modes

### Local dev (docker-compose)

The `worker` service block in `docker/docker-compose.yml` is commented
out by default. To enable standalone mode locally:

1. Uncomment the `worker:` block in `docker/docker-compose.yml`.
2. Add `WORKER_MODE=standalone` to the `app:` service environment (so
   the web container's lifespan skips its in-process worker).
3. `docker compose -f docker/docker-compose.yml up -d`.

### Production (Coolify)

1. On the existing `seadusloome-app` resource, set
   `WORKER_MODE=standalone` under Environment Variables → Production.
2. Add a second Coolify application using the same image:
   - Source: same Git repo / image as `seadusloome-app`.
   - Start command: `python scripts/run_worker.py` (or
     `seadusloome-worker` via the console script — see
     `pyproject.toml [project.scripts]`).
   - Environment: copy the `seadusloome-app` env vars (`DATABASE_URL`,
     `JENA_URL`, `STORAGE_ENCRYPTION_KEY`, `ANTHROPIC_API_KEY`,
     `VOYAGE_API_KEY`, `TIKA_URL`, `STORAGE_DIR`, `EXPORT_DIR`, …) plus
     `WORKER_MODE=standalone`.
   - Persistent storage: mount the same `drafts` and `exports` named
     volumes as `seadusloome-app` so parse/extract/export handlers can
     read/write the encrypted files.
   - Health: no HTTP endpoint — Coolify will only know the container
     is alive if it stays up. Container logs and the
     `background_jobs.claimed_by` column are the source of truth for
     "is the worker actually processing jobs".
3. Redeploy both applications.

To roll back, set `WORKER_MODE` back to `inproc` (or unset) on
`seadusloome-app`, redeploy, and stop / delete the worker resource.

## Shared internals

Both modes import the same handler modules via
`app.jobs.registry.register_all_handlers()` and read/write the same
`background_jobs` table with `FOR UPDATE SKIP LOCKED`. That means you
can mix-and-match (one `inproc` web + N `standalone` workers) without
any code change — the dispatch contention is handled at the DB row
level. In practice picking one mode and staying consistent keeps the
operational model simpler.

## Graceful shutdown

`scripts/run_worker.py` installs `SIGTERM` and `SIGINT` handlers that
set the worker's stop event. The worker finishes its in-flight job
(if any), exits the dispatch loop, and the process terminates with
exit code 0. Coolify / Docker delivers `SIGTERM` on container stop, so
no further configuration is required.

## Archive-warning scheduler

The 90-day draft auto-archive warning scheduler (`#572`,
`app/jobs/archive_warning.py`) **always runs in the web container's
lifespan**, never in the standalone worker. A daily scan must happen
exactly once per deployment; co-locating it with the singleton web
process is the simplest way to guarantee that. If you ever scale the
web tier to N replicas, you will need a dedicated cron container — out
of scope for #348.
