import uuid
from django.conf import settings
from django.db import models
from django.utils import timezone

class Ticket(models.Model):
    class Status(models.TextChoices):
        OPEN = "open", "Open"
        CLAIMED = "claimed", "Claimed"
        RESOLVED = "resolved", "Resolved"
    customer = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="tickets")
    assigned_agent = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT, related_name="assigned_tickets")
    title = models.CharField(max_length=160)
    description = models.TextField(max_length=5000)
    status = models.CharField(max_length=12, choices=Status.choices, default=Status.OPEN)
    version = models.PositiveIntegerField(default=1)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    class Meta:
        indexes = [models.Index(fields=["status", "created_at"]), models.Index(fields=["customer", "created_at"])]

class ConversationMessage(models.Model):
    ticket = models.ForeignKey(Ticket, on_delete=models.CASCADE, related_name="messages")
    sender = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)
    client_message_id = models.UUIDField(default=uuid.uuid4)
    body = models.TextField(max_length=4000)
    created_at = models.DateTimeField(auto_now_add=True)
    read_at = models.DateTimeField(null=True, blank=True)
    class Meta:
        constraints = [models.UniqueConstraint(fields=["ticket", "client_message_id"], name="unique_ticket_client_message")]
        indexes = [models.Index(fields=["ticket", "id"])]


class ConversationReadReceipt(models.Model):
    """A durable, per-reader acknowledgement for a conversation message."""
    ticket = models.ForeignKey(Ticket, on_delete=models.CASCADE, related_name="read_receipts")
    message = models.ForeignKey(ConversationMessage, on_delete=models.CASCADE, related_name="read_receipts")
    reader = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="message_read_receipts")
    read_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["message", "reader"], name="unique_message_reader_receipt")]
        indexes = [models.Index(fields=["ticket", "reader", "message"], name="desk_conver_ticket__b6b315_idx")]

class IdempotencyRecord(models.Model):
    class State(models.TextChoices):
        PROCESSING = "processing", "Processing"
        COMPLETED = "completed", "Completed"
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE)
    operation = models.CharField(max_length=64)
    key = models.CharField(max_length=255)
    fingerprint = models.CharField(max_length=64)
    state = models.CharField(max_length=12, choices=State.choices, default=State.PROCESSING)
    status_code = models.PositiveSmallIntegerField(null=True)
    response_body = models.JSONField(null=True)
    expires_at = models.DateTimeField()
    created_at = models.DateTimeField(auto_now_add=True)
    class Meta:
        constraints = [models.UniqueConstraint(fields=["user", "operation", "key"], name="unique_idempotency_scope")]
        indexes = [models.Index(fields=["expires_at"])]

class OutboxEvent(models.Model):
    class State(models.TextChoices):
        PENDING = "pending", "Pending"
        PUBLISHING = "publishing", "Publishing"
        PUBLISHED = "published", "Published"
        FAILED = "failed", "Failed"

    type = models.CharField(max_length=80)
    payload = models.JSONField()
    created_at = models.DateTimeField(auto_now_add=True)
    published_at = models.DateTimeField(null=True, blank=True)
    attempts = models.PositiveIntegerField(default=0)
    state = models.CharField(max_length=12, choices=State.choices, default=State.PENDING)
    available_at = models.DateTimeField(default=timezone.now)
    lease_token = models.UUIDField(null=True, blank=True, editable=False)
    lease_expires_at = models.DateTimeField(null=True, blank=True)
    last_error = models.TextField(blank=True)
    failed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        indexes = [models.Index(fields=["state", "available_at"], name="desk_outbox_state_726014_idx")]
