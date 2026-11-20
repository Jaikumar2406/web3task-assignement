#!/bin/sh
set -e

celery -A app.workers.celery_app worker --loglevel=info --pool=solo --concurrency=2 -B &
exec uvicorn app.main:app --host 0.0.0.0 --port "${PORT:-8000}"
