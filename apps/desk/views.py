import logging
from django.conf import settings
from django.http import StreamingHttpResponse
from django.shortcuts import get_object_or_404
from django.db import connection
from django.db.models import Q
from django_redis import get_redis_connection
from rest_framework import status
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from .cache import cache_metrics, get_agent_statistics, get_ticket_statistics
from .models import ConversationMessage, Ticket
from .pagination import decode_cursor, encode_cursor, page_size
from .serializers import AssistantRequestSerializer, MessageSerializer, StatusTransitionSerializer, TicketSerializer
from .services import IdempotencyConflict, RequestInProgress, TicketConflict, claim_ticket, create_ticket_idempotently, resolve_ticket
from .streaming import AssistantPrompt, DeterministicLocalProvider, assistant_sse_events


logger = logging.getLogger("collabdesk.ticket_api")

def can_access_ticket(user, ticket: Ticket) -> bool:
    return user.role == "admin" or ticket.customer_id == user.id or ticket.assigned_agent_id == user.id


def api_error(detail: str, code: str, http_status: int, *, headers=None) -> Response:
    """Keep non-field API errors machine-readable without leaking internals."""
    return Response({"detail": detail, "code": code}, status=http_status, headers=headers)


def paginated_tickets(request, queryset):
    limit = page_size(request.query_params.get("limit"))
    cursor = decode_cursor(request.query_params.get("cursor"), kind="tickets")
    if cursor:
        try:
            created_at, ticket_id = cursor["created_at"], int(cursor["id"])
        except (KeyError, TypeError, ValueError):
            # Use the same public validation response for malformed signed data.
            from rest_framework.exceptions import ValidationError
            raise ValidationError({"cursor": ["Invalid cursor."]})
        queryset = queryset.filter(Q(created_at__lt=created_at) | Q(created_at=created_at, id__lt=ticket_id))
    rows = list(queryset.order_by("-created_at", "-id")[: limit + 1])
    more, rows = len(rows) > limit, rows[:limit]
    next_cursor = None
    if more and rows:
        tail = rows[-1]
        next_cursor = encode_cursor({"kind": "tickets", "created_at": tail.created_at.isoformat(), "id": tail.id})
    return Response({"results": TicketSerializer(rows, many=True).data, "next_cursor": next_cursor})


def paginated_messages(request, ticket: Ticket):
    limit = page_size(request.query_params.get("limit"))
    cursor = decode_cursor(request.query_params.get("cursor"), kind="messages")
    queryset = ticket.messages.order_by("id")
    # `after` remains temporarily supported for deployed clients; cursors are
    # preferred and cannot be forged into a different ticket's sequence.
    after = request.query_params.get("after")
    if cursor:
        try:
            if int(cursor["ticket_id"]) != ticket.id:
                raise ValueError
            queryset = queryset.filter(id__gt=int(cursor["id"]))
        except (KeyError, TypeError, ValueError):
            from rest_framework.exceptions import ValidationError
            raise ValidationError({"cursor": ["Invalid cursor."]})
    elif after:
        try:
            queryset = queryset.filter(id__gt=int(after))
        except ValueError:
            from rest_framework.exceptions import ValidationError
            raise ValidationError({"after": ["A whole number is required."]})
    rows = list(queryset[: limit + 1])
    more, rows = len(rows) > limit, rows[:limit]
    next_cursor = encode_cursor({"kind": "messages", "ticket_id": ticket.id, "id": rows[-1].id}) if more and rows else None
    return Response({"results": MessageSerializer(rows, many=True).data, "next_cursor": next_cursor})

@api_view(["GET", "POST"])
def tickets(request):
    if request.method == "GET":
        qs = Ticket.objects.select_related("customer", "assigned_agent").order_by("-id")
        if request.user.role == "customer":
            qs = qs.filter(customer=request.user)
        elif request.user.role == "agent":
            # Open tickets are deliberately discoverable to agents for claim;
            # all non-open tickets are limited to the assigned participant.
            qs = qs.filter(Q(status=Ticket.Status.OPEN, assigned_agent__isnull=True) | Q(assigned_agent=request.user))
        return paginated_tickets(request, qs)
    if request.user.role != "customer":
        return api_error("Only customers may create tickets", "role_forbidden", 403)
    serializer = TicketSerializer(data=request.data)
    serializer.is_valid(raise_exception=True)
    key = request.headers.get("Idempotency-Key", "").strip()
    if not key:
        return api_error("Idempotency-Key header is required", "idempotency_key_required", 400)
    if len(key) > 255:
        return api_error("Idempotency-Key must be at most 255 characters", "idempotency_key_invalid", 400)
    try:
        body, code, replayed = create_ticket_idempotently(user=request.user, key=key, payload=serializer.validated_data)
        return Response(body, status=code, headers={"Idempotency-Replayed": str(replayed).lower()})
    except IdempotencyConflict as exc:
        return api_error(str(exc), "idempotency_conflict", 409)
    except RequestInProgress as exc:
        return api_error(str(exc), "request_in_progress", 409, headers={"Retry-After": "1"})


