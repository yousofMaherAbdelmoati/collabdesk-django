"""Deterministic, local-only fixtures and measurement helpers for DJ-012.

The benchmark data is deliberately identifiable by its title/body prefix and
dedicated accounts.  It is appended to the selected database; this module
never deletes or resets tables.
"""

from __future__ import annotations

import hashlib
import os
import platform
import random
import resource
import sys
import time
import uuid
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone as datetime_timezone
from pathlib import Path

import django
from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.cache import caches
from django.db import connection, reset_queries
from django.utils import timezone

from apps.desk.models import ConversationMessage, Ticket
from .models import ReportJob
from .services import compute_report, normalize_report_params, report_cache_key


SUPPORTED_SIZES = (10_000, 50_000, 100_000)
BENCHMARK_YEAR = 2024
_UTC = datetime_timezone.utc


@dataclass(frozen=True)
class SeedTicket:
    sequence: int
    title: str
    description: str
    status: str
    created_at: datetime
    has_response: bool
    response_at: datetime | None
    message_id: uuid.UUID | None


def validate_namespace(namespace: str) -> str:
    """Return a stable namespace safe for account names and fixture markers."""
    if not namespace or len(namespace) > 32:
        raise ValueError("namespace must contain 1 to 32 characters")
    if not all(character.isascii() and (character.isalnum() or character == "-") for character in namespace):
        raise ValueError("namespace may contain only ASCII letters, numbers, and hyphens")
    return namespace.lower()


def fixture_prefix(namespace: str) -> str:
    return f"DJ-012 benchmark {validate_namespace(namespace)}"


def fixture_accounts(namespace: str) -> tuple[str, str, str]:
    namespace = validate_namespace(namespace)
    return (
        f"dj012-{namespace}-customer",
        f"dj012-{namespace}-agent",
        f"dj012-{namespace}-admin",
    )


def generate_seed_tickets(*, size: int, seed: int, namespace: str) -> Iterator[SeedTicket]:
    """Generate repeatable fictional ticket/message attributes without I/O."""
    if size not in SUPPORTED_SIZES:
        raise ValueError(f"size must be one of: {', '.join(map(str, SUPPORTED_SIZES))}")
    prefix = fixture_prefix(namespace)
    generator = random.Random(seed)
    start = datetime(BENCHMARK_YEAR, 1, 1, tzinfo=_UTC)
    statuses = tuple(Ticket.Status.values)
    for sequence in range(size):
        # The date, status, and response time are all deterministic for the
        # same (size, seed, namespace); the prose intentionally remains fake.
        created_at = start + timedelta(
            days=generator.randrange(366),
            minutes=generator.randrange(24 * 60),
        )
        status = statuses[generator.randrange(len(statuses))]
        has_response = sequence % 3 != 0
        response_at = created_at + timedelta(seconds=30 + generator.randrange(3_600)) if has_response else None
        yield SeedTicket(
            sequence=sequence,
            title=f"{prefix} ticket {sequence:06d}",
            description=f"Fictional benchmark request {sequence:06d}; generated locally only.",
            status=status,
            created_at=created_at,
            has_response=has_response,
            response_at=response_at,
            message_id=(uuid.uuid5(uuid.NAMESPACE_URL, f"dj-012/{namespace}/{seed}/{sequence}") if has_response else None),
        )


def _account(username: str, role: str):
    User = get_user_model()
    user, created = User.objects.get_or_create(username=username, defaults={"role": role})
    if created:
        user.set_unusable_password()
        user.save(update_fields=["password"])
    elif user.role != role:
        raise ValueError(f"account {username!r} already exists with a non-benchmark role")
    return user


