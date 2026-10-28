"""Report request, aggregation, and materialized-summary services.

All report periods are UTC. This avoids server-local-time changes moving a
ticket between buckets and makes a rerun over fixed source data reproducible.
"""

from __future__ import annotations

import hashlib
import json
from datetime import date, datetime, time, timedelta, timezone as datetime_timezone

from django.db import IntegrityError, transaction
from django.db.models import OuterRef, Subquery
from django.db.models.functions import TruncDate, TruncMonth
from django.utils import timezone
from rest_framework.exceptions import ValidationError

from apps.desk.models import ConversationMessage, Ticket
from .models import AnalyticsSummary, ReportJob


UTC = datetime_timezone.utc
_REPORT_FILTERS = {"start", "end", "status", "statuses", "agent_id", "granularity", "refresh"}


def _date_value(value, field: str) -> date:
    if isinstance(value, date) and not isinstance(value, datetime):
        return value
    if not isinstance(value, str):
        raise ValidationError({field: ["Use an ISO-8601 date (YYYY-MM-DD)."]})
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise ValidationError({field: ["Use an ISO-8601 date (YYYY-MM-DD)."]}) from exc


def normalize_report_params(params: dict | None) -> dict:
    """Canonicalise supported filters before fingerprinting a report request."""
    if params is None:
        params = {}
    if not isinstance(params, dict):
        raise ValidationError({"detail": ["Report parameters must be an object."]})
    extensions = set(params) - _REPORT_FILTERS
    if extensions:
        raise ValidationError({"detail": [f"Unsupported report parameters: {', '.join(sorted(extensions))}."]})

    normalized: dict[str, object] = {"granularity": params.get("granularity", "day")}
    if normalized["granularity"] not in {"day", "month"}:
        raise ValidationError({"granularity": ["Use 'day' or 'month'."]})
    start = _date_value(params["start"], "start") if params.get("start") else None
    end = _date_value(params["end"], "end") if params.get("end") else None
    if start and end and start > end:
        raise ValidationError({"end": ["Must not be before start."]})
    if start:
        normalized["start"] = start.isoformat()
    if end:
        normalized["end"] = end.isoformat()

    raw_statuses = params.get("statuses", params.get("status"))
    if raw_statuses not in (None, ""):
        if isinstance(raw_statuses, str):
            statuses = [value.strip() for value in raw_statuses.split(",") if value.strip()]
        elif isinstance(raw_statuses, (list, tuple)):
            statuses = [str(value).strip() for value in raw_statuses if str(value).strip()]
        else:
            raise ValidationError({"statuses": ["Use a comma-separated string or a list."]})
        if not statuses or any(value not in Ticket.Status.values for value in statuses):
            raise ValidationError({"statuses": [f"Use one or more of: {', '.join(Ticket.Status.values)}."]})
        normalized["statuses"] = sorted(set(statuses))

    if params.get("agent_id") not in (None, ""):
        try:
            agent_id = int(params["agent_id"])
        except (TypeError, ValueError) as exc:
            raise ValidationError({"agent_id": ["A positive whole number is required."]}) from exc
        if agent_id < 1:
            raise ValidationError({"agent_id": ["A positive whole number is required."]})
        normalized["agent_id"] = agent_id

    refresh = params.get("refresh", False)
    if isinstance(refresh, str):
        if refresh.lower() not in {"true", "false"}:
            raise ValidationError({"refresh": ["Use true or false."]})
        refresh = refresh.lower() == "true"
    if not isinstance(refresh, bool):
        raise ValidationError({"refresh": ["Use true or false."]})
    if refresh:
        normalized["refresh"] = True
    return normalized


