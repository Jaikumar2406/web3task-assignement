# PRD — Distributed Webhook Dispatcher Engine

| | |
|---|---|
| **Project type** | Backend Engineering / Distributed Systems |
| **Context** | Backend Developer Intern take-home |
| **Deadline** | 48 hours |
| **Primary goal** | A reliable, asynchronous webhook delivery system |
| **Doc status** | Source of truth for *what* we build. See `architecture.md` for *how*. |

---

## 1. Overview

The Distributed Webhook Dispatcher Engine accepts webhook events over an HTTP API, persists them, queues them in a background queue, and reliably delivers them to external destination servers.

It must provide:

- Asynchronous, non-blocking event ingestion
- Automatic retries with exponential backoff
- Per-destination rate limiting
- Full delivery-attempt tracking and logging
- Continued operation while destinations are down or slow

## 2. Problem Statement

Services frequently need to notify third-party systems (user signed up, order placed, payment completed, account updated). External servers may be unavailable, return 5xx, return 429, respond slowly, or fail intermittently.

Temporary failures must **not** cause event loss and must **not** block the caller's API request.

## 3. Goals and Non-Goals

### Goals
- Accept events fast (HTTP 202) and deliver them in the background.
- Never lose a persisted job across worker restarts.
- Retry transient failures per a fixed, testable schedule.
- Protect each destination from being flooded.
- Record every delivery attempt.
- Deploy publicly with API, worker, Redis and PostgreSQL.

### Non-Goals (out of scope)
- Authentication / API keys / multi-tenancy
- Webhook signing (HMAC) — *optional stretch only*
- Dashboard / UI
- Exactly-once delivery (we guarantee **at-least-once**)
- Per-destination custom configuration stored in DB (limits are global config)
- Dead-letter queue UI (failed jobs remain queryable in the DB)

## 4. Technology Stack

| Layer | Choice |
|---|---|
| Language / API | Python 3.11+, FastAPI |
| Queue / Broker | Redis |
| Background workers | Celery |
| Database | PostgreSQL + SQLAlchemy ORM |
| Outbound HTTP | HTTPX |
| Infrastructure | Docker, Docker Compose |
| Hosting | Render (free tier where available) |
| Testing | Pytest, HTTPX TestClient (+ respx / pytest-httpserver for mock destinations) |

## 5. High-Level Workflow

1. Client sends `POST /api/webhooks/dispatch`.
2. FastAPI validates the JSON payload.
3. Event is saved in PostgreSQL (`status = queued`).
4. A Celery job is published to Redis.
5. API immediately returns `202 Accepted`.
6. A Celery worker picks up the job.
7. Worker acquires a rate-limit token for the destination (or reschedules).
8. Worker sends `POST` to the destination URL with the original payload.
9. Success → `success`. Retryable failure → `retrying` and rescheduled. Permanent failure or retries exhausted → `failed`.
10. Every attempt is recorded in `delivery_logs`.

## 6. Functional Requirements

### FR-01 — Webhook Ingestion API

`POST /api/webhooks/dispatch`

**Request**
```json
{
  "destinationUrl": "https://target-server.com/endpoint",
  "eventId": "evt_123456",
  "payload": { "event": "user.signup", "userId": "usr_999" }
}
```

**Requirements**
- Accept JSON only.
- Validate `destinationUrl` (valid `http`/`https` URL), `eventId` (non-empty string, ≤ 255 chars), `payload` (JSON object, size ≤ `MAX_PAYLOAD_BYTES`).
- Generate a unique job ID.
- **Persist before acknowledging.**
- Enqueue the background job.
- Never wait for the destination.

**Success response — `202 Accepted`**
```json
{ "status": "queued", "jobId": "job_123456" }
```

**Error responses**

| Condition | Status |
|---|---|
| Malformed JSON / schema violation / invalid URL | `422 Unprocessable Entity` |
| Payload too large | `413 Payload Too Large` |
| Database unavailable | `503 Service Unavailable` |
| Redis unavailable (job already persisted) | `503 Service Unavailable` — job is kept in DB and recovered by the sweeper (see FR-06) |

### FR-02 — Background Worker

The Celery worker must:
- Fetch pending jobs from Redis.
- Read the event and destination URL from PostgreSQL.
- Send `HTTP POST` with the original payload as the JSON body.
- Capture the destination's response (status code, error).
- Update job status and write a delivery log row.

Outbound request headers (for receiver idempotency):
`Content-Type: application/json`, `X-Webhook-Job-Id`, `X-Webhook-Event-Id`, `X-Webhook-Attempt`.

### FR-03 — Automatic Retry with Exponential Backoff

**Retryable:** HTTP 5xx, HTTP 429, request timeout, connection/transport errors.
**Not retryable:** other 4xx (e.g. 400, 404), 3xx (redirects are not followed). A configurable `EXTRA_RETRYABLE_STATUS_CODES` list allows opting in more codes (e.g. 408).

