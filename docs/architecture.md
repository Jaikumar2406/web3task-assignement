# Architecture — Distributed Webhook Dispatcher Engine

> Companion to `prd.md`. The PRD says *what*; this document says *how*.

---

## 1. Architectural Style

**Producer → Queue → Consumer**, with PostgreSQL as the **source of truth** and Redis as a **transport / coordination layer**.

Key principle: **the database is authoritative, the queue is disposable.** If Redis loses a message, the DB still knows the job exists and the sweeper re-enqueues it.

## 2. System Diagram

```
                    ┌──────────────────────────────┐
  Client ──POST──►  │   FastAPI (API service)      │
                    │   1. validate                │
                    │   2. INSERT job (queued)     │──────┐
                    │   3. enqueue to Celery       │──┐   │
                    │   4. return 202              │  │   │
                    └──────────────────────────────┘  │   │
                                                      ▼   ▼
                                   ┌──────────────┐  ┌──────────────────┐
                                   │    Redis     │  │   PostgreSQL     │
                                   │ - Celery     │  │ - webhook_jobs   │
                                   │   broker     │  │ - delivery_logs  │
                                   │ - rate       │  └──────────────────┘
                                   │   buckets    │          ▲
                                   └──────┬───────┘          │
                                          │ consume          │ read/update
                                          ▼                  │
                    ┌─────────────────────────────────────────┴─┐
                    │ Celery Worker  (+ Beat sweeper)           │
                    │  a. load job, guard terminal states       │
                    │  b. rate-limit check (Redis Lua)          │
                    │  c. HTTPX POST → destination              │──► Destination server
                    │  d. classify → log → update → reschedule  │
                    └───────────────────────────────────────────┘
```

## 3. Components

| Component | Responsibility | Never does |
|---|---|---|
| **API (FastAPI)** | Validate, persist, enqueue, expose job status | Call destinations |
| **Redis** | Celery broker; token-bucket state | Store durable job data |
| **Celery worker** | Deliver webhooks, classify results, log attempts, schedule retries | Serve HTTP |
| **Celery beat (sweeper)** | Recover stale non-terminal jobs | Deliver webhooks directly |
| **PostgreSQL** | Durable jobs and attempt history | — |
| **Rate limiter** | Atomic per-destination token bucket | Block (it only answers allow / wait-time) |

## 4. Module Layout

```
webhook-dispatcher/
├── app/
│   ├── main.py                 # FastAPI app factory, lifespan, exception handlers
│   ├── config.py               # pydantic-settings; single Settings object
│   ├── api/routes.py           # POST /dispatch, GET /jobs/{id}, GET /health
│   ├── models/
│   │   ├── webhook.py          # WebhookJob ORM
│   │   └── delivery_log.py     # DeliveryLog ORM
│   ├── schemas/webhook.py      # Pydantic request/response models
│   ├── services/
│   │   ├── dispatcher.py       # HTTPX send + outcome classification
│   │   ├── retry.py            # pure backoff / retry-decision functions
│   │   └── rate_limiter.py     # Redis token bucket (Lua)
│   ├── workers/
│   │   ├── celery_app.py       # Celery config, beat schedule
│   │   └── tasks.py            # deliver_webhook, sweep_stale_jobs
│   └── database/connection.py  # engine, SessionLocal, get_db
├── tests/ (test_api, test_worker, test_retry, test_rate_limiter, conftest, mock servers)
├── Dockerfile, docker-compose.yml, requirements.txt, .env.example, .gitignore, README.md
└── docs: prd.md, architecture.md, phases.md, memory.md, rules.md
```

Layering rule: `api` and `workers` depend on `services`; `services` depend on `models`/`config`; `services/retry.py` and the classification logic are **pure functions** (no I/O) so they are trivially unit-testable.

## 5. Request Lifecycle (ingestion)

