# Distributed Webhook Dispatcher Engine

A FastAPI service that accepts webhook events, persists them to PostgreSQL, queues delivery through
Redis/Celery, and delivers them with retries, per-destination rate limiting and a full attempt
audit log.

## Status

Phases 0–3 are implemented and verified locally; Phase 4 local finalization is complete.
Deployment URL: _pending — Render deploy requires account credentials and a repository remote_.

## Architecture

```text
Client ──POST /dispatch──▶ FastAPI ──commit──▶ PostgreSQL (source of truth)
                             │                    ▲
                             └─enqueue──▶ Redis ──┘
                                  │
                                  ▼
                         Celery worker ──token bucket──▶ Redis
                                  │
                                  ▼
                            HTTPX POST ──▶ destination
                                  │
                                  └─delivery_logs + status──▶ PostgreSQL

Celery beat ──sweep_stale_jobs──▶ PostgreSQL ──re-enqueue──▶ Redis
```

The API never calls a destination. It validates, commits, enqueues and returns `202`. PostgreSQL is
authoritative; Redis is disposable transport plus limiter state.

### Technology choices

- **FastAPI** — synchronous route handlers, explicit Pydantic schemas.
- **PostgreSQL + SQLAlchemy** — durable jobs, indexes, row locks, attempt history.
- **Redis + Celery** — broker, delayed retries, atomic Lua token bucket.
- **HTTPX** — explicit timeouts, no redirects, classified outcomes.
- **Celery beat** — stale-job sweeper.

## Quick start

```bash
cp .env.example .env
docker compose up --build -d
curl http://localhost:8000/health
```

Compose starts five services: `api`, `worker`, `beat`, `postgres` and `redis`. The local defaults in
`.env.example` use Docker hostnames; outside Docker, point them at `localhost`.

## API

- `GET /health` — probes PostgreSQL and Redis; `200` when both answer.
- `POST /api/webhooks/dispatch` — validate → persist → enqueue → `202`.
- `GET /api/webhooks/jobs/{jobId}` — job state and ordered attempts (`job_<uuid>` ids).

```bash
curl -i http://localhost:8000/health

curl -i -X POST http://localhost:8000/api/webhooks/dispatch \
  -H "Content-Type: application/json" \
  -d '{"destinationUrl":"https://example.com/hooks","eventId":"evt_1","payload":{"event":"user.signup"}}'

curl http://localhost:8000/api/webhooks/jobs/job_<uuid>
```

A successful dispatch returns:

```json
{"status": "queued", "jobId": "job_0e85b05a-ed89-45d1-bff3-16659afb60f4"}
```

## How it works

- **Background processing:** the request handler only commits and enqueues. The worker reloads the
  job by ID, locks the row, checks the rate limit, then increments the attempt counter.
- **Retries:** `2xx` succeeds; `429`, `5xx`, timeouts and transport errors retry at `2 s → 4 s → 8 s`.
  Other `4xx` and `3xx` fail immediately. Max 3 retries / 4 attempts.
- **Rate limiting:** one atomic Redis Lua token bucket per normalized origin
  (`scheme://host:port`). A limited job returns to `queued` and is rescheduled with the wait time;
  no attempt is logged and no retry is consumed. Redis errors fail closed.
- **Recovery:** Celery beat runs `sweep_stale_jobs` every `SWEEPER_INTERVAL_SECONDS`. Jobs in
  `queued`, `retrying` or `processing` older than `STALE_JOB_THRESHOLD_SECONDS` are re-enqueued from
  PostgreSQL. Terminal jobs are untouched.
- **At-least-once delivery:** a crash after the HTTP POST can cause a duplicate. The
  `X-Webhook-Event-Id` header lets receivers deduplicate. `eventId` is indexed but not unique.
- **SSRF safety:** non-http(s), private, loopback, link-local and metadata destinations are rejected
  at ingestion and rechecked before send, unless `ALLOW_PRIVATE_DESTINATIONS=true`.

## Configuration

