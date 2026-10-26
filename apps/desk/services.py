"""Database-backed operations. PostgreSQL is the correctness boundary; Redis is only an optimization."""
import hashlib
import json
import logging
from datetime import timedelta
from django.db import IntegrityError, transaction
from django.db.models import F
from django.utils import timezone
from .models import IdempotencyRecord, OutboxEvent, Ticket

class IdempotencyConflict(Exception): pass
class RequestInProgress(Exception): pass
class TicketConflict(Exception): pass


logger = logging.getLogger("collabdesk.ticket_api")
IDEMPOTENCY_TTL = timedelta(hours=24)


def _invalidate_ticket_analytics_after_commit(ticket_id: int) -> None:
    # Conditional QuerySet updates intentionally bypass model signals.
    from .signals import invalidate_desk_analytics
    transaction.on_commit(lambda: invalidate_desk_analytics([ticket_id]))

def fingerprint(payload: object) -> str:
    return hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def cleanup_expired_idempotency_records(*, now=None) -> int:
    """Delete persisted records whose replay guarantee has elapsed.

    This function is intentionally database-backed and safe to run from a
    scheduled worker or management command. Creation also removes an expired
    record in its locked scope, so an infrequently-run scheduler never makes a
    caller unable to reuse a key.
    """
    now = now or timezone.now()
    deleted, _ = IdempotencyRecord.objects.filter(expires_at__lte=now).delete()
    logger.info("idempotency_cleanup", extra={"event": "idempotency_cleanup", "outcome": "completed", "deleted": deleted})
    return deleted


def _completed_response(record: IdempotencyRecord, digest: str) -> tuple[dict, int, bool]:
    if record.fingerprint != digest:
        logger.info("ticket_create", extra={"event": "ticket_create", "outcome": "idempotency_conflict"})
        raise IdempotencyConflict("Idempotency-Key was reused with a different payload")
    if record.state == IdempotencyRecord.State.COMPLETED:
        logger.info("ticket_create", extra={"event": "ticket_create", "outcome": "replayed"})
        return record.response_body or {}, record.status_code or 201, True
    logger.info("ticket_create", extra={"event": "ticket_create", "outcome": "in_progress"})
    raise RequestInProgress("The original request is still processing; retry shortly")


def create_ticket_idempotently(*, user, key: str, payload: dict) -> tuple[dict, int, bool]:
    """Creation and saved response share one transaction. A crash rolls both back.
    A crash after commit is safely replayed from the persisted completed record."""
    digest, now = fingerprint(payload), timezone.now()
    # A uniqueness collision is expected under concurrent same-key requests.
    # The losing transaction retries and reads the committed canonical row.
    while True:
        collision = False
        with transaction.atomic():
            record = (
                IdempotencyRecord.objects.select_for_update()
                .filter(user=user, operation="tickets.create", key=key)
                .first()
            )
            if record is not None:
                if record.expires_at <= now:
                    # The lock covers deletion and recreation, so a concurrent
                    # retry sees either the old completed row or the new scope.
                    record.delete()
                else:
                    return _completed_response(record, digest)
            try:
                # A savepoint keeps the enclosing transaction usable after the
                # unique constraint decides a different worker won the race.
                with transaction.atomic():
                    record = IdempotencyRecord.objects.create(
                        user=user,
                        operation="tickets.create",
                        key=key,
                        fingerprint=digest,
                        expires_at=now + IDEMPOTENCY_TTL,
                    )
            except IntegrityError:
                collision = True
            if collision:
                continue
            ticket = Ticket.objects.create(customer=user, title=payload["title"], description=payload["description"])
            response = {"id": ticket.id, "title": ticket.title, "description": ticket.description, "status": ticket.status, "customer_id": user.id, "assigned_agent_id": None, "version": ticket.version, "created_at": ticket.created_at.isoformat(), "updated_at": ticket.updated_at.isoformat()}
            record.state, record.status_code, record.response_body = IdempotencyRecord.State.COMPLETED, 201, response
            record.save(update_fields=["state", "status_code", "response_body"])
            OutboxEvent.objects.create(type="ticket.created", payload={"ticket_id": ticket.id})
            logger.info("ticket_create", extra={"event": "ticket_create", "outcome": "created"})
            return response, 201, False

def claim_ticket(*, ticket_id: int, agent) -> Ticket:
    # Conditional UPDATE is atomic across application processes and avoids lock wait amplification.
    updated = Ticket.objects.filter(id=ticket_id, status=Ticket.Status.OPEN, assigned_agent__isnull=True).update(assigned_agent=agent, status=Ticket.Status.CLAIMED, version=F("version") + 1, updated_at=timezone.now())
    if updated != 1:
        logger.info("ticket_claim", extra={"event": "ticket_claim", "outcome": "conflict"})
        raise TicketConflict("Ticket is no longer available")
    # QuerySet.update() bypasses model signals; invalidate only after the
    # conditional database write has succeeded.
    _invalidate_ticket_analytics_after_commit(ticket_id)
    logger.info("ticket_claim", extra={"event": "ticket_claim", "outcome": "claimed"})
    return Ticket.objects.get(id=ticket_id)


def resolve_ticket(*, ticket_id: int, actor, expected_version: int) -> Ticket:
    """Resolve by conditional update, preventing stale or cross-agent writes."""
    filters = {"id": ticket_id, "status": Ticket.Status.CLAIMED, "version": expected_version}
    if actor.role != "admin":
        filters["assigned_agent"] = actor
    updated = Ticket.objects.filter(**filters).update(
        status=Ticket.Status.RESOLVED,
        version=F("version") + 1,
        updated_at=timezone.now(),
    )
    if updated != 1:
        logger.info("ticket_transition", extra={"event": "ticket_transition", "outcome": "conflict"})
        raise TicketConflict("Ticket changed or cannot be transitioned")
    _invalidate_ticket_analytics_after_commit(ticket_id)
    logger.info("ticket_transition", extra={"event": "ticket_transition", "outcome": "resolved"})
    return Ticket.objects.get(id=ticket_id)