```
Client            API                    Postgres           Redis
  │  POST /dispatch │                        │                 │
  │────────────────►│ validate (Pydantic)    │                 │
  │                 │ INSERT job(queued) ───►│ COMMIT          │
  │                 │ deliver_webhook.delay ───────────────────►│
  │                 │   (on failure: log, 503, job stays queued)│
  │◄────────────────│ 202 {status, jobId}    │                 │
```

Order matters: **commit first, enqueue second.** Enqueueing first risks a worker seeing a job that doesn't exist yet. If enqueue fails after commit, the job is safe in the DB; return `503` so the client knows to retry, and the sweeper will also recover it.

> Decision: the client retrying after a 503 can create a duplicate job. Acceptable under at-least-once semantics; document it.

## 6. Worker Algorithm (`deliver_webhook(job_id)`)

Celery settings: `acks_late=True`, `reject_on_worker_lost=True`, `worker_prefetch_multiplier=1`, results backend disabled, `task_time_limit` > HTTP timeout.

**Retries are managed by us, not Celery's `self.retry`.** `attempt_count` in the DB is the single counter. Reschedules use `deliver_webhook.apply_async(args=[job_id], countdown=delay)`. This keeps rate-limit deferrals (which must not consume retries) and real retries under one consistent model.

```python
def deliver_webhook(job_id: str):
    with SessionLocal() as db:
        job = db.get(WebhookJob, job_id, with_for_update=True)
        if job is None or job.status in TERMINAL:          # success / failed
            return                                         # idempotent no-op

        # 1. Rate limit BEFORE consuming an attempt
        allowed, wait = rate_limiter.acquire(origin_key(job.destination_url))
        if not allowed:
            job.status = "queued"; touch(job); db.commit()
            deliver_webhook.apply_async(args=[job_id], countdown=wait + jitter())
            return

        # 2. Begin attempt
        job.attempt_count += 1
        n = job.attempt_count
        job.status = "processing"; db.commit()

        # 3. Send
        outcome = dispatcher.send(job, attempt=n)          # returns Outcome

        # 4. Decide
        if outcome.kind == SUCCESS:
            log(db, job, n, "success", outcome); job.status = "success"
        elif outcome.kind == RETRYABLE and n <= settings.MAX_RETRIES:
            delay = retry.backoff_seconds(n)               # 2, 4, 8
            log(db, job, n, "retrying", outcome); job.status = "retrying"
            db.commit()
            deliver_webhook.apply_async(args=[job_id], countdown=delay)
            return
        else:                                              # PERMANENT or exhausted
            log(db, job, n, "failed", outcome); job.status = "failed"
        db.commit()
```

Commit the log row and job status **in the same transaction**.

### 6.1 Outcome classification (`services/dispatcher.py`, pure)

| Result | Class | Job effect |
|---|---|---|
| 2xx | `SUCCESS` | `success` |
| 429, 5xx | `RETRYABLE` | retry if budget remains |
| `httpx.TimeoutException` | `RETRYABLE` | retry (response_code = null) |
| `httpx.TransportError` (connect, DNS, reset) | `RETRYABLE` | retry |
| 3xx | `PERMANENT` | `failed` (redirects not followed) |
| other 4xx (400, 404, …) | `PERMANENT` | `failed` immediately |
| status in `EXTRA_RETRYABLE_STATUS_CODES` | `RETRYABLE` | retry |

HTTPX client: explicit `httpx.Timeout(connect=5, read=HTTP_TIMEOUT, write=5, pool=5)`, `follow_redirects=False`, truncate stored error messages (≤ 1000 chars) and never store response bodies containing secrets without truncation.

### 6.2 Backoff (`services/retry.py`, pure)

```python
def backoff_seconds(failed_attempt: int, base: int = 2) -> int:
    return base * 2 ** (failed_attempt - 1)     # 1→2, 2→4, 3→8

def should_retry(failed_attempt: int, max_retries: int = 3) -> bool:
    return failed_attempt <= max_retries         # attempts 1..3 retry; attempt 4 is final
```

Optional: add ±10% jitter in production; keep it **off by default** (or injectable) so tests assert exact 2/4/8.

