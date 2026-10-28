from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("reports", "0003_analyticssummary_reportjob_expires_at_and_more"),
    ]

    operations = [
        migrations.AddIndex(
            model_name="reportjob",
            index=models.Index(fields=["created_at"], name="reports_rep_created_41b0f8_idx"),
        ),
    ]
