import uuid
import json

import pytest
from asgiref.sync import async_to_sync
from channels.db import database_sync_to_async
from channels.testing import WebsocketCommunicator
from django.conf import settings
from django.contrib.auth import get_user_model
from django.test import Client

from apps.desk.models import ConversationMessage, ConversationReadReceipt, Ticket
from config.asgi import application


@pytest.fixture(autouse=True)
def in_memory_channel_layer(settings):
    # This covers ASGI behavior without claiming a Redis integration check.
    settings.CHANNEL_LAYERS = {"default": {"BACKEND": "channels.layers.InMemoryChannelLayer"}}


@pytest.fixture
def chat_users(db):
    User = get_user_model()
    return (
        User.objects.create_user(username="chat-customer", password="x", role="customer"),
        User.objects.create_user(username="chat-agent", password="x", role="agent"),
        User.objects.create_user(username="chat-outsider", password="x", role="customer"),
    )


def _communicator(user, ticket_id, origin=b"http://localhost:3000"):
    client = Client()
    client.force_login(user)
    session_id = client.cookies[settings.SESSION_COOKIE_NAME].value
    return WebsocketCommunicator(
        application,
        f"/ws/tickets/{ticket_id}/",
        headers=[(b"origin", origin), (b"cookie", f"{settings.SESSION_COOKIE_NAME}={session_id}".encode())],
    )


async def _open(communicator):
    connected, _ = await communicator.connect()
    assert connected
    return communicator


@pytest.mark.django_db(transaction=True)
def test_two_participants_exchange_typing_messages_and_persisted_read_receipts(chat_users):
    customer, agent, _ = chat_users
    ticket = Ticket.objects.create(customer=customer, assigned_agent=agent, status=Ticket.Status.CLAIMED, title="t", description="d")
    customer_socket = _communicator(customer, ticket.id)
    agent_socket = _communicator(agent, ticket.id)

    async def scenario():
        await _open(customer_socket)
        await _open(agent_socket)
        try:
            await customer_socket.send_json_to({"type": "typing", "is_typing": True})
            customer_typing = await customer_socket.receive_json_from()
            agent_typing = await agent_socket.receive_json_from()
            assert customer_typing == agent_typing == {"type": "typing", "user_id": customer.id, "is_typing": True}

            client_id = str(uuid.uuid4())
            await customer_socket.send_json_to({"type": "message", "client_message_id": client_id, "body": "fictional local hello"})
            customer_message = await customer_socket.receive_json_from()
            agent_message = await agent_socket.receive_json_from()
            assert customer_message == agent_message
            assert customer_message["type"] == "message"
            assert customer_message["duplicate"] is False
            message = customer_message["message"]
            assert message["sender_id"] == customer.id
            assert message["client_message_id"] == client_id

            await agent_socket.send_json_to({"type": "read", "message_id": message["id"]})
            customer_read = await customer_socket.receive_json_from()
            agent_read = await agent_socket.receive_json_from()
            assert customer_read == agent_read
            assert customer_read["type"] == "read"
            assert customer_read["reader_id"] == agent.id
            return message["id"]
        finally:
            await customer_socket.disconnect()
            await agent_socket.disconnect()

    message_id = async_to_sync(scenario)()
    assert ConversationReadReceipt.objects.filter(message_id=message_id, reader=agent).exists()
    assert ConversationMessage.objects.get(id=message_id).read_at is not None


@pytest.mark.django_db(transaction=True)
def test_third_party_and_untrusted_origin_are_denied_before_chat(chat_users):
    customer, agent, outsider = chat_users
    ticket = Ticket.objects.create(customer=customer, assigned_agent=agent, status=Ticket.Status.CLAIMED, title="t", description="d")
    third_party = _communicator(outsider, ticket.id)
    bad_origin = _communicator(customer, ticket.id, b"https://untrusted.example")

    async def scenario():
        connected, _ = await third_party.connect()
        assert not connected
        connected, _ = await bad_origin.connect()
        assert not connected

    async_to_sync(scenario)()


