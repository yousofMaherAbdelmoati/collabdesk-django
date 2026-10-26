from datetime import timedelta

import pytest
from asgiref.sync import async_to_sync
from channels.testing import WebsocketCommunicator
from channels.db import database_sync_to_async
from django.contrib.auth import get_user_model
from django.contrib.sessions.models import Session
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import override_settings
from django.utils import timezone
from rest_framework.test import APIClient

from apps.accounts.authentication import digest_token
from apps.accounts.models import ExpiringToken
from apps.desk.models import Ticket
from config.asgi import application


@pytest.fixture
def customer(db):
    return get_user_model().objects.create_user(username="auth-customer", password="x", role="customer")


@pytest.fixture
def agent(db):
    return get_user_model().objects.create_user(username="auth-agent", password="x", role="agent")


def login(client, username, password, issue_token=False, **headers):
    return client.post(
        "/api/auth/login/",
        {"username": username, "password": password, "issue_token": issue_token},
        format="json",
        **headers,
    )


@pytest.mark.django_db
def test_login_session_logout_and_csrf_strategy(customer):
    client = APIClient(enforce_csrf_checks=True)
    csrf = client.get("/api/auth/csrf/", HTTP_ORIGIN="http://localhost:3000")
    assert csrf.status_code == 200
    assert csrf["Access-Control-Allow-Origin"] == "http://localhost:3000"
    assert "csrftoken" in client.cookies

    response = login(client, customer.username, "x")
    assert response.status_code == 200
    assert response.data["user"]["role"] == "customer"
    assert response.cookies["sessionid"]["httponly"]
    assert client.get("/api/tickets").status_code == 200

    # Session-authenticated unsafe calls require CSRF, including logout.
    assert client.post("/api/auth/logout/").status_code == 403
    client.credentials(HTTP_X_CSRFTOKEN=client.cookies["csrftoken"].value)
    assert client.post("/api/auth/logout/", HTTP_ORIGIN="http://localhost:3000").status_code == 204
    assert client.get("/api/tickets").status_code == 401


@pytest.mark.django_db
def test_bearer_token_expiry_and_revocation(customer):
    client = APIClient()
    response = login(client, customer.username, "x", issue_token=True)
    assert response.status_code == 200
    token = response.data["token"]
    assert ExpiringToken.objects.filter(key_digest=digest_token(token)).exists()
    assert not ExpiringToken.objects.filter(key_digest=token).exists()

    token_client = APIClient()
    token_client.credentials(HTTP_AUTHORIZATION=f"Bearer {token}")
    assert token_client.get("/api/tickets").status_code == 200
    assert token_client.post("/api/auth/logout/").status_code == 204
    assert token_client.get("/api/tickets").status_code == 401

    expired = ExpiringToken.objects.create(
        user=customer,
        key_digest=digest_token("expired-token"),
        expires_at=timezone.now() - timedelta(seconds=1),
    )
    assert expired.revoked_at is None
    expired_client = APIClient()
    expired_client.credentials(HTTP_AUTHORIZATION="Bearer expired-token")
    assert expired_client.get("/api/tickets").status_code == 401


@pytest.mark.django_db
def test_login_rate_limit(customer):
    client = APIClient()
    for _ in range(5):
        assert login(client, customer.username, "not-the-password").status_code == 400
    blocked = login(client, customer.username, "not-the-password")
    assert blocked.status_code == 429
    assert int(blocked["Retry-After"]) >= 1


@pytest.mark.django_db
def test_role_policy_and_horizontal_privilege_escalation(customer, agent):
    other_customer = get_user_model().objects.create_user(username="other-customer", password="x", role="customer")
    admin = get_user_model().objects.create_user(username="desk-admin", password="x", role="admin")
    other_ticket = Ticket.objects.create(customer=other_customer, title="private", description="private")
    own_ticket = Ticket.objects.create(customer=customer, title="mine", description="mine")
    customer_client = APIClient(); customer_client.force_authenticate(customer)
    agent_client = APIClient(); agent_client.force_authenticate(agent)
    admin_client = APIClient(); admin_client.force_authenticate(admin)

    assert customer_client.get(f"/api/tickets/{other_ticket.id}/messages").status_code == 403
    assert customer_client.post(f"/api/tickets/{other_ticket.id}/messages", {"client_message_id": "109a65f1-5d3b-4f5f-9649-097aed198210", "body": "attack"}, format="json").status_code == 403
    assert agent_client.post("/api/tickets", {"title": "no", "description": "no"}, format="json", HTTP_IDEMPOTENCY_KEY="x").status_code == 403
    assert agent_client.post(f"/api/tickets/{own_ticket.id}/claim").status_code == 200
    assert agent_client.get(f"/api/tickets/{other_ticket.id}/messages").status_code == 403
    assert admin_client.get(f"/api/tickets/{other_ticket.id}/messages").status_code == 200
    assert admin.is_staff and not admin.is_superuser


