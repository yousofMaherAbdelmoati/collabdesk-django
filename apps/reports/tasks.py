"""Durable report execution and database-outbox publication.

Celery's broker acknowledgement is deliberately not treated as a database
transaction. Publishing can therefore happen more than once if a publisher
dies after broker acceptance and before it records ``published``. Every
consumer below is safe for that at-least-once delivery model.
"""

from __future__ import annotations

import logging
import threading
import uuid
from datetime import timedelta, timezone as datetime_timezone

from celery import shared_task
from django.conf import settings
from django.core.cache import cache
from django.db import close_old_connections, transaction
from django.db.models import Q
from django.utils import timezone

from apps.desk.models import OutboxEvent
from .models import ReportJob
from .services import (
    compute_precomputed_summaries,
    compute_summary_rows,
    compute_report,
    persist_precomputed_summaries,
    report_cache_key,
)


logger = logging.getLogger("collabdesk.report_jobs")


def _backoff(attempt: int, *, base: int, maximum: int) -> timedelta:
    """Return a bounded exponential delay, with no random retry contract."""
    return timedelta(seconds=min(maximum, base * (2 ** max(0, attempt - 1))))


def _report_retry_at(attempt: int, now):
    return now + _backoff(
        attempt,
        base=settings.REPORT_RETRY_BACKOFF_SECONDS,
        maximum=settings.REPORT_RETRY_MAX_SECONDS,
    )


def _outbox_retry_at(attempt: int, now):
    return now + _backoff(
        attempt,
        base=settings.OUTBOX_RETRY_BACKOFF_SECONDS,
        maximum=settings.OUTBOX_RETRY_MAX_SECONDS,
    )


def enqueue_report_dispatch(job_id: int, *, available_at=None) -> OutboxEvent:
    """Create a report event inside the caller's database transaction."""
    return OutboxEvent.objects.create(
        type="report.run",
        payload={"job_id": job_id},
        available_at=available_at or timezone.now(),
    )


def wake_outbox_publisher() -> None:
    """Best-effort wake-up only; beat remains the durable recovery path."""
    try:
        publish_outbox_events.delay()
    except Exception:
        # A Redis/Celery outage must not roll back the already-committed job
        # and outbox event. The periodic publisher will claim it later.
        logger.warning("outbox_wake_failed", exc_info=True)


def _claim_report_job(job_id: int, now):
    """Atomically acquire a runnable report lease, or return ``None``."""
    with transaction.atomic():
        job = ReportJob.objects.select_for_update(skip_locked=True).filter(id=job_id).first()
        if job is None or job.status in {ReportJob.Status.COMPLETED, ReportJob.Status.FAILED}:
            return None
        runnable = (
            job.status == ReportJob.Status.QUEUED and job.next_attempt_at <= now
        ) or (
            job.status == ReportJob.Status.PROCESSING
            and job.lease_expires_at is not None
            and job.lease_expires_at <= now
        )
        if not runnable:
            return None
        if job.attempts >= settings.REPORT_MAX_ATTEMPTS:
            job.status = ReportJob.Status.FAILED
            job.error = "Report attempt limit reached before a worker could finish."
            job.lease_token = None
            job.lease_expires_at = None
            job.save(update_fields=["status", "error", "lease_token", "lease_expires_at"])
            return None

        token = uuid.uuid4()
        job.status = ReportJob.Status.PROCESSING
        job.attempts += 1
        job.started_at = job.started_at or now
        job.heartbeat_at = now
        job.lease_token = token
        job.lease_expires_at = now + timedelta(seconds=settings.REPORT_JOB_LEASE_SECONDS)
        job.save(
            update_fields=[
                "status", "attempts", "started_at", "heartbeat_at",
                "lease_token", "lease_expires_at",
            ]
        )
        return job, token


def _renew_report_lease(job_id: int, token: uuid.UUID, now=None) -> bool:
    now = now or timezone.now()
    return bool(
        ReportJob.objects.filter(
            id=job_id,
            status=ReportJob.Status.PROCESSING,
            lease_token=token,
        ).update(
            heartbeat_at=now,
            lease_expires_at=now + timedelta(seconds=settings.REPORT_JOB_LEASE_SECONDS),
        )
    )