@pytest.mark.django_db(transaction=True)
def test_duplicate_client_uuid_is_unicast_ack_and_never_creates_second_message(chat_users):
    customer, agent, _ = chat_users
    ticket = Ticket.objects.create(customer=customer, assigned_agent=agent, status=Ticket.Status.CLAIMED, title="t", description="d")
    customer_socket = _communicator(customer, ticket.id)
    agent_socket = _communicator(agent, ticket.id)

    async def scenario():
        await _open(customer_socket)
        await _open(agent_socket)
        try:
            payload = {"type": "message", "client_message_id": str(uuid.uuid4()), "body": "one durable message"}
            await customer_socket.send_json_to(payload)
            first_customer = await customer_socket.receive_json_from()
            first_agent = await agent_socket.receive_json_from()
            assert first_customer == first_agent
            await customer_socket.send_json_to(payload)
            replay = await customer_socket.receive_json_from()
            assert replay["type"] == "message" and replay["duplicate"] is True
            assert replay["message"]["id"] == first_customer["message"]["id"]
            assert await agent_socket.receive_nothing(timeout=0.1)
        finally:
            await customer_socket.disconnect()
            await agent_socket.disconnect()

    async_to_sync(scenario)()
    assert ConversationMessage.objects.filter(ticket=ticket).count() == 1


@pytest.mark.django_db(transaction=True)
def test_reconnect_history_cursor_sync_and_multiple_sessions(chat_users):
    customer, agent, _ = chat_users
    ticket = Ticket.objects.create(customer=customer, assigned_agent=agent, status=Ticket.Status.CLAIMED, title="t", description="d")
    customer_one = _communicator(customer, ticket.id)
    customer_two = _communicator(customer, ticket.id)
    first_agent_socket = _communicator(agent, ticket.id)
    reconnect_agent_socket = _communicator(agent, ticket.id)

    async def scenario():
        await _open(customer_one)
        await _open(customer_two)
        await _open(first_agent_socket)
        agent_socket = first_agent_socket
        try:
            await agent_socket.send_json_to({"type": "message", "client_message_id": str(uuid.uuid4()), "body": "first"})
            first_events = [
                await customer_one.receive_json_from(),
                await customer_two.receive_json_from(),
                await agent_socket.receive_json_from(),
            ]
            assert {event["message"]["body"] for event in first_events} == {"first"}
            first_id = first_events[0]["message"]["id"]

            await agent_socket.disconnect()
            await customer_one.send_json_to({"type": "message", "client_message_id": str(uuid.uuid4()), "body": "missed while offline"})
            assert (await customer_one.receive_json_from())["message"]["body"] == "missed while offline"
            assert (await customer_two.receive_json_from())["message"]["body"] == "missed while offline"

            await _open(reconnect_agent_socket)
            agent_socket = reconnect_agent_socket
            await agent_socket.send_json_to({"type": "history", "after": 0, "limit": 1})
            history = await agent_socket.receive_json_from()
            assert [row["body"] for row in history["results"]] == ["first"]
            assert history["has_more"] is True
            assert history["next_cursor"]
            await agent_socket.send_json_to({"type": "sync", "cursor": history["next_cursor"], "limit": 10})
            recovered = await agent_socket.receive_json_from()
            assert [row["body"] for row in recovered["results"]] == ["missed while offline"]
            assert recovered["has_more"] is False
            assert recovered["next_cursor"] is None
        finally:
            await customer_one.disconnect()
            await customer_two.disconnect()
            await agent_socket.disconnect()

    async_to_sync(scenario)()


@pytest.mark.django_db(transaction=True)
def test_permission_revocation_blocks_future_events_and_returns_structured_error(chat_users):
    customer, agent, _ = chat_users
    ticket = Ticket.objects.create(customer=customer, assigned_agent=agent, status=Ticket.Status.CLAIMED, title="t", description="d")
    agent_socket = _communicator(agent, ticket.id)

    async def scenario():
        await _open(agent_socket)
        # The DB change stands in for a concurrent assignment revocation.
        # The next inbound event must reauthorize rather than trust connect.
        await database_sync_to_async(Ticket.objects.filter(id=ticket.id).update)(assigned_agent=None)
        await agent_socket.send_json_to({"type": "history", "after": 0})
        error = await agent_socket.receive_json_from()
        assert error["type"] == "error"
        assert error["code"] == "participant_forbidden"
        await agent_socket.wait(timeout=1)
    async_to_sync(scenario)()


@pytest.mark.django_db(transaction=True)
def test_invalid_and_oversize_frames_return_structured_errors(chat_users):
    customer, _, _ = chat_users
    ticket = Ticket.objects.create(customer=customer, title="t", description="d")
    socket = _communicator(customer, ticket.id)

    async def scenario():
        await _open(socket)
        try:
            await socket.send_to(text_data="not-json")
            assert (await socket.receive_json_from())["code"] == "invalid_json"

            oversized = json.dumps(
                {"type": "message", "client_message_id": str(uuid.uuid4()), "body": "x" * (21 * 1024)}
            )
            await socket.send_to(text_data=oversized)
            assert (await socket.receive_json_from())["code"] == "payload_too_large"
        finally:
            await socket.disconnect()

    async_to_sync(scenario)()