## 7. Rate Limiter Design

**Token bucket in Redis, atomic via Lua.** One bucket per destination origin: key `rl:{scheme}://{host}:{port}`.

```lua
-- KEYS[1]=bucket  ARGV[1]=capacity  ARGV[2]=refill_per_sec
local t   = redis.call('TIME')
local now = tonumber(t[1]) + tonumber(t[2]) / 1e6
local cap, rate = tonumber(ARGV[1]), tonumber(ARGV[2])

local d = redis.call('HMGET', KEYS[1], 'tokens', 'ts')
local tokens, ts = tonumber(d[1]), tonumber(d[2])
if tokens == nil then tokens, ts = cap, now end

tokens = math.min(cap, tokens + (now - ts) * rate)

local allowed, wait = 0, 0
if tokens >= 1 then tokens = tokens - 1; allowed = 1
else wait = (1 - tokens) / rate end

redis.call('HSET', KEYS[1], 'tokens', tokens, 'ts', now)
redis.call('EXPIRE', KEYS[1], math.ceil(cap / rate) * 2)
return {allowed, tostring(wait)}     -- tostring: Lua floats are truncated on return
```

Why this design:
- **Atomic** — no race between concurrent workers.
- **Redis `TIME`** — clock source is shared, not per-worker.
- **Distributed** — works across any number of workers.
- **Rate-limited jobs are rescheduled** (`apply_async(countdown=wait)`), never dropped, never counted as attempts.
- Keys auto-expire, so idle destinations cost nothing.

*Stretch (NFR-04 "concurrent"):* a per-destination Redis semaphore (`INCR`/`DECR` with TTL) capping in-flight requests.

If Redis is down when the limiter is called: **fail closed** — reschedule the job (short countdown) rather than send unthrottled. Document this choice.

## 8. Reliability and Recovery

| Failure | Behavior |
|---|---|
| Worker crashes mid-task | `acks_late` + `reject_on_worker_lost` → broker redelivers; sweeper is a second safety net |
| Worker restarted while retries are pending | Delayed tasks may be lost with the worker's memory → sweeper re-enqueues stale `retrying` jobs |
| Redis down at ingestion | Job is already committed; API returns 503; sweeper recovers it |
| Redis message lost | Sweeper recovers from DB |
| DB down at ingestion | 503, nothing accepted |
| DB down in worker | Task raises → Celery redelivers (acks_late); no state change |
| Destination slow | HTTPX timeout → retryable; API unaffected (separate process) |
| Duplicate task delivery | Terminal-state guard + row lock make re-execution safe; destination may still see a duplicate → at-least-once, `X-Webhook-Event-Id` for dedupe |

**Sweeper (`sweep_stale_jobs`, beat every `SWEEPER_INTERVAL_SECONDS`):**
```
SELECT id FROM webhook_jobs
 WHERE status IN ('queued','retrying','processing')
   AND updated_at < now() - STALE_JOB_THRESHOLD
 FOR UPDATE SKIP LOCKED LIMIT 100;
→ if attempt_count > MAX_RETRIES + 1 : mark failed
→ else: touch updated_at, apply_async(deliver_webhook)
```
`STALE_JOB_THRESHOLD_SECONDS` (120) must comfortably exceed the longest normal delay (8 s backoff + rate-limit wait + HTTP timeout) to avoid unnecessary duplicates.

Redis broker transport option: `visibility_timeout` must be greater than the longest ETA/countdown used (default 1 h is fine).

## 9. Error Handling Matrix

| Case | Where | Handling |
|---|---|---|
| Invalid JSON / schema | API | `422` with field errors |
| Invalid / disallowed URL (scheme, private IP when disallowed) | API | `422` |
| Payload too large | API | `413` |
| DB failure | API | rollback, `503` |
| Redis enqueue failure | API | log, `503`, job remains `queued` |
| Network failure / DNS | Worker | `RETRYABLE`, log error message |
| Connection/read timeout | Worker | `RETRYABLE`, `response_code = NULL` |
| 429 / 5xx | Worker | `RETRYABLE` |
| Other 4xx / 3xx | Worker | `PERMANENT` → `failed` |
| Unexpected exception in task | Worker | log attempt as failed with error, mark `failed`; never leave a job stuck in `processing` |
| Redis down in limiter | Worker | fail closed → reschedule |

