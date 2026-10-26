"""Authenticated, database-backed ticket chat websocket protocol.

Redis distributes live events only.  Message ids and history cursors are the
durable reconnect contract because pub/sub notifications may be missed.
"""
import json
import time
import uuid

from channels.auth import get_user
from channels.db import database_sync_to_async
from channels.generic.websocket import AsyncWebsocketConsumer
from django.db import IntegrityError, transaction
from rest_framework.exceptions import ValidationError

from .models import ConversationMessage, ConversationReadReceipt, Ticket
from .pagination import decode_cursor, encode_cursor


MAX_FRAME_BYTES = 20 * 1024
MAX_BODY_BYTES = 16 * 1024
MAX_HISTORY_LIMIT = 100
DEFAULT_HISTORY_LIMIT = 50
EVENTS_PER_WINDOW = 60
RATE_WINDOW_SECONDS = 60
TYPING_INTERVAL_SECONDS = 1.0


class ProtocolError(Exception):
    def __init__(self, code, detail):
        self.code, self.detail = code, detail
        super().__init__(detail)


class TicketConsumer(AsyncWebsocketConsumer):
    """Ticket protocol: message, typing, read, and history/sync client events."""

    async def connect(self):
        self.user = self.scope.get("user")
        try:
            self.ticket_id = int(self.scope["url_route"]["kwargs"]["ticket_id"])
        except (KeyError, TypeError, ValueError):
            await self.close(code=4400)
            return
        self._event_times, self._last_typing_at = [], 0.0
        if not await self.is_authorized():
            await self.close(code=4403)
            return
        self.group_name = f"ticket.{self.ticket_id}"
        await self.channel_layer.group_add(self.group_name, self.channel_name)
        await self.accept()

    async def disconnect(self, _close_code):
        if hasattr(self, "group_name"):
            await self.channel_layer.group_discard(self.group_name, self.channel_name)

    async def session_is_active(self):
        """Reload the session and user so logout/expiry revoke open sockets."""
        session = self.scope.get("session")
        if session is None:
            return False
        wrapped = getattr(session, "_wrapped", None)
        if wrapped is not None:
            wrapped.__dict__.pop("_session_cache", None)
        self.user = await get_user(self.scope)
        return bool(getattr(self.user, "is_authenticated", False) and getattr(self.user, "is_active", False))

    @database_sync_to_async
    def _ticket_authorizes_current_user(self):
        try:
            ticket = Ticket.objects.only("customer_id", "assigned_agent_id").get(id=self.ticket_id)
        except Ticket.DoesNotExist:
            return False
        return self.user.role == "admin" or self.user.id in (ticket.customer_id, ticket.assigned_agent_id)

    async def is_authorized(self):
        return await self.session_is_active() and await self._ticket_authorizes_current_user()

    async def ensure_authorized(self):
        if not await self.session_is_active():
            await self.close(code=4401)
            return False
        if await self._ticket_authorizes_current_user():
            return True
        await self.send_error("participant_forbidden", "You are no longer allowed to access this conversation")
        await self.close(code=4403)
        return False

    async def receive(self, text_data=None, bytes_data=None):
        if bytes_data is not None or text_data is None:
            await self.send_error("invalid_payload", "Text JSON is required")
            return
        if len(text_data.encode("utf-8")) > MAX_FRAME_BYTES:
            await self.send_error("payload_too_large", "Payload exceeds the protocol limit")
            return
        if not self.allow_event():
            await self.send_error("rate_limited", "Too many events; retry shortly", retry_after=RATE_WINDOW_SECONDS)
            return
        if not await self.ensure_authorized():
            return
        try:
            payload = json.loads(text_data)
        except (TypeError, json.JSONDecodeError):
            await self.send_error("invalid_json", "Payload must be valid JSON")
            return
        if not isinstance(payload, dict):
            await self.send_error("invalid_payload", "Payload must be a JSON object")
            return
        try:
            event_type = payload.get("type")
            if event_type == "message":
                await self.handle_message(payload)
            elif event_type == "typing":
                await self.handle_typing(payload)
            elif event_type == "read":
                await self.handle_read(payload)
            elif event_type in {"history", "sync"}:
                await self.send_history(payload)
            else:
                raise ProtocolError("unknown_event", "Unsupported event type")
        except ProtocolError as exc:
            await self.send_error(exc.code, exc.detail)

    def allow_event(self):
        now = time.monotonic()
        self._event_times = [stamp for stamp in self._event_times if stamp > now - RATE_WINDOW_SECONDS]
        if len(self._event_times) >= EVENTS_PER_WINDOW:
            return False
        self._event_times.append(now)
        return True

    async def handle_message(self, payload):
        client_id, body = payload.get("client_message_id"), payload.get("body")
        if not isinstance(client_id, str):
            raise ProtocolError("invalid_client_message_id", "client_message_id must be a UUID")
        try:
            client_uuid = uuid.UUID(client_id)
        except (ValueError, AttributeError, TypeError):
            raise ProtocolError("invalid_client_message_id", "client_message_id must be a UUID")
        if not isinstance(body, str) or not body.strip():
            raise ProtocolError("invalid_body", "body must be a non-empty string")
        if len(body) > 4000 or len(body.encode("utf-8")) > MAX_BODY_BYTES:
            raise ProtocolError("invalid_body", "body exceeds the protocol limit")
        try:
            message, created = await self.save_message(client_uuid, body)
        except PermissionError:
            await self.ensure_authorized()  # assignment may have changed mid-request
            return
        wire = self.message_wire(message)
        if not created:
            await self.send_event({"type": "message", "message": wire, "duplicate": True})
            return
        await self.channel_layer.group_send(self.group_name, {"type": "chat.message", "message": wire})

    @database_sync_to_async
    def save_message(self, client_message_id, body):
        with transaction.atomic():
            try:
                ticket = Ticket.objects.only("customer_id", "assigned_agent_id").get(id=self.ticket_id)
            except Ticket.DoesNotExist:
                raise PermissionError
            if not (self.user.role == "admin" or self.user.id in (ticket.customer_id, ticket.assigned_agent_id)):
                raise PermissionError
            existing = ConversationMessage.objects.filter(ticket_id=self.ticket_id, client_message_id=client_message_id).first()
            if existing is not None:
                if existing.sender_id != self.user.id or existing.body != body:
                    raise ProtocolError("message_id_conflict", "client_message_id was reused with different content")
                return existing, False
            try:
                with transaction.atomic():
                    message = ConversationMessage.objects.create(
                        ticket_id=self.ticket_id, sender=self.user, client_message_id=client_message_id, body=body
                    )
            except IntegrityError:
                message = ConversationMessage.objects.get(ticket_id=self.ticket_id, client_message_id=client_message_id)
                if message.sender_id != self.user.id or message.body != body:
                    raise ProtocolError("message_id_conflict", "client_message_id was reused with different content")
                return message, False
            return message, True

    async def handle_typing(self, payload):
        is_typing = payload.get("is_typing", True)
        if not isinstance(is_typing, bool):
            raise ProtocolError("invalid_typing", "is_typing must be a boolean")
        now = time.monotonic()
        if now - self._last_typing_at < TYPING_INTERVAL_SECONDS:
            return  # best-effort indication; do not amplify burst traffic
        self._last_typing_at = now
        await self.channel_layer.group_send(
            self.group_name, {"type": "typing.event", "user_id": self.user.id, "is_typing": is_typing}
        )

    async def handle_read(self, payload):
        message_id = payload.get("message_id")
        if not isinstance(message_id, int) or isinstance(message_id, bool) or message_id < 1:
            raise ProtocolError("invalid_read_receipt", "message_id must be a positive integer")
        try:
            receipt, created = await self.save_read_receipt(message_id)
        except PermissionError:
            await self.ensure_authorized()
            return
        except ConversationMessage.DoesNotExist:
            raise ProtocolError("message_not_found", "Message is not in this conversation")
        if created:
            await self.channel_layer.group_send(
                self.group_name,
                {"type": "read.event", "message_id": message_id, "reader_id": self.user.id, "read_at": receipt.read_at.isoformat()},
            )

    @database_sync_to_async
    def save_read_receipt(self, message_id):
        with transaction.atomic():
            try:
                ticket = Ticket.objects.only("customer_id", "assigned_agent_id").get(id=self.ticket_id)
            except Ticket.DoesNotExist:
                raise PermissionError
            if not (self.user.role == "admin" or self.user.id in (ticket.customer_id, ticket.assigned_agent_id)):
                raise PermissionError
            message = ConversationMessage.objects.select_for_update().get(id=message_id, ticket_id=self.ticket_id)
            if message.sender_id == self.user.id:
                raise ProtocolError("invalid_read_receipt", "A sender cannot read-receipt their own message")
            receipt, created = ConversationReadReceipt.objects.get_or_create(
                message=message, reader=self.user, defaults={"ticket_id": self.ticket_id}
            )
            if created and message.read_at is None:
                message.read_at = receipt.read_at
                message.save(update_fields=["read_at"])
            return receipt, created

    async def send_history(self, payload=None):
        payload = payload or {}
        if not isinstance(payload, dict):
            raise ProtocolError("invalid_history", "history payload must be an object")
        try:
            page = await self.history_page(payload.get("cursor"), payload.get("after"), payload.get("limit"))
        except (ValidationError, ValueError, TypeError, KeyError):
            raise ProtocolError("invalid_cursor", "cursor, after, or limit is invalid")
        await self.send_event({"type": "history", **page})

    @database_sync_to_async
    def history_page(self, cursor_raw, after_raw, limit_raw):
        if cursor_raw is not None and after_raw is not None:
            raise ValueError
        if limit_raw is None:
            limit = DEFAULT_HISTORY_LIMIT
        elif isinstance(limit_raw, int) and not isinstance(limit_raw, bool) and 1 <= limit_raw <= MAX_HISTORY_LIMIT:
            limit = limit_raw
        else:
            raise ValueError
        queryset = ConversationMessage.objects.filter(ticket_id=self.ticket_id).order_by("id")
        after = None
        if cursor_raw is not None:
            if not isinstance(cursor_raw, str):
                raise ValueError
            cursor = decode_cursor(cursor_raw, kind="messages")
            if int(cursor["ticket_id"]) != self.ticket_id:
                raise ValueError
            after = int(cursor["id"])
        elif after_raw is not None:
            if not isinstance(after_raw, int) or isinstance(after_raw, bool) or after_raw < 0:
                raise ValueError
            after = after_raw
        if after is not None:
            queryset = queryset.filter(id__gt=after)
        rows = list(queryset[: limit + 1])
        has_more, rows = len(rows) > limit, rows[:limit]
        next_cursor = encode_cursor({"kind": "messages", "ticket_id": self.ticket_id, "id": rows[-1].id}) if has_more and rows else None
        return {"results": [self.message_wire(row) for row in rows], "next_cursor": next_cursor, "has_more": has_more, "limit": limit, "after": after}

    @staticmethod
    def message_wire(message):
        return {
            "id": message.id, "ticket_id": message.ticket_id, "sender_id": message.sender_id,
            "client_message_id": str(message.client_message_id), "body": message.body,
            "created_at": message.created_at.isoformat(), "read_at": message.read_at.isoformat() if message.read_at else None,
        }

    async def chat_message(self, event):
        if await self.ensure_authorized():
            await self.send_event({"type": "message", "message": event["message"], "duplicate": False})

    async def typing_event(self, event):
        if await self.ensure_authorized():
            await self.send_event({"type": "typing", "user_id": event["user_id"], "is_typing": event["is_typing"]})

    async def read_event(self, event):
        if await self.ensure_authorized():
            await self.send_event({"type": "read", "message_id": event["message_id"], "reader_id": event["reader_id"], "read_at": event["read_at"]})

    async def send_error(self, code, detail, **extra):
        await self.send_event({"type": "error", "code": code, "detail": detail, **extra})

    async def send_event(self, payload):
        await self.send(text_data=json.dumps(payload, separators=(",", ":")))
