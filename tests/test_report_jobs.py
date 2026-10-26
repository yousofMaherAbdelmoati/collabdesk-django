import concurrent.futures
import threading
import uuid
from datetime import timedelta
from unittest.mock import patch

import pytest
from django.contrib.auth import get_user_model
from django.db import connections
from django.test import override_settings
from django.utils import timezone

from apps.desk.models import OutboxEvent
from apps.reports.models import ReportJob
from apps.reports.services import request_report
from apps.reports.tasks import (
    _renew_report_lease,
    publish_outbox_events,
    recover_stale_report_jobs,
    run_report,
)


@pytest.fixture
def admin(db):
    return get_user_model().objects.create_user(username="report-admin", password="x", role="admin")


@pytest.mark.django_db
def test_request_commits_job_and_outbox_before_best_effort_wake(admin):
    with patch("apps.reports.tasks.wake_outbox_publisher"):
        job, created = request_report(user=admin, params={"period": "fictional"})

    assert created is True
    event = OutboxEvent.objects.get()
    assert event.type == "report.run"
    assert event.payload == {"job_id": job.id}
    assert event.state == OutboxEvent.State.PENDING


@pytest.mark.django_db
def test_redis_celery_wake_outage_does_not_lose_committed_dispatch(admin):
    # The request transaction must not depend on Redis accepting the hint.
    with patch("apps.reports.tasks.publish_outbox_events.delay", side_effect=OSError("fictional Redis outage")):
        job, created = request_report(user=admin, params={"period": "fictional outage"})

    assert created is True
    assert ReportJob.objects.filter(id=job.id, status=ReportJob.Status.QUEUED).exists()
    assert OutboxEvent.objects.filter(type="report.run", payload={"job_id": job.id}, state=OutboxEvent.State.PENDING).exists()


@pytest.mark.django_db
def test_publisher_marks_event_after_celery_accepts_it(admin):
    job = ReportJob.objects.create(requested_by=admin, fingerprint="a" * 64, parameters={})
    event = OutboxEvent.objects.create(type="report.run", payload={"job_id": job.id})

    with patch("apps.reports.tasks.run_report.apply_async") as dispatch:
        assert publish_outbox_events.run() == {"published": 1, "retrying": 0, "terminal": 0}

    dispatch.assert_called_once()
    event.refresh_from_db()
    assert event.state == OutboxEvent.State.PUBLISHED
    assert event.published_at is not None
    assert event.attempts == 1


@pytest.mark.django_db
def test_celery_outage_keeps_outbox_event_for_exponential_retry(admin):
    job = ReportJob.objects.create(requested_by=admin, fingerprint="b" * 64, parameters={})
    event = OutboxEvent.objects.create(type="report.run", payload={"job_id": job.id})

    with patch("apps.reports.tasks.run_report.apply_async", side_effect=OSError("fictional broker unavailable")):
        assert publish_outbox_events.run() == {"published": 0, "retrying": 1, "terminal": 0}

    event.refresh_from_db()
    assert event.state == OutboxEvent.State.PENDING
    assert event.attempts == 1
    assert event.available_at > timezone.now()
    assert event.last_error == "Celery publish failed; retry scheduled."


@pytest.mark.django_db
def test_publisher_restart_reclaims_expired_publish_lease(admin):
    job = ReportJob.objects.create(requested_by=admin, fingerprint="c" * 64, parameters={})
    event = OutboxEvent.objects.create(
        type="report.run",
        payload={"job_id": job.id},
        state=OutboxEvent.State.PUBLISHING,
        attempts=1,
        lease_token=uuid.uuid4(),
        lease_expires_at=timezone.now() - timedelta(seconds=1),
    )

    with patch("apps.reports.tasks.run_report.apply_async"):
        publish_outbox_events.run()

    event.refresh_from_db()
    assert event.state == OutboxEvent.State.PUBLISHED
    # A crash after broker acceptance intentionally permits a duplicate send.
    assert event.attempts == 2


@pytest.mark.django_db
def test_duplicate_task_delivery_persists_result_once_before_cache(admin):
    job = ReportJob.objects.create(requested_by=admin, fingerprint="d" * 64, parameters={})
    result = {"total_tickets": 0, "generated_at": "fictional"}

    def assert_persisted_then_cache(*args, **kwargs):
        job.refresh_from_db()
        assert job.status == ReportJob.Status.COMPLETED
        assert job.result == result

    with patch("apps.reports.tasks.compute_report", return_value=result) as compute, patch(
        "apps.reports.tasks.cache.set", side_effect=assert_persisted_then_cache
    ):
        assert run_report.run(job.id) == "completed"
        assert run_report.run(job.id) == "not_runnable"

    job.refresh_from_db()
    assert compute.call_count == 1
    assert job.attempts == 1
    assert job.status == ReportJob.Status.COMPLETED


