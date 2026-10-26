from django.contrib.auth.models import AbstractUser
from django.db import models


class User(AbstractUser):
    class Role(models.TextChoices):
        CUSTOMER = "customer", "Customer"
        AGENT = "agent", "Support agent"
        ADMIN = "admin", "Administrator"
    role = models.CharField(max_length=16, choices=Role.choices, default=Role.CUSTOMER)

    def save(self, *args, **kwargs):
        # Application administrators may enter Django admin, but only an
        # explicitly created superuser receives Django's unrestricted perms.
        # A staff/superuser flag can never elevate a customer or an agent.
        if self.is_superuser:
            self.role = self.Role.ADMIN
            self.is_staff = True
        elif self.role == self.Role.ADMIN:
            self.is_staff = True
        else:
            self.is_staff = False
        # Do not let a partial save persist a role change without its linked
        # Django privilege flags (or vice versa).
        if kwargs.get("update_fields") is not None:
            kwargs["update_fields"] = set(kwargs["update_fields"]) | {"role", "is_staff", "is_superuser"}
        super().save(*args, **kwargs)


class ExpiringToken(models.Model):
    """A hashed, short-lived API credential. The plaintext is never stored."""

    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name="api_tokens")
    key_digest = models.CharField(max_length=64, unique=True)
    expires_at = models.DateTimeField()
    revoked_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    last_used_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        indexes = [models.Index(fields=["expires_at"]), models.Index(fields=["user", "revoked_at"])]


class LoginRateLimit(models.Model):
    """Privacy-preserving per-address login budget; no raw address is kept."""

    fingerprint = models.CharField(max_length=64, unique=True)
    window_started_at = models.DateTimeField()
    attempts = models.PositiveSmallIntegerField(default=0)
