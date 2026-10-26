import concurrent.futures
import asyncio
import json
import subprocess
import sys
import uuid
from unittest.mock import MagicMock, patch
import pytest
from django.contrib.auth import get_user_model
from django.db import connections
from rest_framework.test import APIClient
from apps.desk.models import Ticket

@pytest.fixture
def customer(db): return get_user_model().objects.create_user(username="customer", password="x", role="customer")
@pytest.fixture
def agent(db): return get_user_model().objects.create_user(username="agent", password="x", role="agent")
def client_for(user):
    c = APIClient(); c.force_authenticate(user); return c


def test_asgi_application_imports_in_fresh_process():
    result = subprocess.run(
        [sys.executable, "-c", "import config.asgi"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr


def test_liveness_does_not_require_dependencies(client):
    response = client.get("/api/health/live/")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_health_alias_is_liveness(client):
    response = client.get("/api/health/")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


@patch("apps.desk.views.get_redis_connection")
@patch("apps.desk.views.connection.cursor")
def test_readiness_is_healthy_when_postgres_and_redis_are_available(cursor, redis_connection, client):
    redis_connection.return_value = MagicMock()
    response = client.get("/api/health/ready/")
    assert response.status_code == 200
    assert response.json() == {"status": "ok", "checks": {"database": "ok", "redis": "ok"}}
    cursor.return_value.__enter__.return_value.execute.assert_called_once_with("SELECT 1")
    redis_connection.return_value.ping.assert_called_once_with()


@patch("apps.desk.views.get_redis_connection")
@patch("apps.desk.views.connection.cursor", side_effect=Exception("database unavailable"))
def test_readiness_reports_unavailable_dependencies(cursor, redis_connection, client):
    redis_connection.return_value = MagicMock()
    response = client.get("/api/health/ready/")
    assert response.status_code == 503
    assert response.json() == {"status": "unavailable", "checks": {"database": "unavailable", "redis": "ok"}}


@patch("apps.desk.views.get_redis_connection", side_effect=Exception("redis unavailable"))
@patch("apps.desk.views.connection.cursor")
def test_readiness_reports_unavailable_redis(cursor, redis_connection, client):
    response = client.get("/api/health/ready/")
    assert response.status_code == 503
    assert response.json() == {"status": "unavailable", "checks": {"database": "ok", "redis": "unavailable"}}

@pytest.mark.django_db(transaction=True)
def test_replay_after_response_loss_returns_persisted_original(customer):
    c = client_for(customer); key = str(uuid.uuid4()); payload = {"title": "Help", "description": "Need help"}
    first = c.post("/api/tickets", payload, format="json", HTTP_IDEMPOTENCY_KEY=key)
    # Model a client that lost the successful HTTP response and retries the
    # same request. The response comes from the persisted completed record.
    again = c.post("/api/tickets", payload, format="json", HTTP_IDEMPOTENCY_KEY=key)
    assert first.status_code == 201 and again.status_code == 201
    assert first.data["id"] == again.data["id"] and Ticket.objects.count() == 1
    assert again["Idempotency-Replayed"] == "true"

@pytest.mark.django_db(transaction=True)
def test_key_reuse_with_different_payload_rejected(customer):
    c = client_for(customer); key = str(uuid.uuid4())
    c.post("/api/tickets", {"title":"A", "description":"a"}, format="json", HTTP_IDEMPOTENCY_KEY=key)
    assert c.post("/api/tickets", {"title":"B", "description":"b"}, format="json", HTTP_IDEMPOTENCY_KEY=key).status_code == 409

@pytest.mark.postgres
@pytest.mark.django_db(transaction=True)
def test_parallel_claim_has_one_winner(customer, agent):
    other = get_user_model().objects.create_user(username="agent2", password="x", role="agent")
    ticket = Ticket.objects.create(customer=customer, title="t", description="d")
    def claim(user):
        try:
            return client_for(user).post(f"/api/tickets/{ticket.id}/claim").status_code
        finally:
            # Django connections are thread-local; close this worker thread's
            # connection before pytest tries to remove the PostgreSQL test DB.
            connections.close_all()
    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool: results = list(pool.map(claim, [agent, other]))
    assert sorted(results) == [200, 409]
    ticket.refresh_from_db(); assert ticket.assigned_agent_id in [agent.id, other.id]

@pytest.mark.django_db
def test_message_ownership_and_deduplication(customer, agent):
    ticket = Ticket.objects.create(customer=customer, assigned_agent=agent, status="claimed", title="t", description="d")
    c = client_for(customer); mid = str(uuid.uuid4())
    assert c.post(f"/api/tickets/{ticket.id}/messages", {"client_message_id":mid,"body":"hello"}, format="json").status_code == 201
    assert c.post(f"/api/tickets/{ticket.id}/messages", {"client_message_id":mid,"body":"hello"}, format="json").status_code == 200

@pytest.mark.django_db
def test_stream_has_ordered_tokens(customer):
    ticket = Ticket.objects.create(customer=customer, title="t", description="d")
    r = client_for(customer).post("/api/assistant/stream", {"ticket_id": ticket.id, "question": "What next?"}, format="json")
    assert r["Content-Type"].startswith("text/event-stream")

    async def consume():
        return [chunk async for chunk in r.streaming_content]

    frames = asyncio.run(consume())
    parsed = []
    for frame in frames:
        event, data = frame.decode().rstrip("\n").split("\n")
        parsed.append((event.removeprefix("event: "), json.loads(data.removeprefix("data: "))))
    tokens = [payload for event, payload in parsed if event == "token"]
    assert [token["sequence"] for token in tokens] == list(range(len(tokens)))
    assert parsed[-1] == ("complete", {"chunks": len(tokens), "truncated": False})
