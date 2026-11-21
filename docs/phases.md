# Phases — Build Plan (48-Hour Budget)

> Work phase by phase. Do **not** start a phase until the previous phase's **Exit Criteria** are met. Tick checkboxes as you go and mirror progress in `memory.md`.

**Time budget (≈ 48h, realistic effort ≈ 20–24h)**

| Phase | Focus | Budget |
|---|---|---|
| 0 | Setup & scaffolding | 1.5 h |
| 1 | Core backend (ingestion + queue) | 4 h |
| 2 | Reliability (delivery, retry, logging) | 6 h |
| 3 | Protection (rate limit, errors, recovery) | 5 h |
| 4 | Testing, Docker, deploy, README | 8 h |
| 5 | Final verification & submission | 1.5 h |
| — | Buffer | remainder |

---

## Phase 0 — Setup & Scaffolding

**Goal:** A running skeleton that boots in Docker.

- [x] Create repo, `.gitignore`, `.env.example`
- [x] Create folder structure from `architecture.md` §4 (empty modules with docstrings)
- [x] `requirements.txt` (fastapi, uvicorn, sqlalchemy, psycopg2-binary, pydantic-settings, celery, redis, httpx, pytest, respx, pytest-httpserver)
- [x] `config.py` with `Settings` (all variables from PRD §10)
- [x] `Dockerfile` + `docker-compose.yml` (api, worker, redis, postgres)
- [x] `GET /health` returns 200
- [x] Copy the 5 project docs into the repo `/docs`
- [x] First commit (`42e9b81` — user explicitly requested it after Phase 4 local work)

**Exit criteria:** `docker compose up` starts all four services; `/health` is green.
Verified locally 2026-11-21: 4 healthy containers, `/health` 200, `pytest -q` 2 passed.

---

## Phase 1 — Core Backend (Ingestion + Queue)

**Goal:** `POST /dispatch` persists a job and enqueues it; returns 202.

- [x] `database/connection.py` (engine, `SessionLocal`, `get_db`)
- [x] ORM models `WebhookJob`, `DeliveryLog` (UUID PKs, indexes on `status`, `event_id`, `(status, updated_at)`, `job_id`)
- [x] Table creation on startup (or Alembic if time permits)
- [x] Pydantic schemas: request (URL validation, `eventId`, `payload` object, size cap) and response
- [x] `celery_app.py` (broker config, `acks_late`, `reject_on_worker_lost`, prefetch = 1)
- [x] Stub task `deliver_webhook(job_id)` that only logs
- [x] `POST /api/webhooks/dispatch`: validate → insert → commit → `delay` → 202
- [x] `jobId` format `job_<uuid>`
- [x] Error handling: 422, 413, 503 for DB/Redis failure
- [x] `GET /api/webhooks/jobs/{job_id}`
- [x] Tests: valid → 202, invalid → 422, persisted + queued (scenarios 1–3)

**Exit criteria:** cURL to `/dispatch` returns 202, a `queued` row exists, the worker log shows the stub task consumed.
Verified locally 2026-11-21: 202 + `queued` row + worker received task; 15 tests pass; ruff/black clean.

---

## Phase 2 — Reliability (Delivery, Retry, Logging)

**Goal:** Real delivery with correct retry semantics and a full audit trail.

- [x] `services/dispatcher.py`: HTTPX client with explicit timeouts, headers (`X-Webhook-*`), no redirects
- [x] Outcome classification (pure function): success / retryable / permanent
- [x] `services/retry.py`: `backoff_seconds`, `should_retry` (pure)
- [x] Implement `deliver_webhook` per `architecture.md` §6 (terminal-state guard, row lock, attempt counter, status transitions)
- [x] Reschedule via `apply_async(countdown=…)` — **not** `self.retry`
- [x] Write one `delivery_logs` row per attempt, same transaction as status update
- [x] Never leave a job in `processing` on unexpected exceptions (catch → log → `failed`)
- [x] Tests: success delivery (4), 500 retry (5), 429 retry (6), timeout retry (7), backoff 2/4/8 (8), failed after exhaustion (9), all attempts logged (10), 400/404 not retried

**Exit criteria:** against a mock server that fails twice then succeeds, the job ends `success` with 3 log rows and observed countdowns 2 s and 4 s. A permanently failing mock ends `failed` after 4 attempts (initial + 3 retries).
Verified locally 2026-11-21: mock returned 503, 503, 204 → `success`, 3 log rows, countdowns `[2, 4]`; permanent 500 → `failed` after 4 attempts; 61 tests pass; ruff/black clean.

---

## Phase 3 — Protection (Rate Limit, Errors, Recovery)

**Goal:** Protect destinations and survive infrastructure failures.

