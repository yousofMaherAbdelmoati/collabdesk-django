import concurrent.futures
import logging
import uuid
from datetime import timedelta
from unittest.mock import patch

import pytest
from django.contrib.auth import get_user_model
from django.db import connections
from django.utils import timezone
from rest_framework.test import APIClient

from apps.desk.models import IdempotencyRecord, OutboxEvent, Ticket
from apps.desk.services import cleanup_expired_idempotency_records, create_ticket_idempotently


@pytest.fixture
def customer(db):
    return get_user_model().objects.create_user(username="hardening-customer", password="x", role="customer")


@pytest.fixture
def agent(db):
    return get_user_model().objects.create_user(username="hardening-agent", password="x", role="agent")


def client_for(user):
    client = APIClient()
    client.force_authenticate(user)
    return client


def test_ticket_list_uses_stable_signed_keyset_cursor(customer):
    tickets = [Ticket.objects.create(customer=customer, title=f"ticket-{i}", description="d") for i in range(3)]
    client = client_for(customer)

    first = client.get("/api/tickets?limit=2")
    assert first.status_code == 200
    assert [row["id"] for row in first.data["results"]] == [tickets[2].id, tickets[1].id]
    assert first.data["next_cursor"]

    # A later insert must not appear in the continuation of an existing page.
    newest = Ticket.objects.create(customer=customer, title="new", description="d")
    second = client.get("/api/tickets", {"cursor": first.data["next_cursor"], "limit": 2})
    assert second.status_code == 200
    assert [row["id"] for row in second.data["results"]] == [tickets[0].id]
    assert newest.id not in [row["id"] for row in second.data["results"]]

    malformed = client.get("/api/tickets?cursor=not-a-cursor")
    assert malformed.status_code == 400
    assert malformed.data == {"cursor": ["Invalid cursor."]}


def test_message_list_uses_stable_cursor_and_participant_boundary(customer, agent):
    ticket = Ticket.objects.create(customer=customer, assigned_agent=agent, status=Ticket.Status.CLAIMED, title="t", description="d")
    client = client_for(customer)
    for index in range(3):
        assert client.post(
            f"/api/tickets/{ticket.id}/messages",
            {"client_message_id": str(uuid.uuid4()), "body": f"message-{index}"},
            format="json",
        ).status_code == 201

    first = client.get(f"/api/tickets/{ticket.id}/messages?limit=2")
    assert [row["body"] for row in first.data["results"]] == ["message-0", "message-1"]
    second = client.get(f"/api/tickets/{ticket.id}/messages", {"cursor": first.data["next_cursor"]})
    assert [row["body"] for row in second.data["results"]] == ["message-2"]

    other_ticket = Ticket.objects.create(customer=customer, title="other", description="d")
    cross_ticket_cursor = client.get(f"/api/tickets/{other_ticket.id}/messages", {"cursor": first.data["next_cursor"]})
    assert cross_ticket_cursor.status_code == 400

    other = get_user_model().objects.create_user(username="other-hardening", password="x", role="customer")
    denied = client_for(other).get(f"/api/tickets/{ticket.id}/messages")
    assert denied.status_code == 403
    assert denied.data["code"] == "participant_forbidden"


def test_transition_requires_assigned_participant_and_current_version(customer, agent):
    ticket = Ticket.objects.create(customer=customer, assigned_agent=agent, status=Ticket.Status.CLAIMED, title="t", description="d", version=3)
    client = client_for(agent)

    stale = client.post(f"/api/tickets/{ticket.id}/status", {"status": "resolved", "expected_version": 2}, format="json")
    assert stale.status_code == 409
    resolved = client.post(f"/api/tickets/{ticket.id}/status", {"status": "resolved", "expected_version": 3}, format="json")
    assert resolved.status_code == 200
    assert resolved.data["status"] == Ticket.Status.RESOLVED
    assert resolved.data["version"] == 4

    forbidden = client_for(customer).post(f"/api/tickets/{ticket.id}/status", {"status": "resolved", "expected_version": 4}, format="json")
    assert forbidden.status_code == 403