def _digest(params: dict) -> str:
    return hashlib.sha256(json.dumps(params, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def report_cache_key(job_id: int) -> str:
    return f"reports:result:{job_id}"


def request_report(*, user, params: dict) -> tuple[ReportJob, bool]:
    params = normalize_report_params(params)
    digest, now = _digest(params), timezone.now()
    existing = ReportJob.objects.filter(
        fingerprint=digest,
        status__in=[ReportJob.Status.QUEUED, ReportJob.Status.PROCESSING],
    ).order_by("-id").first()
    if existing:
        return existing, False
    if not params.get("refresh"):
        existing = ReportJob.objects.filter(
            fingerprint=digest, status=ReportJob.Status.COMPLETED, expires_at__gt=now,
        ).order_by("-id").first()
        if existing:
            return existing, False

    with transaction.atomic():
        try:
            with transaction.atomic():
                job = ReportJob.objects.create(requested_by=user, fingerprint=digest, parameters=params)
        except IntegrityError:
            job = ReportJob.objects.filter(
                fingerprint=digest,
                status__in=[ReportJob.Status.QUEUED, ReportJob.Status.PROCESSING],
            ).order_by("-id").first()
            if job is None and not params.get("refresh"):
                job = ReportJob.objects.filter(
                    fingerprint=digest, status=ReportJob.Status.COMPLETED, expires_at__gt=now,
                ).order_by("-id").first()
            if job is None:
                raise
            return job, False

        from .tasks import enqueue_report_dispatch, wake_outbox_publisher
        enqueue_report_dispatch(job.id)
        transaction.on_commit(wake_outbox_publisher)
        return job, True


def _source_queryset(params: dict):
    queryset = Ticket.objects.all()
    if params.get("start"):
        queryset = queryset.filter(created_at__gte=datetime.combine(date.fromisoformat(params["start"]), time.min, tzinfo=UTC))
    if params.get("end"):
        next_day = date.fromisoformat(params["end"]) + timedelta(days=1)
        queryset = queryset.filter(created_at__lt=datetime.combine(next_day, time.min, tzinfo=UTC))
    if params.get("statuses"):
        queryset = queryset.filter(status__in=params["statuses"])
    if params.get("agent_id"):
        queryset = queryset.filter(assigned_agent_id=params["agent_id"])
    return queryset


def _metrics_for(params: dict, *, include_bucket_agent_activity: bool = False) -> dict:
    """Compute metrics with maintained ORM expressions, never ``.extra()``."""
    bucket = TruncMonth("created_at", tzinfo=UTC) if params["granularity"] == "month" else TruncDate("created_at", tzinfo=UTC)
    first_response = ConversationMessage.objects.filter(
        ticket_id=OuterRef("pk"), sender_id=OuterRef("assigned_agent_id"),
        created_at__gte=OuterRef("created_at"),
    ).order_by("created_at", "id").values("created_at")[:1]
    rows = _source_queryset(params).annotate(
        bucket_start=bucket, first_response_at=Subquery(first_response),
    ).values("id", "bucket_start", "status", "assigned_agent_id", "created_at", "first_response_at").order_by("bucket_start", "id")

    buckets: dict[date, dict] = {}
    agents: dict[int, dict] = {}
    for row in rows:
        period = row["bucket_start"].date() if isinstance(row["bucket_start"], datetime) else row["bucket_start"]
        metric = buckets.setdefault(period, {"period": period.isoformat(), "total_tickets": 0, "by_status": {status: 0 for status in Ticket.Status.values}, "_response_seconds": [], "_agents": {}})
        metric["total_tickets"] += 1
        metric["by_status"][row["status"]] += 1
        response_seconds = None
        if row["first_response_at"] is not None:
            response_seconds = max(0.0, (row["first_response_at"] - row["created_at"]).total_seconds())
            metric["_response_seconds"].append(response_seconds)
        if row["assigned_agent_id"] is not None:
            activity = agents.setdefault(row["assigned_agent_id"], {"agent_id": row["assigned_agent_id"], "assigned_tickets": 0, "resolved_tickets": 0, "responded_tickets": 0, "_response_seconds": []})
            bucket_activity = metric["_agents"].setdefault(row["assigned_agent_id"], {"agent_id": row["assigned_agent_id"], "assigned_tickets": 0, "resolved_tickets": 0, "responded_tickets": 0, "_response_seconds": []})
            activity["assigned_tickets"] += 1
            bucket_activity["assigned_tickets"] += 1
            if row["status"] == Ticket.Status.RESOLVED:
                activity["resolved_tickets"] += 1
                bucket_activity["resolved_tickets"] += 1
            if response_seconds is not None:
                activity["responded_tickets"] += 1
                activity["_response_seconds"].append(response_seconds)
                bucket_activity["responded_tickets"] += 1
                bucket_activity["_response_seconds"].append(response_seconds)

    def finish(metric: dict) -> dict:
        seconds = metric.pop("_response_seconds")
        metric["response_time"] = {"responded_tickets": len(seconds), "average_seconds": round(sum(seconds) / len(seconds), 3) if seconds else None}
        return metric

    result_buckets = []
    for key in sorted(buckets):
        bucket_metric = buckets[key]
        bucket_agents = bucket_metric.pop("_agents")
        finished_bucket = finish(bucket_metric)
        if include_bucket_agent_activity:
            finished_bucket["agent_activity"] = [finish(bucket_agents[agent_id]) for agent_id in sorted(bucket_agents)]
        result_buckets.append(finished_bucket)
    result_agents = [finish(agents[key]) for key in sorted(agents)]
    return {
        "aggregation_timezone": "UTC", "granularity": params["granularity"],
        "filters": {key: value for key, value in params.items() if key != "refresh"},
        "time_buckets": result_buckets, "agent_activity": result_agents,
        "total_tickets": sum(item["total_tickets"] for item in result_buckets),
    }


def compute_report(job: ReportJob) -> dict:
    result = _metrics_for(normalize_report_params(job.parameters))
    result["generated_at"] = timezone.now().isoformat()
    return result


def compute_precomputed_summaries(job: ReportJob) -> list[tuple[str, date, dict]]:
    """Build global daily/monthly buckets for the period a report touched."""
    parameters = normalize_report_params(job.parameters)
    base = {key: value for key, value in parameters.items() if key not in {"statuses", "agent_id", "refresh", "granularity"}}
    return compute_summary_rows(base)


def compute_summary_rows(base: dict) -> list[tuple[str, date, dict]]:
    """Return global materialized rows with activity scoped to each bucket."""
    summaries = []
    for granularity in (AnalyticsSummary.Granularity.DAY, AnalyticsSummary.Granularity.MONTH):
        selected = _metrics_for({**base, "granularity": granularity}, include_bucket_agent_activity=True)
        for selected_bucket in selected["time_buckets"]:
            period_start = date.fromisoformat(selected_bucket["period"])
            bucket = dict(selected_bucket)
            summaries.append((granularity, period_start, bucket))
    return summaries


def persist_precomputed_summaries(summaries: list[tuple[str, date, dict]]) -> None:
    for granularity, period_start, metrics in summaries:
        AnalyticsSummary.objects.update_or_create(granularity=granularity, period_start=period_start, defaults={"metrics": metrics})


def invalidate_analytics_summaries(ticket_ids: list[int]) -> int:
    """Invalidate buckets affected by committed ticket/message changes."""
    if not ticket_ids:
        return 0
    periods = set()
    for created_at in Ticket.objects.filter(id__in=set(ticket_ids)).values_list("created_at", flat=True):
        value = created_at.astimezone(UTC).date()
        periods.add((AnalyticsSummary.Granularity.DAY, value))
        periods.add((AnalyticsSummary.Granularity.MONTH, value.replace(day=1)))
    if not periods:
        return 0
    from django.db.models import Q
    query = Q()
    for granularity, period in periods:
        query |= Q(granularity=granularity, period_start=period)
    deleted, _ = AnalyticsSummary.objects.filter(query).delete()
    return deleted
