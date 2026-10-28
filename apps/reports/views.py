import csv

from django.core.cache import cache
from django.http import HttpResponse
from django.shortcuts import get_object_or_404
from django.utils import timezone
from rest_framework.decorators import api_view, renderer_classes
from rest_framework.exceptions import ValidationError
from rest_framework.renderers import BaseRenderer, JSONRenderer
from rest_framework.response import Response
from drf_spectacular.utils import OpenApiTypes, extend_schema

from apps.desk.pagination import decode_cursor, encode_cursor, page_size
from .models import ReportJob
from .services import report_cache_key, request_report


class CSVNegotiationRenderer(BaseRenderer):
    """Allows ``?format=csv`` to reach the explicit CSV download response."""

    media_type = "text/csv"
    format = "csv"
    charset = "utf-8"

    def render(self, data, accepted_media_type=None, renderer_context=None):
        # Error responses remain compact and machine-readable enough for this
        # download endpoint; successful CSV is returned as HttpResponse below.
        return str(data or "").encode(self.charset)


def require_admin(request):
    return request.user.role == "admin"


def _forbidden():
    return Response({"detail": "Administrator role required", "code": "role_forbidden"}, status=403)


def _serialize_job(job: ReportJob):
    return {
        "id": job.id,
        "status": job.status,
        "attempts": job.attempts,
        "parameters": job.parameters,
        "created_at": job.created_at,
        "completed_at": job.completed_at,
        "expires_at": job.expires_at,
        "error": job.error,
    }


def _expired(job: ReportJob) -> bool:
    return job.status == ReportJob.Status.EXPIRED or (
        job.status == ReportJob.Status.COMPLETED
        and (job.expires_at is None or job.expires_at <= timezone.now())
    )


def _result_or_expired(job: ReportJob):
    if _expired(job):
        return None
    if job.status != ReportJob.Status.COMPLETED:
        return None
    try:
        result = cache.get(report_cache_key(job.id))
        if result is None:
            result = job.result
            if result is not None:
                remaining = max(1, int((job.expires_at - timezone.now()).total_seconds())) if job.expires_at else None
                cache.set(report_cache_key(job.id), result, timeout=remaining)
        return result
    except Exception:
        return job.result


def _paginated_reports(request):
    limit = page_size(request.query_params.get("limit"))
    cursor = decode_cursor(request.query_params.get("cursor"), kind="reports")
    queryset = ReportJob.objects.all()
    status = request.query_params.get("status")
    if status:
        if status not in ReportJob.Status.values:
            raise ValidationError({"status": ["Unknown report status."]})
        queryset = queryset.filter(status=status)
    requested_by = request.query_params.get("requested_by")
    if requested_by:
        try:
            queryset = queryset.filter(requested_by_id=int(requested_by))
        except ValueError as exc:
            raise ValidationError({"requested_by": ["A whole number is required."]}) from exc
    if cursor:
        try:
            created_at, job_id = cursor["created_at"], int(cursor["id"])
        except (KeyError, TypeError, ValueError) as exc:
            raise ValidationError({"cursor": ["Invalid cursor."]}) from exc
        from django.db.models import Q
        queryset = queryset.filter(Q(created_at__lt=created_at) | Q(created_at=created_at, id__lt=job_id))
    rows = list(queryset.order_by("-created_at", "-id")[: limit + 1])
    more, rows = len(rows) > limit, rows[:limit]
    next_cursor = None
    if more and rows:
        tail = rows[-1]
        next_cursor = encode_cursor({"kind": "reports", "created_at": tail.created_at.isoformat(), "id": tail.id})
    return Response({"results": [_serialize_job(job) for job in rows], "next_cursor": next_cursor})


@extend_schema(methods=["GET"], operation_id="report_list", request=None, responses={200: OpenApiTypes.OBJECT})
@extend_schema(methods=["POST"], operation_id="report_create", request=OpenApiTypes.OBJECT, responses={202: OpenApiTypes.OBJECT})
@api_view(["GET", "POST"])
def reports(request):
    if not require_admin(request):
        return _forbidden()
    if request.method == "GET":
        return _paginated_reports(request)
    job, queued = request_report(user=request.user, params=request.data or {})
    return Response({"id": job.id, "status": job.status, "created": queued, "expires_at": job.expires_at}, status=202)


@extend_schema(operation_id="report_detail", request=None, responses={200: OpenApiTypes.OBJECT})
@api_view(["GET"])
def report_detail(request, job_id):
    if not require_admin(request):
        return _forbidden()
    job = get_object_or_404(ReportJob, id=job_id)
    if _expired(job):
        return Response({"detail": "Report result has expired; request a refresh.", "code": "report_expired"}, status=410)
    result = _result_or_expired(job)
    return Response({**_serialize_job(job), "result": result})


@extend_schema(operation_id="report_download", request=None, responses={200: OpenApiTypes.OBJECT})
@api_view(["GET"])
@renderer_classes([JSONRenderer, CSVNegotiationRenderer])
def report_download(request, job_id):
    if not require_admin(request):
        return _forbidden()
    job = get_object_or_404(ReportJob, id=job_id)
    if _expired(job):
        return Response({"detail": "Report result has expired; request a refresh.", "code": "report_expired"}, status=410)
    result = _result_or_expired(job)
    if job.status != ReportJob.Status.COMPLETED or result is None:
        return Response({"detail": "Report is not ready for download.", "code": "report_not_ready"}, status=409)
    export_format = request.query_params.get("format", "json").lower()
    if export_format == "json":
        response = Response(result)
        response["Content-Disposition"] = f'attachment; filename="report-{job.id}.json"'
        return response
    if export_format != "csv":
        raise ValidationError({"format": ["Use json or csv."]})

    response = HttpResponse(content_type="text/csv; charset=utf-8")
    response["Content-Disposition"] = f'attachment; filename="report-{job.id}.csv"'
    writer = csv.writer(response)
    writer.writerow(["scope", "period", "agent_id", "total_tickets", "open", "claimed", "resolved", "responded_tickets", "average_response_seconds", "assigned_tickets", "resolved_tickets"])
    for bucket in result.get("time_buckets", []):
        statuses = bucket.get("by_status", {})
        response_time = bucket.get("response_time", {})
        writer.writerow(["time_bucket", bucket.get("period"), "", bucket.get("total_tickets"), statuses.get("open", 0), statuses.get("claimed", 0), statuses.get("resolved", 0), response_time.get("responded_tickets", 0), response_time.get("average_seconds"), "", ""])
    for activity in result.get("agent_activity", []):
        response_time = activity.get("response_time", {})
        writer.writerow(["agent_activity", "", activity.get("agent_id"), "", "", "", "", activity.get("responded_tickets", 0), response_time.get("average_seconds"), activity.get("assigned_tickets", 0), activity.get("resolved_tickets", 0)])
    return response
