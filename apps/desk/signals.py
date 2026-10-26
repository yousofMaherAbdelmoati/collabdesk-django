"""Invalidate derived desk statistics after durable changes commit."""
from django.db import transaction
from django.db.models.signals import post_delete, post_save
from django.dispatch import receiver

from .cache import invalidate_ticket_statistics
from .models import ConversationMessage, Ticket


def invalidate_desk_analytics(ticket_ids: list[int]) -> None:
    """Invalidate local dashboard cache and report materializations post-commit."""
    invalidate_ticket_statistics()
    # Keep reports optional at import time: the reports app itself imports desk
    # models while Django is populating the registry.
    from apps.reports.services import invalidate_analytics_summaries
    invalidate_analytics_summaries(ticket_ids)


def _invalidate_after_commit(**kwargs) -> None:
    if kwargs.get("raw"):
        return
    instance = kwargs["instance"]
    ticket_id = instance.id if isinstance(instance, Ticket) else instance.ticket_id
    transaction.on_commit(lambda: invalidate_desk_analytics([ticket_id]))


@receiver(post_save, sender=Ticket)
@receiver(post_delete, sender=Ticket)
@receiver(post_save, sender=ConversationMessage)
@receiver(post_delete, sender=ConversationMessage)
def invalidate_statistics_for_desk_change(sender, **kwargs) -> None:
    _invalidate_after_commit(**kwargs)
