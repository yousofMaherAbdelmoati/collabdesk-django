# Generated manually for DJ-007.

import django.db.models
import django.utils.timezone
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("reports", "0001_initial"),
    ]

    operations = [
        migrations.AddField(
            model_name="reportjob",
            name="heartbeat_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="reportjob",
            name="lease_expires_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="reportjob",
            name="lease_token",
            field=models.UUIDField(blank=True, editable=False, null=True),
        ),
        migrations.AddField(
            model_name="reportjob",
            name="next_attempt_at",
            field=models.DateTimeField(default=django.utils.timezone.now),
        ),
        migrations.AddIndex(
            model_name="reportjob",
            index=models.Index(fields=["status", "next_attempt_at"], name="reports_rep_status_fd43e7_idx"),
        ),
        migrations.AddConstraint(
            model_name="reportjob",
            constraint=models.UniqueConstraint(
                condition=models.Q(("status__in", ["queued", "processing"])),
                fields=("fingerprint",),
                name="unique_active_report_fingerprint",
            ),
        ),
    ]