| Attempt | Wait before attempt |
|---|---|
| Initial attempt (#1) | Immediately |
| Retry 1 (#2) | 2 s |
| Retry 2 (#3) | 4 s |
| Retry 3 (#4) | 8 s |

Formula: `delay = BACKOFF_BASE_SECONDS * 2^(n-1)` where `n` is the attempt that just failed.
Maximum **3 retries** (4 total attempts). After the last failure the job becomes `failed`.

### FR-04 — Delivery Logging

One `delivery_logs` row per attempt, containing: job ID, attempt number, status, response code, error message, timestamp. Event ID and destination URL are obtained by joining `webhook_jobs` (single source of truth, no duplication).

**Job statuses:** `queued`, `processing`, `retrying`, `success`, `failed`
**Attempt-log statuses:** `success`, `retrying` (attempt failed, another is scheduled), `failed` (attempt failed, no more attempts)

### FR-05 — Per-Destination Rate Limiting

- Algorithm: **Token Bucket** (Redis + Lua for atomicity).
- Key = normalized destination origin (`scheme://host:port`).
- Each destination has its own bucket.
- When empty: **reschedule** the job after the computed wait; never drop it.
- A rate-limit deferral **does not consume a retry** and is **not** a delivery attempt.
- Threshold is configurable (`RATE_LIMIT_CAPACITY`, `RATE_LIMIT_REFILL_PER_SECOND`).

### FR-06 — Recovery of Persisted Jobs (reliability)

A Celery beat "sweeper" task periodically re-enqueues jobs that are in `queued`, `retrying` or `processing` and have not been updated for `STALE_JOB_THRESHOLD_SECONDS`. This covers: Redis outage at enqueue time, worker crash mid-task, lost broker messages. Terminal jobs (`success`, `failed`) are never touched.

### FR-07 — Job Status Endpoint (recommended)

`GET /api/webhooks/jobs/{job_id}`

```json
{
  "jobId": "job_123456",
  "eventId": "evt_123456",
  "destinationUrl": "https://target-server.com/endpoint",
  "status": "success",
  "attemptCount": 2,
  "createdAt": "2026-01-01T10:00:00Z",
  "updatedAt": "2026-01-01T10:00:03Z",
  "attempts": [
    { "attemptNumber": 1, "status": "retrying", "responseCode": 503, "errorMessage": null, "attemptedAt": "..." },
    { "attemptNumber": 2, "status": "success", "responseCode": 200, "errorMessage": null, "attemptedAt": "..." }
  ]
}
```
Returns `404` for unknown IDs, `422` for malformed IDs.

### FR-08 — Health Endpoint (small extra)

`GET /health` → `200 {"status":"ok"}`; checks DB and Redis connectivity. Used by Render health checks.

## 7. Data Model

### `webhook_jobs`
| Column | Type | Notes |
|---|---|---|
| id | UUID (PK) | Exposed to clients as `job_<uuid>` |
| event_id | String(255) | Indexed (not unique — see Decision D-04) |
| destination_url | Text | |
| payload | JSONB | |
| status | String | One of the 5 job statuses; indexed |
| attempt_count | Integer | Default 0 |
| created_at | Timestamp (tz) | |
| updated_at | Timestamp (tz) | Used by the sweeper; indexed with status |

### `delivery_logs`
| Column | Type | Notes |
|---|---|---|
| id | UUID (PK) | |
| job_id | UUID (FK → webhook_jobs.id) | Indexed |
| attempt_number | Integer | 1-based |
| status | String | `success` / `retrying` / `failed` |
| response_code | Integer, nullable | Null on timeout / network error |
| error_message | Text, nullable | |
| attempted_at | Timestamp (tz) | |

Every attempt creates a **separate** row.

## 8. Job State Machine

```
 queued ──► processing ──► success
   ▲            │
   │            ├──► retrying ──► processing (next attempt)
   │            │
   │            └──► failed   (permanent error OR retries exhausted)
   │
   └── rate-limited: processing is never entered; job stays/returns to queued
       and is rescheduled with a delay (no attempt consumed)
```

## 9. Non-Functional Requirements

| ID | Requirement |
|---|---|
| NFR-01 | Ingestion returns 202 right after persist + enqueue; never waits on a destination. |
| NFR-02 | Jobs are persisted; worker restarts never silently lose pending jobs; delivery records remain in DB. |
| NFR-03 | Handle: invalid JSON, invalid URLs, network failures, connection timeouts, 429s, 5xx, Redis failures, DB failures — each with a defined behavior (see `architecture.md` §9). |
| NFR-04 | Prevent excessive request rate (and, as a stretch, excessive concurrency) to any single destination. |
| NFR-05 | Delivery semantics: **at-least-once**. Receivers can deduplicate via `X-Webhook-Event-Id` / `X-Webhook-Job-Id`. |
| NFR-06 | Outbound HTTP timeout is bounded (`HTTP_TIMEOUT_SECONDS`, default 10). |
| NFR-07 | SSRF safety: by default reject destinations resolving to private/loopback/link-local addresses; toggle via `ALLOW_PRIVATE_DESTINATIONS` (enabled in dev/tests). |

## 10. Configuration (environment variables)

| Variable | Default | Purpose |
|---|---|---|
| `DATABASE_URL` | — | PostgreSQL connection string |
| `REDIS_URL` | — | Redis for Celery broker and rate limiter |
| `HTTP_TIMEOUT_SECONDS` | `10` | Outbound request timeout |
| `MAX_RETRIES` | `3` | Retries after the initial attempt |
| `BACKOFF_BASE_SECONDS` | `2` | Base of exponential backoff |
| `RATE_LIMIT_CAPACITY` | `5` | Token bucket size (burst) |
| `RATE_LIMIT_REFILL_PER_SECOND` | `1` | Sustained rate per destination |
| `EXTRA_RETRYABLE_STATUS_CODES` | *(empty)* | Comma-separated extra codes to retry |
| `ALLOW_PRIVATE_DESTINATIONS` | `false` | Allow localhost/private IP targets |
| `MAX_PAYLOAD_BYTES` | `262144` | Request size cap |
| `SWEEPER_INTERVAL_SECONDS` | `30` | Beat schedule |
| `STALE_JOB_THRESHOLD_SECONDS` | `120` | Age before a non-terminal job is re-enqueued |
| `LOG_LEVEL` | `INFO` | |

## 11. Testing Requirements

All scenarios below must have automated tests (Pytest). Use mock HTTP servers for success / slow / failing destinations.

| # | Scenario | Test file |
|---|---|---|
| 1 | Valid request → 202 | `test_api.py` |
| 2 | Invalid request → validation error | `test_api.py` |
| 3 | Event persisted and queued | `test_api.py` |
| 4 | Successful destination receives the webhook | `test_worker.py` |
| 5 | HTTP 500 triggers retry | `test_retry.py` |
| 6 | HTTP 429 triggers retry | `test_retry.py` |
| 7 | Timeout triggers retry | `test_retry.py` |
| 8 | Backoff is exactly 2, 4, 8 s | `test_retry.py` |
| 9 | Job `failed` after exhausting retries | `test_retry.py` |
| 10 | Every attempt logged | `test_worker.py` |
| 11 | Rate limiter blocks excess requests | `test_rate_limiter.py` |
| 12 | Slow destination does not block ingestion API | `test_api.py` |
| 13 | Worker restart does not lose persisted pending jobs | `test_worker.py` |
| + | 400/404 are **not** retried | `test_retry.py` |
| + | Rate-limit deferral consumes no attempt | `test_rate_limiter.py` |

## 12. Deployment Requirements

Publicly deployed (free tier where available) with: FastAPI API service, Redis, Celery worker (+ beat), PostgreSQL. API and worker share the same Redis and PostgreSQL. Provide a public API URL and verify, in the deployed environment, that: the API accepts requests, jobs enter the queue, workers process them, attempts are recorded, and retries work.

> ⚠️ Render's free tier does not include dedicated Background Workers. See `architecture.md` §10 for the single-container workaround and `memory.md` for the decision record.

## 13. Final Deliverables

1. Publicly accessible live API endpoint.
2. GitHub repository with complete source.
3. `README.md` with: overview, architecture, technology choices, local setup, environment variables, API docs, sample cURL, testing instructions, deployment URL, and explanations of background processing, retries and rate limiting.

## 14. Acceptance Criteria

- [x] Ingestion API works and responds 202 immediately
- [x] Jobs are persisted and queued
- [x] Worker delivers webhook events
- [x] Retries follow 2 / 4 / 8 s exponential backoff, max 3 retries
- [x] Permanent 4xx errors are not retried
- [x] Delivery logs are maintained for every attempt
- [x] Per-destination rate limiting works; limited jobs are rescheduled, never lost
- [x] Slow destination does not block the API
- [x] Persisted jobs survive worker restarts (sweeper + `acks_late`)
- [x] All 13 required test scenarios pass
- [ ] Deployed and publicly accessible; retry verified in deployment
- [ ] GitHub repo and README complete — README complete; public repo URL pending

Local verification 2026-11-21: the first ten items are covered by 90 passing tests plus the healthy local Docker stack. The last two require the deployed URL and a public repository remote.

## 15. Assumptions and Clarifications

| ID | Item | Resolution |
|---|---|---|
| A-1 | `jobId` format in the example is `job_123456` while DB id is UUID | API returns `job_<uuid>`; prefix stripped on lookup |
| A-2 | FR-04 lists event ID and URL per log; table 2 lacks them | Obtained via join on `job_id` |
| A-3 | Rate-limit "destination" | Normalized origin (`scheme://host:port`) |
| A-4 | Does a rate-limit deferral count as a retry? | No |
| A-5 | Duplicate `eventId` submissions | Accepted as separate jobs (idempotency is a stretch goal) |
| A-6 | 3xx responses | Treated as non-success, non-retryable |