## 10. Deployment Architecture

**Local:** `docker-compose.yml` runs `api`, `worker` (with `-B` or a separate `beat`), `redis`, `postgres`. API and worker share one image with different commands.

```
api:     uvicorn app.main:app --host 0.0.0.0 --port 8000
worker:  celery -A app.workers.celery_app worker --loglevel=info --concurrency=4
beat:    celery -A app.workers.celery_app beat --loglevel=info      # or worker -B
```

**Render (free tier considerations — verify current pricing before relying on this):**
- Web Service (API): free tier available.
- PostgreSQL: free tier exists but with limits/expiry — note in README.
- Redis ("Key Value"): free tier available.
- **Celery worker as a Render "Background Worker" is generally a paid service type.** Free-tier workaround: run the worker in the same container as the API via a `start.sh` that launches `celery ... worker -B --pool=solo`/`--concurrency=2 &` then `exec uvicorn`. This still satisfies "API and worker share the same Redis and PostgreSQL."
- Free web services spin down when idle; the worker dies with them. The sweeper + DB-as-truth design means pending jobs resume when the service wakes. Mention this limitation in the README.
- If a paid Background Worker is acceptable, use the separate-service topology instead and delete the workaround.
- Connection strings: use Render's internal URLs for `DATABASE_URL` / `REDIS_URL`; convert `postgres://` → `postgresql+psycopg2://` for SQLAlchemy.

## 11. Observability

- Structured logs with `job_id`, `event_id`, `attempt`, `destination_origin`, `outcome`, `latency_ms`.
- `delivery_logs` table is the audit trail.
- `GET /health` for liveness/dependency checks.
- Never log full payloads at INFO level.

## 12. Security Notes

- **SSRF:** validate scheme (`http`/`https` only); resolve and reject private, loopback, link-local and metadata addresses unless `ALLOW_PRIVATE_DESTINATIONS=true`. Re-check at send time (DNS can change).
- No redirects followed.
- Secrets only via environment variables; `.env` never committed.
- Request size limits.
- Containers run as non-root.

## 13. Testing Architecture

| Layer | Tooling |
|---|---|
| API | `TestClient`, dependency-override DB session, Celery `delay` mocked or `task_always_eager` |
| Worker logic | Call task function directly with a test DB; `respx` to mock HTTPX; patch `apply_async` to assert `countdown` |
| Retry / backoff | Pure-function unit tests + task-level assertions on `countdown == 2, 4, 8` (no real sleeping) |
| Rate limiter | Real Redis (compose service / CI service container) for Lua; monkeypatch time or use small refill rates |
| Slow destination | `pytest-httpserver` (or a tiny local server) with a delay; assert API latency stays low |
| Restart safety | Insert a stale `queued` job, run `sweep_stale_jobs`, assert it is enqueued and delivered |
| DB in tests | Postgres via compose for integration; for unit tests use `JSON().with_variant(JSONB, "postgresql")` so SQLite also works |

## 14. Key Trade-offs

| Decision | Alternative | Why this one |
|---|---|---|
| DB as source of truth + sweeper | Trust broker only | Meets "no silent loss" despite Redis volatility |
| Own attempt counter + `apply_async(countdown)` | Celery `self.retry` | Rate-limit deferrals must not burn retries |
| Token bucket in Lua | Sliding window | Burst tolerance, O(1) state, atomic |
| Sync SQLAlchemy + `def` routes | Async stack | Simpler with Celery; FastAPI threadpool is sufficient |
| At-least-once | Exactly-once | Exactly-once over HTTP is impractical; expose idempotency headers |
| Worker in same container (free tier) | Separate worker service | Cost; documented limitation |