class _ReportLeaseHeartbeat:
    """Refresh a long-running compute lease without making Redis authoritative."""

    def __init__(self, job_id: int, token: uuid.UUID):
        self.job_id = job_id
        self.token = token
        self.stop = threading.Event()
        self.thread: threading.Thread | None = None

    def __enter__(self):
        self.thread = threading.Thread(target=self._run, daemon=True, name=f"report-heartbeat-{self.job_id}")
        self.thread.start()
        return self

    def _run(self):
        try:
            while not self.stop.wait(settings.REPORT_HEARTBEAT_SECONDS):
                close_old_connections()
                if not _renew_report_lease(self.job_id, self.token):
                    return
        except Exception:
            # The existing lease still bounds ownership. A transient database
            # failure never lets this thread claim that work succeeded.
            logger.warning("report_heartbeat_failed", extra={"job_id": self.job_id}, exc_info=True)
        finally:
            close_old_connections()

    def __exit__(self, exc_type, exc, traceback):
        self.stop.set()
        if self.thread is not None:
            self.thread.join(timeout=min(1, settings.REPORT_HEARTBEAT_SECONDS + 0.1))


def _record_report_failure(job_id: int, token: uuid.UUID, exc: Exception) -> str:
    """Persist retry/terminal state before asking the outbox to dispatch again."""
    now = timezone.now()
    with transaction.atomic():
        job = ReportJob.objects.select_for_update().filter(id=job_id).first()
        if job is None or job.status != ReportJob.Status.PROCESSING or job.lease_token != token:
            return "lost_lease"
        # This is returned by the report API; do not expose database/broker
        # exception text (which can contain connection details or query data).
        job.error = "Report processing failed; retry scheduled."
        job.lease_token = None
        job.lease_expires_at = None
        if job.attempts >= settings.REPORT_MAX_ATTEMPTS:
            job.status = ReportJob.Status.FAILED
            job.completed_at = now
            job.save(update_fields=["status", "error", "completed_at", "lease_token", "lease_expires_at"])
            return "failed"

        job.status = ReportJob.Status.QUEUED
        job.next_attempt_at = _report_retry_at(job.attempts, now)
        job.save(update_fields=["status", "error", "next_attempt_at", "lease_token", "lease_expires_at"])
        enqueue_report_dispatch(job.id, available_at=job.next_attempt_at)
        transaction.on_commit(wake_outbox_publisher)
        return "retrying"


@shared_task(bind=True, name="apps.reports.run_report")
def run_report(self, job_id: int):
    """Run one report under a renewable DB lease.

    Duplicate delivery returns harmlessly once the first owner has completed,
    and a worker that loses its lease cannot persist a late result.
    """
    claimed = _claim_report_job(job_id, timezone.now())
    if claimed is None:
        return "not_runnable"
    job, token = claimed
    try:
        with _ReportLeaseHeartbeat(job.id, token):
            result = compute_report(job)
            summaries = compute_precomputed_summaries(job)
        now = timezone.now()
        with transaction.atomic():
            # This conditional ownership check is the idempotency guard for
            # persisted report results and comes before the optional cache.
            # A report and the summaries derived from the same source snapshot
            # commit together. Cache publication remains deliberately outside
            # this transaction: Redis must never decide report durability.
            completed = ReportJob.objects.filter(
                id=job.id,
                status=ReportJob.Status.PROCESSING,
                lease_token=token,
            ).update(
                result=result,
                status=ReportJob.Status.COMPLETED,
                completed_at=now,
                expires_at=now + timedelta(seconds=settings.REPORT_RESULT_TTL_SECONDS),
                lease_token=None,
                lease_expires_at=None,
                heartbeat_at=now,
                error="",
            )
            if completed:
                persist_precomputed_summaries(summaries)
        if not completed:
            return "lost_lease"
        try:
            cache.set(report_cache_key(job.id), result, timeout=settings.REPORT_RESULT_TTL_SECONDS)
        except Exception:
            logger.warning("report_cache_write_failed", extra={"job_id": job.id}, exc_info=True)
        return "completed"
    except Exception as exc:
        # Do not rely on Celery's retry bookkeeping: the DB row and a delayed
        # outbox event are the durable retry schedule.
        return _record_report_failure(job.id, token, exc)


