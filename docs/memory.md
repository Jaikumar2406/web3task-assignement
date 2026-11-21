# memory.md — Project Memory & Decision Log

> **Purpose:** persistent context for the developer *and any AI coding assistant*. Read this file **first** at the start of every session. Update it **last** at the end of every session.
> Keep it short, factual and current. Delete stale info rather than letting it rot.

---

## 1. Project Snapshot

- **Name:** Distributed Webhook Dispatcher Engine
- **What:** API accepts webhook events → persists in PostgreSQL → queues via Celery/Redis → worker delivers via HTTPX → retries with exponential backoff → per-destination token-bucket rate limiting → every attempt logged.
- **Context:** Backend Developer Intern take-home, 48 h deadline.
- **Stack:** Python 3.11+, FastAPI, Celery, Redis, PostgreSQL, SQLAlchemy, HTTPX, Docker, Render, Pytest.
- **Docs:** `prd.md` (what) · `architecture.md` (how) · `phases.md` (order of work) · `rules.md` (how to code) · `memory.md` (this file).
- **Deliverables:** live API URL · GitHub repo · README.

## 2. Current Status

> Update after every session.

| Item | Value |
|---|---|
| **Current phase** | Phase 5 — local verification complete; deployed checks pending URLs |
| **Last completed task** | Final local submission verification: 90 tests green, ruff/black clean, Docker healthy, clean tree at `2575ab8` |
| **Next task** | User sends live API URL and public repo URL; run deployed Phase 5 scenarios and record both URLs |
| **Blockers** | Live URL and repo URL pending from user; no commit pending |
| **Deadline** | _fill in: date/time_ |
| **Live URL** | _not deployed yet_ |
| **Repo URL** | _fill in_ |

### Phase tracker
- [x] Phase 0 — Setup (verified; committed in `42e9b81`)
- [x] Phase 1 — Core backend (verified; committed in `42e9b81`)
- [x] Phase 2 — Reliability (verified; committed in `42e9b81`)
- [x] Phase 3 — Protection (verified; committed in `42e9b81`)
- [ ] Phase 4 — Finalization (local work committed in `42e9b81`; Render deploy + URL pending)
- [ ] Phase 5 — Verification & submission (local checks done; deployed scenarios + URLs pending)

## 3. Non-Negotiable Facts (quick reference)

- Ingestion returns **202** and **never waits** on the destination.
- **Persist (commit) before enqueue.**
- Retry schedule: attempt 1 immediate → **2 s → 4 s → 8 s**. Max **3 retries = 4 total attempts**, then `failed`.
- Retry on: **5xx, 429, timeout, network error.** Do **not** retry other 4xx (400, 404) or 3xx.
- Job statuses: `queued`, `processing`, `retrying`, `success`, `failed`.
- One `delivery_logs` row **per attempt**.
- Rate limit: **token bucket in Redis (Lua)**, per destination **origin**. Limited jobs are **rescheduled, not dropped, not counted as attempts.**
- DB is the **source of truth**; a **sweeper** re-enqueues stale non-terminal jobs.
- Delivery guarantee: **at-least-once.**
- `jobId` exposed as `job_<uuid>`; DB PK is the raw UUID.

## 4. Decision Log

Format: `ID — Decision — Reason — Date`. Never delete entries; mark superseded ones.

| ID | Decision | Reason |
|---|---|---|
| D-01 | PostgreSQL is authoritative; Redis is transport only | Redis can lose data; requirement NFR-02 |
| D-02 | Attempts tracked in DB (`attempt_count`), reschedule with `apply_async(countdown=…)`, **not** `self.retry` | Rate-limit deferrals must not consume retries; single counter |
| D-03 | `acks_late=True`, `reject_on_worker_lost=True`, `prefetch_multiplier=1` | Crash-safe redelivery |
| D-04 | `event_id` indexed but **not unique** | PRD doesn't require idempotent ingestion; avoids surprising 409s. Stretch: optional idempotency |
| D-05 | Rate-limiter key = normalized origin `scheme://host:port` | "Destination" needs a concrete definition |
| D-06 | Token bucket via Redis Lua using Redis `TIME` | Atomic, distributed, burst-friendly |
| D-07 | Fail **closed** if Redis unavailable in limiter (reschedule) | Never hammer a destination unthrottled |
| D-08 | Event ID / destination URL in logs obtained via JOIN, not duplicated columns | Keeps PRD table 2 exactly; normalization |
| D-09 | Sync SQLAlchemy + `def` FastAPI routes | Simpler with Celery; threadpool is non-blocking enough |
| D-10 | Redirects (3xx) not followed; treated as permanent failure | Avoid SSRF bypass / surprising behavior |
| D-11 | SSRF guard on by default; `ALLOW_PRIVATE_DESTINATIONS=true` for dev/tests | Security vs. testability with local mock servers |
| D-12 | Retry jitter **off by default** | Tests must assert exact 2/4/8 |
| D-13 | Free-tier Render has no free Background Worker → run Celery worker+beat in same container as API via `start.sh` *(re-verify current Render pricing before final deploy)* | Cost; still satisfies shared Redis/Postgres requirement |
| D-14 | Return **503** (not 202) when enqueue fails after commit; job stays `queued` and is recovered by sweeper | Honest signal to the client; no loss |
| D-15 | Pin `redis>=6.1,<7` in `requirements.txt` | Celery 5.7 / kombu 5.7 refuses redis-py < 6.1 at worker startup |
| D-16 | Redis limiter outages defer by `BACKOFF_BASE_SECONDS` | Fail closed without inventing another environment variable |