- [x] `services/rate_limiter.py`: Redis token bucket via Lua (`TIME`, atomic); `acquire(origin) -> (allowed, wait)`
- [x] Origin normalization (`scheme://host:port`, lowercase host, default ports)
- [x] Integrate into worker **before** the attempt counter increments; deferral = no attempt, no failure log
- [x] Fail-closed behavior when Redis is unavailable in the limiter
- [x] Sweeper task + Celery beat schedule; stale `queued`/`retrying`/`processing` recovery
- [x] SSRF guard + `ALLOW_PRIVATE_DESTINATIONS` flag
- [x] Payload size and URL edge-case handling
- [x] Structured logging
- [x] Tests: limiter blocks excess (11), deferral consumes no attempt, slow destination doesn't block API (12), restart/sweeper recovery (13), Redis-down at enqueue → 503 and job recoverable
- [ ] *(Stretch)* per-destination concurrency cap; `Retry-After` honoring (capped)

**Exit criteria:** flooding one destination with N > capacity jobs delivers them gradually with none lost; killing the worker mid-run and restarting it still completes every job.
Verified locally 2026-11-21: real Redis token bucket blocks the 3rd token with a positive wait; a deferred job stayed `queued` with 0 attempts and 0 logs; stale `retrying` job was re-enqueued by `sweep_stale_jobs`; SSRF cases return 422; 89 tests pass, ruff/black clean; 5 compose services healthy with beat scheduling every 30 s.

---

## Phase 4 — Finalization (Tests, Docker, Deploy, README)

**Goal:** Everything reproducible, deployed and documented.

### 4a. Tests & quality
- [x] All 13 PRD scenarios green; `pytest -q` passes from a clean checkout
- [x] Test instructions verified in a fresh clone
- [x] Lint/format pass (ruff/black — see `rules.md`)

### 4b. Docker
- [x] Multi-stage / slim Dockerfile, non-root user
- [x] Compose healthchecks (`depends_on: condition: service_healthy`)
- [x] `.env.example` complete

### 4c. Deployment (Render)
- [ ] Provision PostgreSQL and Redis (Key Value)
- [ ] Deploy API web service
- [x] Worker strategy decided & implemented (separate worker if paid; otherwise `start.sh` running worker + beat alongside uvicorn)
- [ ] Set env vars; confirm API and worker point at the same Redis and Postgres
- [ ] Public URL recorded in `memory.md` and README

### 4d. README
- [x] Overview, architecture diagram, tech choices
- [x] Local setup, env var table
- [x] API docs + sample cURL (dispatch, status, health)
- [x] Testing instructions
- [x] Explanations: background processing, retries/backoff, rate limiting, recovery, at-least-once
- [x] Known limitations (free-tier spin-down, duplicates on 503 retry)
- [ ] Deployment URL

Verified locally 2026-11-21: **90 tests pass** in the repo and in a clean venv copy installed from `requirements.txt`; ruff/black clean; Docker rebuild healthy with 5 services; `/health` returns `{"status":"ok"}`; `start.sh` executable and syntax-valid; live dispatch returned 202 and produced one attempt log. Committed as `42e9b81`. User will deploy manually and send the public URL to record here.

**Exit criteria:** a stranger can clone, `docker compose up`, run the cURL examples and `pytest` without asking questions.

---

## Phase 5 — Final Verification & Submission

Run these **against the deployed URL**:

- [ ] `POST /dispatch` to a healthy test receiver (e.g. a request-bin style endpoint) → 202, then status becomes `success`
- [ ] Destination that returns 500 → observe `retrying` → `failed` after 4 attempts, 4 log rows, ~2/4/8 s spacing
- [ ] Destination returning 429 → retried
- [ ] Destination returning 404 → `failed` after 1 attempt
- [ ] Burst of requests to one destination → throttled, none lost
- [ ] Invalid payload → 422
- [ ] Walk through the **Acceptance Criteria** in `prd.md` §14 — every box ticked
- [ ] Repo is public, no secrets committed, README links the live URL
- [ ] Final `memory.md` update (local status recorded; live URL/repo URL fields still pending)

Local submission checks 2026-11-21: **90 tests pass**, ruff/black clean, 5 compose services healthy, `/health` 200, clean working tree at `2575ab8`, no tracked secrets/stray `print`s/unresolved `TODO`s. Deployed-URL scenarios, public repo and live URL remain unchecked pending user-provided URLs.

---

## Definition of Done (global)

A phase is done only when: code merged, tests for that phase pass, no `TODO`s without a linked note in `memory.md`, docs updated if behavior changed, and the exit criteria above are demonstrated (not assumed).

## Cut Line (if time runs short)

Keep: ingestion, delivery, retries + backoff, logging, token-bucket limiter, sweeper, core tests, deployment, README.
Cut first: concurrency cap, `Retry-After`, jitter, Alembic, webhook signing, advanced observability.
