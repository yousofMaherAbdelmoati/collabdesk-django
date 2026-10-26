# Generated manually for DJ-007.

import django.utils.timezone
from django.db import migrations, models


def preserve_existing_published_events(apps, schema_editor):
    OutboxEvent = apps.get_model("desk", "OutboxEvent")
    OutboxEvent.objects.filter(published_at__isnull=False).update(state="published")


class Migration(migrations.Migration):

    dependencies = [
        ("desk", "0002_conversationreadreceipt"),
    ]

    operations = [
        migrations.AddField(
            model_name="outboxevent",
            name="available_at",
            field=models.DateTimeField(default=django.utils.timezone.now),
        ),
        migrations.AddField(
            model_name="outboxevent",
            name="failed_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="outboxevent",
            name="last_error",
            field=models.TextField(blank=True),
        ),
        migrations.AddField(
            model_name="outboxevent",
            name="lease_expires_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="outboxevent",
            name="lease_token",
            field=models.UUIDField(blank=True, editable=False, null=True),
        ),
        migrations.AddField(
            model_name="outboxevent",
            name="state",
            field=models.CharField(choices=[("pending", "Pending"), ("publishing", "Publishing"), ("published", "Published"), ("failed", "Failed")], default="pending", max_length=12),
        ),
        migrations.RunPython(preserve_existing_published_events, migrations.RunPython.noop),
        migrations.AddIndex(
            model_name="outboxevent",
            index=models.Index(fields=["state", "available_at"], name="desk_outbox_state_726014_idx"),
        ),
    ]
