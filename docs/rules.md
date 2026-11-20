# rules.md — Engineering Rules & Conventions

> Applies to every contributor, human or AI. If a rule blocks you, don't bypass it silently: note the exception in `memory.md` §4 (Decision Log).
> Priority when documents conflict: `prd.md` (requirements) > `rules.md` > `architecture.md` > `phases.md`. Record any conflict in `memory.md`.

---

## 1. Golden Rules (never violate)

1. **The API never waits on a destination.** No outbound webhook HTTP call inside a request handler. Ever.
2. **Persist, commit, then enqueue.** Never enqueue a job that isn't committed.
3. **The database is the source of truth.** Redis may lose data; the system must recover from PostgreSQL.
4. **No silent loss.** Every job ends in `success` or `failed` — never stuck in `processing`, never dropped by the rate limiter.
5. **One `delivery_logs` row per attempt**, written in the same transaction as the status update.
6. **Retries follow the schedule exactly:** 2 s, 4 s, 8 s; max 3 retries (4 attempts total). Do not change without updating the PRD and tests.
7. **Rate-limit deferrals are not attempts** and never consume retries.
8. **No secrets in the repo.** Config comes from environment variables only.
9. **Don't scope-creep.** Build what `prd.md` asks, in `phases.md` order. Stretch items only after the phase exit criteria are met.

## 2. Python & Style

- Python 3.11+. Type hints on all function signatures; `from __future__ import annotations` where helpful.
- Format with **black** (line length 100) and lint with **ruff**. Run both before every commit.
- Naming: `snake_case` functions/variables, `PascalCase` classes, `UPPER_SNAKE` constants. Pydantic *API* fields use `camelCase` aliases (`destinationUrl`, `eventId`); internal code and DB use `snake_case`.
- Functions do one thing; target < 40 lines. Prefer early returns over deep nesting.
- No wildcard imports. No mutable default arguments. No bare `except:` — catch specific exceptions; use `except Exception` only at a boundary, and always log + handle.
- Docstrings on every public function/class explaining *why*, not just *what*.
- Comments for non-obvious decisions (e.g. why Lua, why fail-closed). Don't narrate trivial code.
- No `print()`; use the `logging` module.

## 3. Project Structure Rules

- Follow the layout in `architecture.md` §4. Add files freely inside it; don't reshuffle top-level structure without a decision-log entry.
- **Layering:** `api` and `workers` → `services` → `models`/`config`. Services never import from `api` or `workers`. Models contain no business logic.
- **Pure logic stays pure:** `services/retry.py` and outcome classification have **no I/O, no clock, no globals** — inputs in, decision out.
- Single `Settings` object (`pydantic-settings`) in `config.py`. Never call `os.getenv` elsewhere. Never hard-code retry counts, timeouts, URLs or rate limits.
- Keep route handlers thin: parse → call service/DB → return. Business rules live in `services/`.

## 4. API Rules

- Route prefix `/api/webhooks`. Response models are explicit Pydantic schemas — never return ORM objects directly.
- Status codes: `202` accepted · `422` validation · `413` too large · `404` unknown job · `503` dependency unavailable. No `500`s for expected failures.
- Validate **everything** at the edge: URL scheme (`http`/`https` only), non-empty `eventId`, `payload` is a JSON object, size cap.
- Job IDs are exposed as `job_<uuid>`; parse/strip the prefix in one helper and use it everywhere.
- Error bodies are consistent JSON; never leak stack traces, SQL, or internal hostnames.
- Handlers are `def` (sync) unless there is a real reason for `async`. Never block an `async def` handler with sync I/O.

## 5. Database Rules

- UUID primary keys; timezone-aware timestamps (UTC). Never use naive datetimes.
- Use SQLAlchemy sessions as context managers or via the `get_db` dependency; **always** commit/rollback explicitly and close sessions.
- Row-lock jobs (`SELECT … FOR UPDATE`) when claiming for delivery; use `SKIP LOCKED` in the sweeper.
- Indexes: `webhook_jobs.status`, `webhook_jobs.event_id`, `(status, updated_at)`, `delivery_logs.job_id`.
- Update `updated_at` on every status change (the sweeper depends on it).
- No raw SQL string concatenation. Parameterized queries / ORM only.
- Never delete delivery logs.

## 6. Worker, Retry & Rate-Limit Rules