def test_expired_key_is_cleaned_and_may_be_reused(customer):
    payload = {"title": "fresh", "description": "new request"}
    IdempotencyRecord.objects.create(
        user=customer,
        operation="tickets.create",
        key="expired-key",
        fingerprint="old",
        state=IdempotencyRecord.State.COMPLETED,
        status_code=201,
        response_body={"id": 123},
        expires_at=timezone.now() - timedelta(seconds=1),
    )
    body, status_code, replayed = create_ticket_idempotently(user=customer, key="expired-key", payload=payload)
    assert status_code == 201 and replayed is False
    assert body["title"] == "fresh"
    record = IdempotencyRecord.objects.get(user=customer, operation="tickets.create", key="expired-key")
    assert record.fingerprint != "old"

    IdempotencyRecord.objects.create(
        user=customer,
        operation="tickets.create",
        key="cleanup-key",
        fingerprint="old",
        expires_at=timezone.now() - timedelta(seconds=1),
    )
    assert cleanup_expired_idempotency_records() == 1
    assert not IdempotencyRecord.objects.filter(key="cleanup-key").exists()


def test_failed_ticket_creation_rolls_back_ticket_record_and_outbox(customer):
    with patch("apps.desk.services.OutboxEvent.objects.create", side_effect=RuntimeError("fictional local failure")):
        with pytest.raises(RuntimeError):
            create_ticket_idempotently(user=customer, key="rollback-key", payload={"title": "t", "description": "d"})
    assert Ticket.objects.count() == 0
    assert IdempotencyRecord.objects.count() == 0
    assert OutboxEvent.objects.count() == 0


def test_ticket_events_never_include_request_content(customer):
    secret_like_title = "fictional title must not enter diagnostics"
    key = "fictional-idempotency-key-must-not-enter-diagnostics"
    records = []

    class Capture(logging.Handler):
        def emit(self, record):
            records.append(record)

    logger = logging.getLogger("collabdesk.ticket_api")
    capture = Capture()
    logger.addHandler(capture)
    try:
        create_ticket_idempotently(user=customer, key=key, payload={"title": secret_like_title, "description": "fictional body"})
    finally:
        logger.removeHandler(capture)
    record = next(record for record in records if record.name == "collabdesk.ticket_api")
    assert record.event == "ticket_create"
    assert record.outcome == "created"
    assert secret_like_title not in record.getMessage()
    assert key not in record.getMessage()


@pytest.mark.postgres
@pytest.mark.django_db(transaction=True)
def test_twenty_same_key_creations_have_one_committed_result(customer):
    key = "parallel-" + str(uuid.uuid4())
    payload = {"title": "parallel", "description": "same fictional payload"}

    def create(_):
        try:
            return create_ticket_idempotently(user=customer, key=key, payload=payload)
        finally:
            connections.close_all()

    with concurrent.futures.ThreadPoolExecutor(max_workers=20) as pool:
        results = list(pool.map(create, range(20)))
    assert {result[0]["id"] for result in results} == {Ticket.objects.get().id}
    assert sum(not result[2] for result in results) == 1
    assert Ticket.objects.count() == 1
    assert IdempotencyRecord.objects.count() == 1


@pytest.mark.postgres
@pytest.mark.django_db(transaction=True)
def test_twenty_claims_have_exactly_one_winner(customer):
    ticket = Ticket.objects.create(customer=customer, title="claim", description="d")
    agents = [
        get_user_model().objects.create_user(username=f"hardening-agent-{index}", password="x", role="agent")
        for index in range(20)
    ]

    def claim(agent):
        try:
            return client_for(agent).post(f"/api/tickets/{ticket.id}/claim").status_code
        finally:
            connections.close_all()

    with concurrent.futures.ThreadPoolExecutor(max_workers=20) as pool:
        statuses = list(pool.map(claim, agents))
    assert statuses.count(200) == 1
    assert statuses.count(409) == 19
    ticket.refresh_from_db()
    assert ticket.status == Ticket.Status.CLAIMED
    assert ticket.assigned_agent_id in {agent.id for agent in agents}