@api_view(["GET"])
def ticket_statistics(request):
    """Cache-aside dashboard totals; authorization is evaluated before caching."""
    payload = {"tickets": get_ticket_statistics(request.user)}
    if request.user.role == "agent":
        payload["agent"] = get_agent_statistics(request.user)
    return Response(payload)


@api_view(["GET"])
def ticket_cache_metrics(request):
    if request.user.role != "admin":
        return api_error("Administrator role required", "role_forbidden", 403)
    return Response(cache_metrics())

@api_view(["POST"])
def claim(request, ticket_id: int):
    if request.user.role != "agent":
        return api_error("Agent role required", "role_forbidden", 403)
    try:
        return Response(TicketSerializer(claim_ticket(ticket_id=ticket_id, agent=request.user)).data)
    except TicketConflict as exc:
        return api_error(str(exc), "ticket_conflict", 409)


@api_view(["POST"])
def transition_status(request, ticket_id: int):
    ticket = get_object_or_404(Ticket, id=ticket_id)
    # Customers cannot change workflow state. Agents must be the assigned
    # participant; administrators may resolve for operational recovery.
    if request.user.role not in {"agent", "admin"} or (request.user.role == "agent" and ticket.assigned_agent_id != request.user.id):
        return api_error("Not permitted to transition this ticket", "participant_forbidden", 403)
    serializer = StatusTransitionSerializer(data=request.data)
    serializer.is_valid(raise_exception=True)
    try:
        resolved = resolve_ticket(ticket_id=ticket_id, actor=request.user, expected_version=serializer.validated_data["expected_version"])
        return Response(TicketSerializer(resolved).data)
    except TicketConflict as exc:
        return api_error(str(exc), "ticket_conflict", 409)

@api_view(["GET", "POST"])
def messages(request, ticket_id: int):
    ticket = get_object_or_404(Ticket, id=ticket_id)
    if not can_access_ticket(request.user, ticket):
        return api_error("Not a conversation participant", "participant_forbidden", 403)
    if request.method == "GET":
        return paginated_messages(request, ticket)
    serializer = MessageSerializer(data=request.data)
    serializer.is_valid(raise_exception=True)
    obj, created = ConversationMessage.objects.get_or_create(ticket=ticket, client_message_id=serializer.validated_data["client_message_id"], defaults={"sender": request.user, "body": serializer.validated_data["body"]})
    if not created and (obj.sender_id != request.user.id or obj.body != serializer.validated_data["body"]):
        return api_error("client_message_id was reused with different content", "message_id_conflict", 409)
    logger.info("message_create", extra={"event": "message_create", "outcome": "created" if created else "replayed"})
    return Response(MessageSerializer(obj).data, status=201 if created else 200)

@api_view(["POST"])
@permission_classes([IsAuthenticated])
def assistant_stream(request):
    data = AssistantRequestSerializer(data=request.data); data.is_valid(raise_exception=True)
    ticket = get_object_or_404(Ticket, id=data.validated_data["ticket_id"])
    if not can_access_ticket(request.user, ticket):
        return api_error("Not a conversation participant", "participant_forbidden", 403)
    provider = DeterministicLocalProvider(delay_seconds=settings.ASSISTANT_STREAM_TOKEN_DELAY_SECONDS)
    events = assistant_sse_events(
        provider,
        AssistantPrompt(ticket_id=ticket.id, question=data.validated_data["question"]),
        timeout_seconds=settings.ASSISTANT_STREAM_TIMEOUT_SECONDS,
        max_chunks=settings.ASSISTANT_STREAM_MAX_CHUNKS,
        max_output_chars=settings.ASSISTANT_STREAM_MAX_OUTPUT_CHARS,
    )
    response = StreamingHttpResponse(events, content_type="text/event-stream; charset=utf-8")
    response["Cache-Control"] = "no-cache, no-transform"; response["X-Accel-Buffering"] = "no"
    return response

@api_view(["GET"])
@permission_classes([])
def liveness(_request):
    """Process-only probe; it must stay healthy while dependencies recover."""
    return Response({"status": "ok"})


@api_view(["GET"])
@permission_classes([])
def readiness(_request):
    """Report whether this API can safely serve requests that need PG and Redis."""
    checks = {"database": "ok", "redis": "ok"}

    try:
        with connection.cursor() as cursor:
            cursor.execute("SELECT 1")
    except Exception:
        checks["database"] = "unavailable"

    try:
        get_redis_connection("default").ping()
    except Exception:
        checks["redis"] = "unavailable"

    ready = all(value == "ok" for value in checks.values())
    return Response(
        {"status": "ok" if ready else "unavailable", "checks": checks},
        status=status.HTTP_200_OK if ready else status.HTTP_503_SERVICE_UNAVAILABLE,
    )


# Retain the original health URL as a liveness alias for existing callers.
health = liveness