def _claim_next_outbox_event(now):
    with transaction.atomic():
        event = (
            OutboxEvent.objects.select_for_update(skip_locked=True)
            .filter(
                Q(state=OutboxEvent.State.PENDING, available_at__lte=now)
                | Q(state=OutboxEvent.State.PUBLISHING, lease_expires_at__lte=now)
            )
            .order_by("available_at", "id")
            .first()
        )
        if event is None:
            return None
        if event.attempts >= settings.OUTBOX_MAX_ATTEMPTS:
            event.state = OutboxEvent.State.FAILED
            event.failed_at = now
            event.lease_token = None
            event.lease_expires_at = None
            event.last_error = "Outbox publish attempt limit reached."
            event.save(update_fields=["state", "failed_at", "lease_token", "lease_expires_at", "last_error"])
            return "terminal", None
        token = uuid.uuid4()
        event.state = OutboxEvent.State.PUBLISHING
        event.attempts += 1
        event.lease_token = token
        event.lease_expires_at = now + timedelta(seconds=settings.OUTBOX_LEASE_SECONDS)
        event.save(update_fields=["state", "attempts", "lease_token", "lease_expires_at"])
        return event, token


def _publish_to_celery(event: OutboxEvent) -> None:
    if event.type == "report.run":
        job_id = event.payload.get("job_id")
        if not isinstance(job_id, int):
            raise ValueError("report.run outbox event has no integer job_id")
        # Task ids aid observability only. Brokers/workers may still deliver
        # this more than once, which run_report explicitly tolerates.
        run_report.apply_async(args=[job_id], task_id=f"report-outbox-{event.id}")
        return
    # Existing non-report events are still durably published rather than being
    # silently discarded. This no-op consumer is the extension point for a
    # future real subscriber and is deliberately idempotent.
    handle_outbox_event.apply_async(args=[event.id], task_id=f"outbox-event-{event.id}")


def _mark_outbox_published(event_id: int, token: uuid.UUID) -> bool:
    return bool(
        OutboxEvent.objects.filter(
            id=event_id,
            state=OutboxEvent.State.PUBLISHING,
            lease_token=token,
        ).update(
            state=OutboxEvent.State.PUBLISHED,
            published_at=timezone.now(),
            lease_token=None,
            lease_expires_at=None,
            last_error="",
        )
    )


def _release_outbox_failure(event_id: int, token: uuid.UUID, exc: Exception) -> str:
    now = timezone.now()
    with transaction.atomic():
        event = OutboxEvent.objects.select_for_update().filter(id=event_id).first()
        if event is None or event.state != OutboxEvent.State.PUBLISHING or event.lease_token != token:
            return "lost_lease"
        # Outbox state is operational metadata, not a place for broker URLs,
        # credentials, or arbitrary exception messages.
        event.last_error = "Celery publish failed; retry scheduled."
        event.lease_token = None
        event.lease_expires_at = None
        if event.attempts >= settings.OUTBOX_MAX_ATTEMPTS:
            event.state = OutboxEvent.State.FAILED
            event.failed_at = now
            event.save(update_fields=["state", "failed_at", "last_error", "lease_token", "lease_expires_at"])
            return "terminal"
        event.state = OutboxEvent.State.PENDING
        event.available_at = _outbox_retry_at(event.attempts, now)
        event.save(update_fields=["state", "available_at", "last_error", "lease_token", "lease_expires_at"])
        return "retrying"