@pytest.mark.django_db
@override_settings(REPORT_MAX_ATTEMPTS=2, REPORT_RETRY_BACKOFF_SECONDS=1, REPORT_RETRY_MAX_SECONDS=4)
def test_report_failures_are_durably_retried_then_terminal(admin):
    job = ReportJob.objects.create(requested_by=admin, fingerprint="e" * 64, parameters={})
    with patch("apps.reports.tasks.compute_report", side_effect=RuntimeError("fictional compute failure")), patch(
        "apps.reports.tasks.wake_outbox_publisher"
    ):
        assert run_report.run(job.id) == "retrying"
        job.refresh_from_db()
        assert job.status == ReportJob.Status.QUEUED
        assert job.attempts == 1
        retry_event = OutboxEvent.objects.get(type="report.run")
        assert retry_event.available_at == job.next_attempt_at

        ReportJob.objects.filter(id=job.id).update(next_attempt_at=timezone.now() - timedelta(seconds=1))
        assert run_report.run(job.id) == "failed"

    job.refresh_from_db()
    assert job.status == ReportJob.Status.FAILED
    assert job.attempts == 2
    assert OutboxEvent.objects.count() == 1


@pytest.mark.django_db
@override_settings(REPORT_MAX_ATTEMPTS=2, REPORT_RETRY_BACKOFF_SECONDS=1, REPORT_RETRY_MAX_SECONDS=4)
def test_watchdog_recovers_expired_job_and_applies_terminal_policy(admin):
    first = ReportJob.objects.create(
        requested_by=admin,
        fingerprint="f" * 64,
        parameters={},
        status=ReportJob.Status.PROCESSING,
        attempts=1,
        lease_token=uuid.uuid4(),
        lease_expires_at=timezone.now() - timedelta(seconds=1),
    )
    terminal = ReportJob.objects.create(
        requested_by=admin,
        fingerprint="g" * 64,
        parameters={},
        status=ReportJob.Status.PROCESSING,
        attempts=2,
        lease_token=uuid.uuid4(),
        lease_expires_at=timezone.now() - timedelta(seconds=1),
    )

    with patch("apps.reports.tasks.wake_outbox_publisher"):
        assert recover_stale_report_jobs.run() == {"recovered": 1, "terminal": 1}

    first.refresh_from_db()
    terminal.refresh_from_db()
    assert first.status == ReportJob.Status.QUEUED
    assert first.lease_token is None
    assert OutboxEvent.objects.get(type="report.run", payload={"job_id": first.id}).available_at == first.next_attempt_at
    assert terminal.status == ReportJob.Status.FAILED


@pytest.mark.django_db
def test_lease_heartbeat_renews_only_current_owner(admin):
    token = uuid.uuid4()
    job = ReportJob.objects.create(
        requested_by=admin,
        fingerprint="h" * 64,
        parameters={},
        status=ReportJob.Status.PROCESSING,
        lease_token=token,
        lease_expires_at=timezone.now() + timedelta(seconds=1),
    )
    assert _renew_report_lease(job.id, token) is True
    job.refresh_from_db()
    assert job.heartbeat_at is not None
    assert _renew_report_lease(job.id, uuid.uuid4()) is False


@pytest.mark.django_db
@override_settings(OUTBOX_MAX_ATTEMPTS=1)
def test_outbox_has_terminal_failure_policy(admin):
    job = ReportJob.objects.create(requested_by=admin, fingerprint="i" * 64, parameters={})
    event = OutboxEvent.objects.create(type="report.run", payload={"job_id": job.id})
    with patch("apps.reports.tasks.run_report.apply_async", side_effect=OSError("fictional outage")):
        assert publish_outbox_events.run() == {"published": 0, "retrying": 0, "terminal": 1}
    event.refresh_from_db()
    assert event.state == OutboxEvent.State.FAILED
    assert event.failed_at is not None


@pytest.mark.postgres
@pytest.mark.django_db(transaction=True)
def test_competing_report_requests_have_one_active_row(admin):
    """Exercises the PostgreSQL partial unique index across separate connections."""

    def request(_):
        try:
            with patch("apps.reports.tasks.wake_outbox_publisher"):
                return request_report(user=admin, params={"scope": "fictional"})
        finally:
            connections.close_all()

    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(request, range(2)))

    assert sum(created for _, created in results) == 1
    assert ReportJob.objects.filter(status__in=[ReportJob.Status.QUEUED, ReportJob.Status.PROCESSING]).count() == 1
    assert OutboxEvent.objects.filter(type="report.run").count() == 1


@pytest.mark.postgres
@pytest.mark.django_db(transaction=True)
def test_competing_workers_claim_one_report_lease(admin):
    job = ReportJob.objects.create(requested_by=admin, fingerprint="j" * 64, parameters={})
    compute_started = threading.Event()
    release_compute = threading.Event()

    def slow_compute(_job):
        compute_started.set()
        assert release_compute.wait(timeout=5)
        return {"total_tickets": 0}

    def run_worker():
        try:
            return run_report.run(job.id)
        finally:
            connections.close_all()

    with patch("apps.reports.tasks.compute_report", side_effect=slow_compute):
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            first = pool.submit(run_worker)
            assert compute_started.wait(timeout=5)
            second = pool.submit(run_worker)
            release_compute.set()
            outcomes = [first.result(timeout=5), second.result(timeout=5)]

    job.refresh_from_db()
    assert sorted(outcomes) == ["completed", "not_runnable"]
    assert job.attempts == 1
    assert job.status == ReportJob.Status.COMPLETED
