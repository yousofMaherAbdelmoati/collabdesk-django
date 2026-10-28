"""Periodic maintenance for durable desk data."""

from celery import shared_task

from .services import cleanup_expired_idempotency_records


@shared_task(name="apps.desk.cleanup_idempotency_records")
def cleanup_idempotency_records():
    return cleanup_expired_idempotency_records()
