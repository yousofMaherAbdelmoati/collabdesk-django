# Generated manually for DJ-008.

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("reports", "0002_reportjob_leases_and_active_uniqueness"),
    ]

    operations = [
        migrations.CreateModel(
            name="AnalyticsSummary",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("granularity", models.CharField(choices=[("day", "Day"), ("month", "Month")], max_length=8)),
                ("period_start", models.DateField()),
                ("metrics", models.JSONField(default=dict)),
                ("calculated_at", models.DateTimeField(auto_now=True)),
            ],
        ),
        migrations.AddField(
            model_name="reportjob",
            name="expires_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AlterField(
            model_name="reportjob",
            name="status",
            field=models.CharField(choices=[("queued", "Queued"), ("processing", "Processing"), ("completed", "Completed"), ("failed", "Failed"), ("expired", "Expired")], default="queued", max_length=12),
        ),
        migrations.AddIndex(
            model_name="reportjob",
            index=models.Index(fields=["expires_at"], name="reports_rep_expires_5d2b8c_idx"),
        ),
        migrations.AddIndex(
            model_name="analyticssummary",
            index=models.Index(fields=["granularity", "period_start"], name="reports_ana_granula_598742_idx"),
        ),
        migrations.AddConstraint(
            model_name="analyticssummary",
            constraint=models.UniqueConstraint(fields=("granularity", "period_start"), name="unique_analytics_summary_bucket"),
        ),
    ]
