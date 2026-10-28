from datetime import datetime, timedelta, timezone
from unittest.mock import patch
import uuid

import pytest
from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.test import override_settings
from django.utils import timezone as django_timezone
from rest_framework.test import APIClient
from rest_framework.exceptions import ValidationError

from apps.desk.models import ConversationMessage, Ticket
from apps.reports.models import AnalyticsSummary, ReportJob
from apps.reports.services import compute_report, normalize_report_params, report_cache_key, request_report
from apps.reports.tasks import expire_report_results, run_report


TEST_CACHES = {
    "default": {
        "BACKEND": "django.core.cache.backends.locmem.LocMemCache",
        "LOCATION": "report-analytics-tests",
    }
}


def client_for(user):
    client = APIClient()
    client.force_authenticate(user)
    return client


@pytest.fixture(autouse=True)
def isolated_cache(settings):
    settings.CACHES = TEST_CACHES
    cache.clear()
    yield
    cache.clear()


@pytest.fixture
def admin(db):
    return get_user_model().objects.create_user(username="analytics-admin", password="x", role="admin")


@pytest.fixture
def customer(db):
    return get_user_model().objects.create_user(username="analytics-customer", password="x", role="customer")


@pytest.fixture
def agent(db):
    return get_user_model().objects.create_user(username="analytics-agent", password="x", role="agent")


@pytest.mark.django_db(transaction=True)
def test_response_time_agent_activity_and_utc_buckets_are_correct(admin, customer, agent):
    first = Ticket.objects.create(customer=customer, assigned_agent=agent, status=Ticket.Status.RESOLVED, title="first", description="fictional")
    second = Ticket.objects.create(customer=customer, assigned_agent=agent, status=Ticket.Status.CLAIMED, title="second", description="fictional")
    created = datetime(2026, 4, 2, 23, 50, tzinfo=timezone.utc)
    Ticket.objects.filter(id=first.id).update(created_at=created)
    Ticket.objects.filter(id=second.id).update(created_at=created + timedelta(minutes=1))
    # A customer message is deliberately not a support response.
    customer_message = ConversationMessage.objects.create(ticket=first, sender=customer, client_message_id=uuid.uuid4(), body="fictional customer reply")
    agent_message = ConversationMessage.objects.create(ticket=first, sender=agent, client_message_id=uuid.uuid4(), body="fictional agent reply")
    ConversationMessage.objects.filter(id=customer_message.id).update(created_at=created + timedelta(minutes=2))
    ConversationMessage.objects.filter(id=agent_message.id).update(created_at=created + timedelta(minutes=5))

    job = ReportJob.objects.create(requested_by=admin, fingerprint="a" * 64, parameters=normalize_report_params({"start": "2026-04-02", "end": "2026-04-02", "granularity": "day"}))
    result = compute_report(job)

    assert result["aggregation_timezone"] == "UTC"
    assert result["time_buckets"] == [{
        "period": "2026-04-02", "total_tickets": 2,
        "by_status": {"open": 0, "claimed": 1, "resolved": 1},
        "response_time": {"responded_tickets": 1, "average_seconds": 300.0},
    }]
    assert result["agent_activity"] == [{
        "agent_id": agent.id, "assigned_tickets": 2, "resolved_tickets": 1,
        "responded_tickets": 1, "response_time": {"responded_tickets": 1, "average_seconds": 300.0},
    }]


def test_report_parameters_reject_unimplemented_filters():
    with pytest.raises(ValidationError):
        normalize_report_params({"scope": "all"})


@pytest.mark.django_db(transaction=True)
def test_worker_persists_result_and_daily_monthly_summaries_atomically(admin, customer, agent):
    ticket = Ticket.objects.create(customer=customer, assigned_agent=agent, status=Ticket.Status.CLAIMED, title="summary", description="fictional")
    created = datetime(2026, 5, 7, 10, tzinfo=timezone.utc)
    Ticket.objects.filter(id=ticket.id).update(created_at=created)
    job = ReportJob.objects.create(requested_by=admin, fingerprint="b" * 64, parameters=normalize_report_params({"start": "2026-05-07", "end": "2026-05-07"}))

    assert run_report.run(job.id) == "completed"
    job.refresh_from_db()
    assert job.result["total_tickets"] == 1
    assert job.expires_at is not None
    assert AnalyticsSummary.objects.filter(granularity="day", period_start="2026-05-07").exists()
    assert AnalyticsSummary.objects.filter(granularity="month", period_start="2026-05-01").exists()
    assert cache.get(report_cache_key(job.id)) == job.result


