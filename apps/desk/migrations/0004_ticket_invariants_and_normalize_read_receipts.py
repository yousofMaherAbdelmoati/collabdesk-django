from django.db import migrations, models
from django.db.models import Q


class Migration(migrations.Migration):

    dependencies = [
        ("desk", "0003_outbox_delivery_leases"),
    ]

    operations = [
        migrations.RemoveIndex(
            model_name="conversationreadreceipt",
            name="desk_conver_ticket__b6b315_idx",
        ),
        migrations.RemoveField(
            model_name="conversationreadreceipt",
            name="ticket",
        ),
        migrations.AddConstraint(
            model_name="ticket",
            constraint=models.CheckConstraint(
                condition=(
                    Q(status="open", assigned_agent__isnull=True)
                    | Q(status__in=["claimed", "resolved"], assigned_agent__isnull=False)
                ),
                name="ticket_status_requires_matching_assignment",
            ),
        ),
    ]