## 5. Gotchas & Lessons (add as you discover them)

- Lua scripts: floats returned from Redis are **truncated to integers** → return the wait time as a string (`tostring`).
- `postgres://` URLs from Render must be rewritten to `postgresql+psycopg2://` for SQLAlchemy.
- Celery countdown/ETA tasks live in worker memory until due → a worker restart can lose them; this is exactly why the sweeper exists.
- Redis broker `visibility_timeout` must exceed the longest countdown (default 1 h is OK).
- SQLAlchemy `JSONB` doesn't exist on SQLite → use `JSON().with_variant(JSONB, "postgresql")` if running unit tests on SQLite.
- Don't `sleep` in tests — assert the `countdown` passed to `apply_async`.
- Free Render web services spin down when idle → worker stops too → pending jobs resume via sweeper on wake.
- Make sure the sweeper threshold (120 s) ≫ max normal delay, otherwise it causes duplicate deliveries.
- Celery 5.7 / kombu 5.7 raises `VersionMismatch` at worker startup with redis-py < 6.1 — pin `redis>=6.1`.
- Docker Desktop is not always running on this Windows host; start it before `docker compose up`.
- Pydantic response models using `alias_generator` also need `populate_by_name=True` to be constructed with snake_case kwargs.
- Raising `fastapi.HTTPException` from a Pydantic field validator propagates correctly (used for 413 payload cap) — no custom handler needed.
- Ruff flags `Depends(...)` in defaults as B008; use `Annotated[Session, Depends(get_db)]`.
- SQLAlchemy `Session.close()` detaches instances; capture `job.id` before a task that closes a shared test session.
- Rate-limiter keys are `rl:<normalized-origin>`; integration tests must flush `rl:*test-*` so old buckets do not leak between runs.
- SQLite returns naive datetimes from `DateTime(timezone=True)`; normalize with `.replace(tzinfo=UTC)` before comparing with `utcnow()`.
- Host port 6379 is occupied by `wslrelay` here; do not publish Redis in compose — limiter tests use the existing localhost Redis.
- PowerShell mangles inline JSON passed to `curl.exe` here; use `Invoke-RestMethod` for local smoke checks.
- A clean checkout needs no `.env` for tests because `tests/conftest.py` sets safe defaults.
- Render free-tier deploy uses `./start.sh`: embedded Celery beat (`-B`) and worker run beside Uvicorn; `$PORT` is respected.

## 6. Open Questions

> Resolve, then move to the Decision Log.

- [ ] Is a paid Render Background Worker acceptable? (Decides D-13.)
- [ ] Does the reviewer expect `eventId` idempotency? (Currently: no — D-04.)
- [ ] Alembic vs `create_all`? (Default: `create_all`; Alembic only if time permits.)
- [ ] Which public receiver will be used to demo success/failure in the deployed environment? (e.g. a request-bin site, or a tiny mock receiver deployed alongside.)

## 7. Useful Commands

```bash
# Run everything
docker compose up --build

# Tests
pytest -q
pytest tests/test_retry.py -q

# Sample dispatch
curl -i -X POST http://localhost:8000/api/webhooks/dispatch \
  -H "Content-Type: application/json" \
  -d '{"destinationUrl":"https://httpbin.org/post","eventId":"evt_1","payload":{"event":"user.signup","userId":"usr_999"}}'

# Job status
curl http://localhost:8000/api/webhooks/jobs/<job_id>

# Celery worker / beat (local)
celery -A app.workers.celery_app worker --loglevel=info
celery -A app.workers.celery_app beat --loglevel=info
```

## 8. Environment Variables (names only — never put values here)

`DATABASE_URL`, `REDIS_URL`, `HTTP_TIMEOUT_SECONDS`, `MAX_RETRIES`, `BACKOFF_BASE_SECONDS`, `RATE_LIMIT_CAPACITY`, `RATE_LIMIT_REFILL_PER_SECOND`, `EXTRA_RETRYABLE_STATUS_CODES`, `ALLOW_PRIVATE_DESTINATIONS`, `MAX_PAYLOAD_BYTES`, `SWEEPER_INTERVAL_SECONDS`, `STALE_JOB_THRESHOLD_SECONDS`, `LOG_LEVEL`

## 9. Session Log

