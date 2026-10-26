from datetime import datetime, timedelta, timezone
from itertools import islice

import pytest
from django.core.cache import caches
from django.contrib.auth import get_user_model
from django.test.utils import override_settings
from django.utils import timezone as django_timezone

from apps.desk.models import ConversationMessage, Ticket
from apps.reports.benchmarking import generate_seed_tickets, run_benchmark
from apps.reports.models import ReportJob
from apps.reports.services import compute_report, normalize_report_params


@pytest.mark.unit
def test_seed_generator_is_repeatable_and_explicitly_limited_to_supported_sizes():
    first = list(islice(generate_seed_tickets(size=10_000, seed=12, namespace="repeatable"), 4))
    second = list(islice(generate_seed_tickets(size=10_000, seed=12, namespace="repeatable"), 4))

    assert first == second
    assert first[0].title == "DJ-012 benchmark repeatable ticket 000000"
    assert first[0].created_at.year == 2024
    with pytest.raises(ValueError, match="size must be one of"):
        list(generate_seed_tickets(size=3, seed=12, namespace="repeatable"))


@pytest.mark.django_db
def test_benchmark_records_live_and_precomputed_query_cache_and_latency_data():
    User = get_user_model()
    admin = User.objects.create_user(username="benchmark-admin", password="x", role=User.Role.ADMIN)
    customer = User.objects.create_user(username="benchmark-customer", password="x", role=User.Role.CUSTOMER)
    agent = User.objects.create_user(username="benchmark-agent", password="x", role=User.Role.AGENT)
    created = datetime(2024, 2, 3, 10, tzinfo=timezone.utc)
    ticket = Ticket.objects.create(customer=customer, assigned_agent=agent, title="fictional benchmark", description="fictional", status=Ticket.Status.RESOLVED)
    Ticket.objects.filter(id=ticket.id).update(created_at=created, updated_at=created)
    message = ConversationMessage.objects.create(ticket=ticket, sender=agent, body="fictional response")
    ConversationMessage.objects.filter(id=message.id).update(created_at=created + timedelta(seconds=60))
    params = normalize_report_params({"start": "2024-01-01", "end": "2024-12-31", "agent_id": agent.id, "granularity": "month"})
    job = ReportJob.objects.create(
        requested_by=admin,
        fingerprint="f" * 64,
        parameters=params,
        status=ReportJob.Status.COMPLETED,
        expires_at=django_timezone.now() + timedelta(hours=1),
    )
    job.result = compute_report(job)
    job.save(update_fields=["result"])

    locmem = {"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache", "LOCATION": "test-dj012"}}
    with override_settings(CACHES=locmem):
        caches.close_all()
        result = run_benchmark(job=job, warmups=1, iterations=2)
    caches.close_all()

    assert result["live_aggregation"]["query_count"]["total"] >= 2
    assert result["precomputed_result_retrieval"]["cache"] == {"hits": 1, "misses": 1}
    assert len(result["live_aggregation"]["samples"]) == 2
    assert result["resource_use"]["process_max_rss_kb"] > 0