@pytest.mark.django_db(transaction=True)
def test_each_materialized_bucket_has_its_own_agent_activity(admin, customer, agent):
    other_agent = get_user_model().objects.create_user(username="analytics-other-agent", password="x", role="agent")
    first = Ticket.objects.create(customer=customer, assigned_agent=agent, status=Ticket.Status.CLAIMED, title="day one", description="fictional")
    second = Ticket.objects.create(customer=customer, assigned_agent=other_agent, status=Ticket.Status.RESOLVED, title="day two", description="fictional")
    Ticket.objects.filter(id=first.id).update(created_at=datetime(2026, 5, 7, 10, tzinfo=timezone.utc))
    Ticket.objects.filter(id=second.id).update(created_at=datetime(2026, 5, 8, 10, tzinfo=timezone.utc))
    job = ReportJob.objects.create(requested_by=admin, fingerprint="m" * 64, parameters=normalize_report_params({"start": "2026-05-07", "end": "2026-05-08"}))

    assert run_report.run(job.id) == "completed"
    first_summary = AnalyticsSummary.objects.get(granularity="day", period_start="2026-05-07")
    second_summary = AnalyticsSummary.objects.get(granularity="day", period_start="2026-05-08")
    assert first_summary.metrics["agent_activity"] == [{"agent_id": agent.id, "assigned_tickets": 1, "resolved_tickets": 0, "responded_tickets": 0, "response_time": {"responded_tickets": 0, "average_seconds": None}}]
    assert second_summary.metrics["agent_activity"] == [{"agent_id": other_agent.id, "assigned_tickets": 1, "resolved_tickets": 1, "responded_tickets": 0, "response_time": {"responded_tickets": 0, "average_seconds": None}}]


@pytest.mark.django_db(transaction=True)
def test_summary_is_invalidated_after_source_message_changes(admin, customer, agent):
    ticket = Ticket.objects.create(customer=customer, assigned_agent=agent, status=Ticket.Status.CLAIMED, title="invalidate", description="fictional")
    created = datetime(2026, 6, 3, 10, tzinfo=timezone.utc)
    Ticket.objects.filter(id=ticket.id).update(created_at=created)
    job = ReportJob.objects.create(requested_by=admin, fingerprint="c" * 64, parameters=normalize_report_params({"start": "2026-06-03", "end": "2026-06-03"}))
    assert run_report.run(job.id) == "completed"
    assert AnalyticsSummary.objects.filter(period_start="2026-06-03").exists()
    ConversationMessage.objects.create(ticket=ticket, sender=agent, client_message_id=uuid.uuid4(), body="fictional")
    assert not AnalyticsSummary.objects.filter(period_start="2026-06-03").exists()


@pytest.mark.django_db
def test_admin_report_list_download_and_filters_are_authorized(admin, customer):
    first = ReportJob.objects.create(requested_by=admin, fingerprint="d" * 64, parameters={})
    second = ReportJob.objects.create(requested_by=admin, fingerprint="e" * 64, parameters={}, status=ReportJob.Status.COMPLETED, result={"time_buckets": [{"period": "2026-01-01", "total_tickets": 1, "by_status": {"open": 1}, "response_time": {"responded_tickets": 0, "average_seconds": None}}], "agent_activity": []}, expires_at=django_timezone.now() + timedelta(hours=1))
    denied = client_for(customer)
    assert denied.get("/api/reports").status_code == 403
    assert denied.get(f"/api/reports/{second.id}/download?format=csv").status_code == 403

    client = client_for(admin)
    listed = client.get("/api/reports", {"status": "completed", "limit": 1})
    assert listed.status_code == 200
    assert [item["id"] for item in listed.data["results"]] == [second.id]
    assert listed.data["next_cursor"] is None
    json_download = client.get(f"/api/reports/{second.id}/download")
    assert json_download.status_code == 200
    assert json_download["Content-Disposition"].endswith(f"report-{second.id}.json\"")
    csv_download = client.get(f"/api/reports/{second.id}/download?format=csv")
    assert csv_download.status_code == 200
    assert csv_download["Content-Type"].startswith("text/csv")
    assert "time_bucket,2026-01-01" in csv_download.content.decode()
    assert first.id != second.id


@pytest.mark.django_db
@override_settings(REPORT_RESULT_TTL_SECONDS=1)
def test_expired_result_is_removed_from_cache_and_requires_refresh(admin):
    job, created = request_report(user=admin, params={"start": "2026-01-01", "end": "2026-01-01"})
    assert created is True
    ReportJob.objects.filter(id=job.id).update(
        status=ReportJob.Status.COMPLETED,
        result={"fictional": "result"},
        expires_at=django_timezone.now() - timedelta(seconds=1),
    )
    cache.set(report_cache_key(job.id), {"fictional": "result"})

    assert expire_report_results.run() == {"expired": 1}
    job.refresh_from_db()
    assert job.status == ReportJob.Status.EXPIRED
    assert job.result is None
    assert cache.get(report_cache_key(job.id)) is None
    assert client_for(admin).get(f"/api/reports/{job.id}").status_code == 410
    fresh, created = request_report(user=admin, params={"start": "2026-01-01", "end": "2026-01-01"})
    assert created is True and fresh.id != job.id


@pytest.mark.django_db
def test_refresh_policy_creates_new_work_while_valid_result_is_reused_otherwise(admin):
    job, created = request_report(user=admin, params={"start": "2026-02-01", "end": "2026-02-01"})
    assert created is True
    ReportJob.objects.filter(id=job.id).update(
        status=ReportJob.Status.COMPLETED,
        result={"fictional": True},
        expires_at=django_timezone.now() + timedelta(hours=1),
    )
    reused, created = request_report(user=admin, params={"start": "2026-02-01", "end": "2026-02-01"})
    assert (reused.id, created) == (job.id, False)
    refreshed, created = request_report(user=admin, params={"start": "2026-02-01", "end": "2026-02-01", "refresh": True})
    assert created is True and refreshed.id != job.id
