"""Celery application configuration shared by the API and workers."""

from __future__ import annotations

from celery import Celery

from app.config import settings

celery_app = Celery(
    "webhook_dispatcher",
    broker=settings.redis_url,
    backend=None,
    include=["app.workers.tasks"],
)

celery_app.conf.update(
    task_acks_late=True,
    task_reject_on_worker_lost=True,
    worker_prefetch_multiplier=1,
    task_time_limit=int(settings.http_timeout_seconds) + 30,
    broker_transport_options={"visibility_timeout": 3600},
    result_backend=None,
    beat_schedule={
        "sweep-stale-jobs": {
            "task": "sweep_stale_jobs",
            "schedule": settings.sweeper_interval_seconds,
        }
    },
)
