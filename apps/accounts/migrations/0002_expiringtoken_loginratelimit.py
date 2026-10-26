# Generated manually for the authentication hardening task.
from django.db import migrations, models
import django.db.models.deletion


def normalize_existing_users(apps, schema_editor):
    User = apps.get_model("accounts", "User")
    User.objects.filter(is_superuser=True).update(role="admin", is_staff=True)
    User.objects.filter(role="admin").update(is_staff=True)
    User.objects.exclude(role="admin").update(is_staff=False, is_superuser=False)


class Migration(migrations.Migration):
    dependencies = [("accounts", "0001_initial")]

    operations = [
        migrations.CreateModel(
            name="LoginRateLimit",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("fingerprint", models.CharField(max_length=64, unique=True)),
                ("window_started_at", models.DateTimeField()),
                ("attempts", models.PositiveSmallIntegerField(default=0)),
            ],
        ),
        migrations.CreateModel(
            name="ExpiringToken",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("key_digest", models.CharField(max_length=64, unique=True)),
                ("expires_at", models.DateTimeField()),
                ("revoked_at", models.DateTimeField(blank=True, null=True)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("last_used_at", models.DateTimeField(blank=True, null=True)),
                ("user", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="api_tokens", to="accounts.user")),
            ],
        ),
        migrations.AddIndex(model_name="expiringtoken", index=models.Index(fields=["expires_at"], name="accounts_ex_expires_96dd10_idx")),
        migrations.AddIndex(model_name="expiringtoken", index=models.Index(fields=["user", "revoked_at"], name="accounts_ex_user_id_476fad_idx")),
        migrations.RunPython(
            code=normalize_existing_users,
            reverse_code=migrations.RunPython.noop,
        ),
    ]