| Variable | Default | Purpose |
|---|---|---|
| `DATABASE_URL` | required | SQLAlchemy PostgreSQL URL (`postgres://` is rewritten automatically). |
| `REDIS_URL` | required | Celery broker and token-bucket Redis URL. |
| `HTTP_TIMEOUT_SECONDS` | `10` | Read timeout for outbound HTTPX calls. |
| `MAX_RETRIES` | `3` | Retries after the initial attempt. |
| `BACKOFF_BASE_SECONDS` | `2` | Base for `2/4/8` backoff and fail-closed limiter deferral. |
| `RATE_LIMIT_CAPACITY` | `5` | Token-bucket capacity per origin. |
| `RATE_LIMIT_REFILL_PER_SECOND` | `1` | Token refill rate. |
| `EXTRA_RETRYABLE_STATUS_CODES` | empty | Comma-separated extra retryable HTTP codes. |
| `ALLOW_PRIVATE_DESTINATIONS` | `false` | Disable SSRF IP blocking (useful for local mock receivers). |
| `MAX_PAYLOAD_BYTES` | `262144` | Ingestion payload cap; larger bodies return `413`. |
| `SWEEPER_INTERVAL_SECONDS` | `30` | Celery beat interval for stale-job recovery. |
| `STALE_JOB_THRESHOLD_SECONDS` | `120` | Age after which a non-terminal job is considered stale. |
| `LOG_LEVEL` | `INFO` | Python logging level. |

## Testing

```bash
python -m pip install -r requirements.txt
python -m pytest -q
ruff check app tests
black --check --line-length 100 app tests
```

`tests/test_rate_limiter.py` uses a real Redis instance when one is reachable and skips those two
integration tests otherwise. All HTTP delivery tests use `respx`; no test sleeps for backoff.

| PRD scenario | Test |
|---|---|
| 1 — valid request → 202 | `tests/test_api.py::test_valid_request_returns_202` |
| 2 — invalid request → 422 | `tests/test_api.py::test_invalid_request_returns_422` |
| 3 — persisted and queued | `tests/test_api.py::test_event_is_persisted_before_enqueue` |
| 4 — successful delivery | `tests/test_worker.py::test_successful_delivery_marks_job_success_and_logs_attempt` |
| 5 — 500 retries | `tests/test_worker.py::test_500_triggers_retry_with_2s_countdown` |
| 6 — 429 retries | `tests/test_worker.py::test_429_triggers_retry` |
| 7 — timeout retries | `tests/test_worker.py::test_timeout_triggers_retry_with_null_response_code` |
| 8 — exact 2/4/8 backoff | `tests/test_retry.py::test_backoff_is_2_4_8_seconds` |
| 9 — failed after exhaustion | `tests/test_worker.py::test_permanent_failure_exhausts_after_four_attempts` |
| 10 — every attempt logged | `tests/test_worker.py::test_every_attempt_is_logged` |
| 11 — limiter blocks excess | `tests/test_rate_limiter.py::test_token_bucket_blocks_excess_and_returns_wait` |
| 12 — slow destination does not block API | `tests/test_api.py::test_slow_destination_does_not_block_dispatch` |
| 13 — worker restart recovery | `tests/test_worker.py::test_sweeper_requeues_stale_job` |
| + — 400/404 not retried | `tests/test_worker.py::test_permanent_statuses_are_not_retried` |
| + — deferral consumes no attempt | `tests/test_worker.py::test_rate_limit_deferral_consumes_no_attempt` |

## Deployment (Render)

Free-tier Render has no dedicated Background Worker, so `start.sh` runs the Celery worker with an
embedded beat (`-B`) alongside Uvicorn in the same container.

1. Provision **PostgreSQL** and **Redis / Key Value** on Render.
2. Create a **Web Service** from this repo using the provided `Dockerfile`.
3. Set the start command to `./start.sh` (it respects Render's `$PORT`).
4. Set env vars: internal `DATABASE_URL` and `REDIS_URL`, plus any tuning values from the table
   above. Set `ALLOW_PRIVATE_DESTINATIONS=false` unless deliberately testing a local receiver.
5. Verify `GET /health`, submit a dispatch, poll job status, and confirm `delivery_logs` rows.
6. Record the public URL here and in `docs/memory.md`: _pending_.

If a paid Background Worker is available, run the worker and beat as separate services instead and
drop `start.sh`.

## Known limitations

- At-least-once delivery can duplicate events; receivers should deduplicate on `X-Webhook-Event-Id`.
- A client retry after an enqueue `503` can create a duplicate job (the first one stays `queued`).
- Free-tier web services spin down; delayed in-memory tasks can be lost, but the sweeper recovers
  from PostgreSQL when the service wakes.
- Schema changes use `create_all`; there are no Alembic migrations.
- Not implemented (stretch): per-destination concurrency cap, `Retry-After` honoring, jitter,
  webhook signing.

## Documentation

- [PRD — what we build](docs/prd.md)
- [Architecture — how we build it](docs/architecture.md)
- [Phases — build order](docs/phases.md)
- [Rules — engineering conventions](docs/rules.md)
- [Memory — status and decision log](docs/memory.md)