def seed_benchmark_tickets(*, size: int, seed: int, namespace: str, batch_size: int = 1_000) -> dict:
    """Append missing tagged fixtures and return an idempotent seed manifest."""
    namespace = validate_namespace(namespace)
    customer_name, agent_name, admin_name = fixture_accounts(namespace)
    customer = _account(customer_name, get_user_model().Role.CUSTOMER)
    agent = _account(agent_name, get_user_model().Role.AGENT)
    admin = _account(admin_name, get_user_model().Role.ADMIN)
    tickets = list(generate_seed_tickets(size=size, seed=seed, namespace=namespace))
    expected_titles = {ticket.title for ticket in tickets}
    existing_title_rows = list(
        Ticket.objects.filter(
            customer=customer, assigned_agent=agent, title__in=expected_titles,
        ).values_list("title", flat=True)
    )
    existing_titles = set(existing_title_rows)
    if len(existing_title_rows) != len(existing_titles):
        raise RuntimeError("duplicate benchmark ticket titles found; choose a fresh namespace")
    missing = [ticket for ticket in tickets if ticket.title not in existing_titles]
    for offset in range(0, len(missing), batch_size):
        batch = missing[offset : offset + batch_size]
        models = [
            Ticket(
                customer=customer,
                assigned_agent=agent,
                title=ticket.title,
                description=ticket.description,
                status=ticket.status,
                created_at=ticket.created_at,
                updated_at=ticket.created_at,
            )
            for ticket in batch
        ]
        Ticket.objects.bulk_create(models, batch_size=batch_size)
        # DateTimeField(auto_now_add=True) owns insertion timestamps, so set
        # the intended deterministic timestamp immediately afterwards.
        for model, ticket in zip(models, batch):
            model.created_at = ticket.created_at
            model.updated_at = ticket.created_at
        Ticket.objects.bulk_update(models, ["created_at", "updated_at"], batch_size=batch_size)

    ticket_rows = list(Ticket.objects.filter(
            customer=customer, assigned_agent=agent, title__in=expected_titles,
        ).values_list("id", "title"))
    ticket_by_title = {title: identifier for identifier, title in ticket_rows}
    if len(ticket_rows) != len(ticket_by_title):
        raise RuntimeError("duplicate benchmark ticket titles found after seeding")
    if len(ticket_by_title) != size:
        raise RuntimeError("benchmark seed does not own exactly the requested ticket set")

    expected_messages = [ticket for ticket in tickets if ticket.has_response]
    message_prefix = f"{fixture_prefix(namespace)} response"
    ticket_ids = [ticket_by_title[ticket.title] for ticket in expected_messages]
    existing_message_ticket_ids = set(
        ConversationMessage.objects.filter(ticket_id__in=ticket_ids, body__startswith=message_prefix)
        .values_list("ticket_id", flat=True)
    )
    missing_messages = [ticket for ticket in expected_messages if ticket_by_title[ticket.title] not in existing_message_ticket_ids]
    for offset in range(0, len(missing_messages), batch_size):
        batch = missing_messages[offset : offset + batch_size]
        models = [
            ConversationMessage(
                ticket_id=ticket_by_title[ticket.title],
                sender=agent,
                client_message_id=ticket.message_id,
                body=f"{message_prefix} {ticket.sequence:06d}; fictional local fixture.",
                created_at=ticket.response_at,
            )
            for ticket in batch
        ]
        ConversationMessage.objects.bulk_create(models, batch_size=batch_size)
        for model, ticket in zip(models, batch):
            model.created_at = ticket.response_at
        ConversationMessage.objects.bulk_update(models, ["created_at"], batch_size=batch_size)

    return {
        "namespace": namespace,
        "size": size,
        "seed": seed,
        "created_tickets": len(missing),
        "reused_tickets": size - len(missing),
        "created_messages": len(missing_messages),
        "reused_messages": len(expected_messages) - len(missing_messages),
        "customer_id": customer.id,
        "agent_id": agent.id,
        "admin_id": admin.id,
        "params": {
            "start": f"{BENCHMARK_YEAR}-01-01",
            "end": f"{BENCHMARK_YEAR}-12-31",
            "agent_id": agent.id,
            "granularity": "month",
        },
    }


def ensure_precomputed_result(*, manifest: dict) -> ReportJob:
    """Persist the fixture's result once; subsequent measurements only retrieve it."""
    params = normalize_report_params(manifest["params"])
    digest = hashlib.sha256(
        ("dj-012-benchmark:" + str(manifest["namespace"]) + ":" + str(manifest["seed"]) + ":" + str(manifest["size"])).encode()
    ).hexdigest()
    job, _ = ReportJob.objects.get_or_create(
        fingerprint=digest,
        defaults={"requested_by_id": manifest["admin_id"], "parameters": params},
    )
    if job.parameters != params:
        raise RuntimeError("existing benchmark report has incompatible parameters")
    if job.result is None or job.expires_at is None or job.expires_at <= timezone.now():
        job.result = compute_report(job)
        job.status = ReportJob.Status.COMPLETED
        job.completed_at = timezone.now()
        job.expires_at = timezone.now() + timedelta(days=1)
        job.save(update_fields=["result", "status", "completed_at", "expires_at"])
    return job


def retrieve_precomputed_result(job_id: int) -> tuple[dict, str]:
    """Use the same cache-aside result shape as the report-detail endpoint."""
    backend = caches["default"]
    key = report_cache_key(job_id)
    result = backend.get(key)
    if result is not None:
        return result, "hit"
    job = ReportJob.objects.only("result", "expires_at").get(id=job_id)
    if job.result is None or job.expires_at is None or job.expires_at <= timezone.now():
        raise RuntimeError("benchmark precomputed result is unavailable")
    backend.set(key, job.result, timeout=max(1, int((job.expires_at - timezone.now()).total_seconds())))
    return job.result, "miss"


def _percentile(values: list[float], percentile: int) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    index = max(0, min(len(ordered) - 1, round((percentile / 100) * (len(ordered) - 1))))
    return round(ordered[index], 3)