> Newest first. One entry per session. 3–6 lines.

```
### YYYY-MM-DD HH:MM — <short title>
Done:     …
Decided:  … (add to §4 if durable)
Learned:  … (add to §5 if reusable)
Next:     …
```

### 2026-11-21 — Phase 0 scaffolding
Done:     Repo `webhook-dispatcher/` with full module stubs, Settings, Dockerfile/compose (api, worker, redis, postgres), `/health` probing DB + Redis, 2 smoke tests (`pytest -q` green), all 4 containers healthy, `/health` returns 200.
Decided:  D-15 — pin redis-py >=6.1 for kombu 5.7.
Learned:  Worker crashed with `VersionMismatch` on redis-py 5.3; Docker Desktop must be started manually here.
Next:     First commit (if requested), then Phase 1 — ORM models, schemas, dispatch endpoint.

### 2026-11-21 — Phase 1 core backend
Done:     ORM models + indexes, startup `create_all`, request/response schemas with camelCase aliases, POST `/api/webhooks/dispatch` (commit→enqueue→202), GET `/api/webhooks/jobs/{id}`, 422/404/413/503 handling, 15 tests green (`pytest -q`), ruff + black clean. Live stack: 202, row persisted, worker consumed stub task, `/health` 200.
Decided:  No new durable decisions; kept D-01/D-02/D-14 behaviour.
Learned:  Alias models need `populate_by_name`; HTTPException in a validator yields the intended status; ruff requires `Annotated` for FastAPI dependencies.
Next:     First commit (if requested), then Phase 2 — HTTPX dispatcher, outcome classification, retry/backoff and delivery logs.

### 2026-11-21 — Phase 2 reliability
Done:     HTTPX dispatcher with `X-Webhook-*` headers/no redirects, pure classification + 2/4/8 backoff, real `deliver_webhook` with row lock/attempt counter/terminal guard/per-attempt logs, 61 tests green, ruff/black clean. Live mock: 503, 503, 204 → `success`, countdowns `[2, 4]`, 3 log rows.
Decided:  No new durable decisions; implemented D-02/D-10/D-12 and the `rules.md` §6 attempt ordering.
Learned:  A worker using `with SessionLocal() as db` closes a shared test session; capture the UUID before the task runs. Ruff BLE001 needs a targeted `noqa` for the intentional catch-all.
Next:     First commit (if requested), then Phase 3 — Redis token bucket, sweeper and SSRF guard.

### 2026-11-21 — Phase 3 protection
Done:     Redis Lua token bucket (TIME, atomic), normalized origins, fail-closed deferrals before attempt increment, SSRF validation at API and send time, stale-job sweeper + beat service, 89 tests green, ruff/black clean. Live: 5 services healthy, beat scheduling every 30 s.
Decided:  D-16 — reuse `BACKOFF_BASE_SECONDS` for fail-closed limiter deferral.
Learned:  Redis keys are `rl:<origin>` so test cleanup must match `rl:*test-*`; SQLite yields naive datetimes; host 6379 is already occupied by wslrelay.
Next:     First commit (if requested), then Phase 4 — deployment and final README.

### 2026-11-21 — Phase 4 local finalization
Done:     Added `start.sh` (worker+embedded beat+Uvicorn), `.dockerignore`, lint/format deps, full README and named attempt-log test. 90 tests pass in repo and clean venv; ruff/black clean; Docker rebuild healthy; `/health` 200; smoke dispatch 202 → one attempt log. User requested the first commit: `42e9b81`.
Decided:  D-13 implemented as the free-tier Render strategy; paid Background Worker remains an option.
Learned:  PowerShell inline JSON to `curl.exe` is unreliable here; use `Invoke-RestMethod`. Fresh-copy verification excludes `.env`/caches and installs only from `requirements.txt`.
Next:     User deploys manually and sends the public URL; record it in memory.md and README.

### 2026-11-21 — Phase 5 local verification
Done:     Re-ran 90 tests, ruff/black, Docker stack and `/health`; clean tree at `2575ab8`; no tracked secrets, stray prints or unresolved TODOs. Ticked the first 10 acceptance criteria locally in `prd.md`.
Decided:  No new decisions; deployed scenarios must not be claimed without the live URL.
Learned:  `git remote -v` is empty, so the repo is not public yet; acceptance items 11–12 remain blocked.
Next:     User sends live API URL and public repo URL, then run deployed Phase 5 scenarios and record both URLs.

### (template — copy above)

## 10. Assistant Protocol (for AI coding assistants)

1. Read `memory.md`, then `rules.md`, then the relevant section of `prd.md` / `architecture.md` / `phases.md`.
2. Work on **one task** from the current phase in `phases.md`.
3. If the request conflicts with a decision in §4, **say so** before proceeding; don't silently override.
4. When you make a durable decision or hit a gotcha, add it here.
5. End each session by updating §2 (Status) and §9 (Session Log).
