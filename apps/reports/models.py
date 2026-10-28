from django.conf import settings
from django.db import models
from django.db.models import Q
from django.utils import timezone

class ReportJob(models.Model):
    class Status(models.TextChoices):
        QUEUED = "queued", "Queued"; PROCESSING = "processing", "Processing"; COMPLETED = "completed", "Completed"; FAILED = "failed", "Failed"; EXPIRED = "expired", "Expired"
    requested_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)
    fingerprint = models.CharField(max_length=64)
    parameters = models.JSONField(default=dict)
    status = models.CharField(max_length=12, choices=Status.choices, default=Status.QUEUED)
    attempts = models.PositiveIntegerField(default=0)
    result = models.JSONField(null=True)
    error = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    started_at = models.DateTimeField(null=True); completed_at = models.DateTimeField(null=True)
    # Completed output is retained for a bounded period.  The database remains
    # authoritative while Redis is only a convenience cache for that period.
    expires_at = models.DateTimeField(null=True, blank=True)
    # A task owns a report only while it holds this lease.  The token prevents
    # a worker that woke up after a lease expiry from overwriting a newer run.
    lease_token = models.UUIDField(null=True, blank=True, editable=False)
    lease_expires_at = models.DateTimeField(null=True, blank=True)
    heartbeat_at = models.DateTimeField(null=True, blank=True)
    next_attempt_at = models.DateTimeField(default=timezone.now)
    class Meta:
        indexes = [
            models.Index(fields=["status", "created_at"]),
            models.Index(fields=["fingerprint", "status"]),
            models.Index(fields=["status", "next_attempt_at"], name="reports_rep_status_fd43e7_idx"),
            models.Index(fields=["expires_at"], name="reports_rep_expires_5d2b8c_idx"),
            models.Index(fields=["created_at"], name="reports_rep_created_41b0f8_idx"),
        ]
        constraints = [
            # This is the race-free part of de-duplication. Application-level
            # lookup alone cannot prevent two concurrent requests from both
            # creating an active equivalent report.
            models.UniqueConstraint(
                fields=["fingerprint"],
                condition=Q(status__in=["queued", "processing"]),
                name="unique_active_report_fingerprint",
            ),
        ]


class AnalyticsSummary(models.Model):
    """Materialized, UTC analytics buckets used for dashboard/report refreshes.

    ``metrics`` deliberately contains only aggregate values and numeric agent
    identifiers.  It is invalidated when source tickets/messages change and is
    rebuilt by a report worker or the scheduled refresh task.
    """

    class Granularity(models.TextChoices):
        DAY = "day", "Day"
        MONTH = "month", "Month"

    granularity = models.CharField(max_length=8, choices=Granularity.choices)
    period_start = models.DateField()
    metrics = models.JSONField(default=dict)
    calculated_at = models.DateTimeField(auto_now=True)

    class Meta:
        indexes = [models.Index(fields=["granularity", "period_start"], name="reports_ana_granula_598742_idx")]
        constraints = [
            models.UniqueConstraint(
                fields=["granularity", "period_start"],
                name="unique_analytics_summary_bucket",
            ),
        ]