def _summarize(samples: list[dict]) -> dict:
    latencies = [sample["wall_ms"] for sample in samples]
    query_counts = [sample["query_count"] for sample in samples]
    return {
        "operations": len(samples),
        "latency_ms": {
            "min": min(latencies) if latencies else 0.0,
            "p50": _percentile(latencies, 50),
            "p95": _percentile(latencies, 95),
            "p99": _percentile(latencies, 99),
            "max": max(latencies) if latencies else 0.0,
        },
        "query_count": {"total": sum(query_counts), "min": min(query_counts, default=0), "max": max(query_counts, default=0)},
        "cache": {
            "hits": sum(sample.get("cache") == "hit" for sample in samples),
            "misses": sum(sample.get("cache") == "miss" for sample in samples),
        },
        "samples": samples,
    }


def _read_text(path: str) -> str | None:
    try:
        return Path(path).read_text().strip() or None
    except OSError:
        return None


def _current_rss_kb() -> int | None:
    status = _read_text("/proc/self/status")
    if status is None:
        return None
    for line in status.splitlines():
        if line.startswith("VmRSS:"):
            try:
                return int(line.split()[1])
            except (IndexError, ValueError):
                return None
    return None


def runtime_configuration() -> dict:
    """Capture non-secret, portable machine/runtime/container facts."""
    return {
        "platform": platform.platform(),
        "python": sys.version.split()[0],
        "django": django.get_version(),
        "database": {"vendor": connection.vendor, "engine": settings.DATABASES["default"]["ENGINE"]},
        "cache_backend": f"{caches['default'].__class__.__module__}.{caches['default'].__class__.__name__}",
        "cpu_count": os.cpu_count(),
        "container": {"dockerenv": Path("/.dockerenv").exists(), "cgroup": _read_text("/proc/self/cgroup")},
        "cgroup_limits": {"cpu_max": _read_text("/sys/fs/cgroup/cpu.max"), "memory_max": _read_text("/sys/fs/cgroup/memory.max")},
    }


def run_benchmark(*, job: ReportJob, warmups: int, iterations: int) -> dict:
    """Measure live aggregation and cache-aside precomputed retrieval."""
    if warmups < 0 or iterations < 2:
        raise ValueError("warmups must be non-negative and iterations must be at least 2")
    backend = caches["default"]
    key = report_cache_key(job.id)
    prior_force_debug = connection.force_debug_cursor
    usage_before = resource.getrusage(resource.RUSAGE_SELF)
    rss_before = _current_rss_kb()
    connection.force_debug_cursor = True
    try:
        # Warm both paths. The measured retrieval then starts deliberately
        # cold, so its hit/miss count demonstrates both outcomes.
        for _ in range(warmups):
            compute_report(job)
        backend.delete(key)
        for _ in range(warmups):
            retrieve_precomputed_result(job.id)

        live_samples = []
        for _ in range(iterations):
            reset_queries()
            started_wall, started_cpu = time.perf_counter(), time.process_time()
            result = compute_report(job)
            live_samples.append({
                "wall_ms": round((time.perf_counter() - started_wall) * 1000, 3),
                "cpu_ms": round((time.process_time() - started_cpu) * 1000, 3),
                "query_count": len(connection.queries),
                "result_total_tickets": result["total_tickets"],
                "cache": "not_applicable",
            })

        backend.delete(key)
        precomputed_samples = []
        for _ in range(iterations):
            reset_queries()
            started_wall, started_cpu = time.perf_counter(), time.process_time()
            result, cache_state = retrieve_precomputed_result(job.id)
            precomputed_samples.append({
                "wall_ms": round((time.perf_counter() - started_wall) * 1000, 3),
                "cpu_ms": round((time.process_time() - started_cpu) * 1000, 3),
                "query_count": len(connection.queries),
                "result_total_tickets": result["total_tickets"],
                "cache": cache_state,
            })
    finally:
        connection.force_debug_cursor = prior_force_debug

    if any(sample["result_total_tickets"] != job.result["total_tickets"] for sample in live_samples + precomputed_samples):
        raise RuntimeError("live and precomputed result totals differ")
    usage_after = resource.getrusage(resource.RUSAGE_SELF)
    return {
        "warmups_per_strategy": warmups,
        "iterations_per_strategy": iterations,
        "live_aggregation": _summarize(live_samples),
        "precomputed_result_retrieval": _summarize(precomputed_samples),
        "resource_use": {
            "measurement_user_cpu_seconds": round(usage_after.ru_utime - usage_before.ru_utime, 6),
            "measurement_system_cpu_seconds": round(usage_after.ru_stime - usage_before.ru_stime, 6),
            "rss_kb_before": rss_before,
            "rss_kb_after": _current_rss_kb(),
            "process_max_rss_kb": usage_after.ru_maxrss,
        },
    }