@shared_task(name="apps.reports.publish_outbox_events")
def publish_outbox_events(batch_size=None):
    """Publish due events with DB leases, bounded retries, and no exactly-once claim."""
    batch_size = batch_size or settings.OUTBOX_PUBLISH_BATCH_SIZE
    outcomes = {"published": 0, "retrying": 0, "terminal": 0}
    for _ in range(batch_size):
        claimed = _claim_next_outbox_event(timezone.now())
        if claimed is None:
            break
        if claimed[0] == "terminal":
            outcomes["terminal"] += 1
            continue
        event, token = claimed
        try:
            _publish_to_celery(event)
        except Exception as exc:
            outcome = _release_outbox_failure(event.id, token, exc)
            if outcome in outcomes:
                outcomes[outcome] += 1
        else:
            if _mark_outbox_published(event.id, token):
                outcomes["published"] += 1
    return outcomes


@shared_task(name="apps.reports.handle_outbox_event")
def handle_outbox_event(event_id: int):
    """Placeholder idempotent consumer for event types outside this task's scope."""
    logger.info("outbox_event_delivered", extra={"event_id": event_id})
    return "delivered"


@shared_task(name="apps.reports.recover_stale_report_jobs")
def recover_stale_report_jobs(batch_size=None):
    """Turn abandoned processing leases into delayed durable re-dispatches."""
    batch_size = batch_size or settings.REPORT_RECOVERY_BATCH_SIZE
    recovered = terminal = 0
    for _ in range(batch_size):
        now = timezone.now()
        with transaction.atomic():
            job = (
                ReportJob.objects.select_for_update(skip_locked=True)
                .filter(status=ReportJob.Status.PROCESSING, lease_expires_at__lte=now)
                .order_by("lease_expires_at", "id")
                .first()
            )
            if job is None:
                break
            job.lease_token = None
            job.lease_expires_at = None
            if job.attempts >= settings.REPORT_MAX_ATTEMPTS:
                job.status = ReportJob.Status.FAILED
                job.error = "Report lease expired after the maximum number of attempts."
                job.completed_at = now
                job.save(update_fields=["status", "error", "completed_at", "lease_token", "lease_expires_at"])
                terminal += 1
                continue
            job.status = ReportJob.Status.QUEUED
            job.next_attempt_at = _report_retry_at(job.attempts, now)
            job.error = "Report worker lease expired; retry scheduled."
            job.save(update_fields=["status", "next_attempt_at", "error", "lease_token", "lease_expires_at"])
            enqueue_report_dispatch(job.id, available_at=job.next_attempt_at)
            transaction.on_commit(wake_outbox_publisher)
            recovered += 1
    return {"recovered": recovered, "terminal": terminal}


@shared_task(name="apps.reports.expire_report_results")
def expire_report_results(batch_size=None):
    """Remove expired durable output and its best-effort Redis copy."""
    batch_size = batch_size or settings.REPORT_RECOVERY_BATCH_SIZE
    now = timezone.now()
    jobs = list(
        ReportJob.objects.filter(
            status=ReportJob.Status.COMPLETED,
        ).filter(Q(expires_at__isnull=True) | Q(expires_at__lte=now)).order_by("expires_at", "id")[:batch_size]
    )
    expired = 0
    for job in jobs:
        # Conditional update makes concurrent scheduler runs harmless.
        updated = ReportJob.objects.filter(
            id=job.id, status=ReportJob.Status.COMPLETED,
        ).filter(Q(expires_at__isnull=True) | Q(expires_at__lte=now)).update(
            result=None, status=ReportJob.Status.EXPIRED,
        )
        if updated:
            try:
                cache.delete(report_cache_key(job.id))
            except Exception:
                logger.warning("report_cache_delete_failed", extra={"job_id": job.id}, exc_info=True)
            expired += 1
    return {"expired": expired}


@shared_task(name="apps.reports.refresh_analytics_summaries")
def refresh_analytics_summaries(days=32):
    """Rebuild recent global UTC day/month summaries from PostgreSQL."""
    if not isinstance(days, int) or days < 1:
        raise ValueError("days must be a positive integer")
    today = timezone.now().astimezone(datetime_timezone.utc).date()
    params = {"start": (today - timedelta(days=days - 1)).isoformat(), "end": today.isoformat()}
    summaries = compute_summary_rows(params)
    with transaction.atomic():
        persist_precomputed_summaries(summaries)
    return {"refreshed": len(summaries)}