@pytest.mark.django_db
def test_staff_flags_cannot_elevate_customer_or_agent():
    User = get_user_model()
    customer = User.objects.create_user(username="not-staff", password="x", role="customer", is_staff=True)
    agent = User.objects.create_user(username="not-super", password="x", role="agent", is_superuser=True)
    assert not customer.is_staff and not customer.is_superuser
    assert agent.role == User.Role.ADMIN and agent.is_staff and agent.is_superuser
    customer.role = User.Role.ADMIN
    customer.save(update_fields=["role"])
    customer.refresh_from_db()
    assert customer.is_staff


@pytest.mark.django_db
def test_cors_rejects_unknown_origin_and_never_wildcards(customer):
    client = APIClient()
    allowed = client.options("/api/auth/login/", HTTP_ORIGIN="http://localhost:3000")
    assert allowed.status_code == 204
    assert allowed["Access-Control-Allow-Origin"] == "http://localhost:3000"
    assert allowed["Access-Control-Allow-Credentials"] == "true"
    assert "*" not in allowed["Access-Control-Allow-Origin"]
    assert client.options("/api/auth/login/", HTTP_ORIGIN="https://evil.example").status_code == 403
    assert login(client, customer.username, "x", HTTP_ORIGIN="https://evil.example").status_code == 403


@pytest.mark.django_db(transaction=True)
@override_settings(CHANNEL_LAYERS={"default": {"BACKEND": "channels.layers.InMemoryChannelLayer"}})
def test_websocket_requires_session_authorization_and_allowed_origin(customer):
    ticket = Ticket.objects.create(customer=customer, title="websocket", description="websocket")
    browser = APIClient()
    assert login(browser, customer.username, "x").status_code == 200
    cookie = f"sessionid={browser.cookies['sessionid'].value}".encode()

    async def exercise_websockets():
        valid = WebsocketCommunicator(application, f"/ws/tickets/{ticket.id}/", headers=[(b"origin", b"http://localhost:3000"), (b"cookie", cookie)])
        connected, _ = await valid.connect()
        assert connected
        # Server-side deletion (the effect of logout/expiry) closes an open
        # socket before it may process another client event.
        @database_sync_to_async
        def revoke_session():
            Session.objects.filter(session_key=browser.cookies["sessionid"].value).delete()

        await revoke_session()
        await valid.send_to(text_data='{"type":"typing"}')
        closed = await valid.receive_output(timeout=1)
        assert closed == {"type": "websocket.close", "code": 4401}
        await valid.disconnect()

        no_session = WebsocketCommunicator(application, f"/ws/tickets/{ticket.id}/?token=never-in-a-url", headers=[(b"origin", b"http://localhost:3000")])
        connected, _ = await no_session.connect()
        assert not connected

        evil_origin = WebsocketCommunicator(application, f"/ws/tickets/{ticket.id}/", headers=[(b"origin", b"https://evil.example"), (b"cookie", cookie)])
        connected, _ = await evil_origin.connect()
        assert not connected

    async_to_sync(exercise_websockets)()


@pytest.mark.django_db
def test_local_seed_command_is_environment_gated(settings):
    settings.LOCAL_DEMO_SEED_ENABLED = False
    with pytest.raises(CommandError):
        call_command("seed_local_accounts", "--password", "fictional-only")
    settings.LOCAL_DEMO_SEED_ENABLED = True
    call_command("seed_local_accounts", "--password", "fictional-only")
    assert set(get_user_model().objects.filter(username__startswith="demo-").values_list("role", flat=True)) == {"customer", "agent", "admin"}