- Task signature: `deliver_webhook(job_id: str)` — pass **only the ID**, never the payload (the DB is read fresh).
- Tasks must be **idempotent**: first step is "load job; if terminal or missing → return".
- Configure `acks_late=True`, `reject_on_worker_lost=True`, `worker_prefetch_multiplier=1`.
- Reschedule with `apply_async(args=[job_id], countdown=…)`. **Do not use `self.retry`** (decision D-02).
- Order inside the task: terminal check → **rate-limit check** → increment attempt → send → classify → log → update → reschedule.
- Every outbound call has an explicit timeout and `follow_redirects=False`.
- Classification table (do not improvise): 2xx success · 429/5xx/timeout/transport error retryable · other 4xx/3xx permanent.
- Any unexpected exception inside the attempt → log the attempt as failed with the error and move the job to `failed` (or schedule retry if clearly transient). A job must never be left `processing`.
- Rate limiter: **Redis Lua, atomic, using Redis `TIME`**. One bucket per normalized origin. Fail **closed** if Redis errors.
- Outbound headers: `X-Webhook-Job-Id`, `X-Webhook-Event-Id`, `X-Webhook-Attempt`, `Content-Type: application/json`.
- Truncate stored error messages (≤ 1000 chars). Never store auth headers.

## 7. Security Rules

- SSRF guard: reject non-http(s) schemes and private / loopback / link-local / metadata IPs unless `ALLOW_PRIVATE_DESTINATIONS=true`. Re-validate at send time.
- Never log full payloads at INFO or above; log IDs, status, latency.
- `.env` is git-ignored; `.env.example` has placeholders only.
- Docker images run as a non-root user; pin base image versions.
- Don't commit tokens, DB URLs or Render credentials — not even "temporary" ones.

## 8. Testing Rules

- Every PRD §11 scenario has at least one named test. Name tests by behavior: `test_500_triggers_retry_with_2s_backoff`.
- **No real network** in unit tests; mock HTTPX with `respx`. Use `pytest-httpserver` (or a tiny local server) for the slow-destination test.
- **No `time.sleep` for backoff.** Patch `apply_async` and assert `countdown` values `2, 4, 8`.
- The rate limiter's Lua script is tested against **real Redis** (compose service / CI service), not a mock.
- Each test is independent: fresh DB state (transaction rollback or truncate), flushed Redis keys.
- Test both the happy path **and** the failure path for every new behavior. Bug fix → add a regression test first.
- `pytest -q` must pass from a clean clone before any phase is marked done.

## 9. Logging & Observability

- Structured, single-line logs with `job_id`, `event_id`, `attempt`, `origin`, `outcome`, `latency_ms`.
- INFO for lifecycle transitions; WARNING for retryable failures; ERROR for permanent failures / unexpected exceptions.
- Health endpoint must check DB and Redis.

## 10. Git & Workflow

- Branch per phase or feature: `phase-2/retry-logic`. Small, focused commits.
- **Conventional commits:** `feat:`, `fix:`, `test:`, `docs:`, `refactor:`, `chore:`.
- Commit after each passing task, not at the end. `main` always runs.
- Don't commit: `.env`, `__pycache__`, `.venv`, `*.pyc`, `.pytest_cache`, local DB files.
- Update docs in the **same commit** as the behavior change.

## 11. Documentation Rules

- README must satisfy PRD §13 (overview, architecture, stack, local setup, env vars, API docs, cURL samples, testing, deployment URL, explanations of background processing / retries / rate limiting).
- Document known limitations honestly (at-least-once duplicates, free-tier spin-down, 503-retry duplicates).
- After every session: update `memory.md` §2 (Status) and §9 (Session Log).

## 12. Rules for AI Coding Assistants

1. Read `memory.md` → `rules.md` → relevant doc sections **before** writing code.
2. One task at a time, from the current phase in `phases.md`. State which task you're doing.
3. Don't invent requirements, endpoints, columns, or env vars. If something is ambiguous, ask or record an assumption in `memory.md`.
4. Don't override a Decision Log entry silently; flag the conflict first.
5. Prefer the simplest implementation that satisfies the PRD. No speculative abstractions, no extra frameworks.
6. Always include or update tests with code changes. Show how to run them.
7. Never fake results (e.g. claiming tests pass, or that deployment works) — run it, or say it hasn't been run.
8. When finished: summarize what changed, what's untested, and what's next; update `memory.md`.

## 13. Definition of Done (per task)

- [ ] Meets the PRD requirement it maps to
- [ ] Follows the Golden Rules (§1)
- [ ] Tests written and passing; no network in unit tests
- [ ] `ruff` + `black` clean
- [ ] No secrets, no stray `print`s, no unresolved `TODO`s (or they're logged in `memory.md`)
- [ ] Docs / `memory.md` updated
