"""Opaque, signed keyset cursors for ticket APIs."""
from django.core import signing
from rest_framework.exceptions import ValidationError


CURSOR_SALT = "collabdesk.ticket-api.cursor.v1"


def encode_cursor(value: dict) -> str:
    return signing.dumps(value, salt=CURSOR_SALT, compress=True)


def decode_cursor(raw: str | None, *, kind: str) -> dict | None:
    if not raw:
        return None
    try:
        value = signing.loads(raw, salt=CURSOR_SALT)
    except signing.BadSignature as exc:
        raise ValidationError({"cursor": ["Invalid cursor."]}) from exc
    if not isinstance(value, dict) or value.get("kind") != kind:
        raise ValidationError({"cursor": ["Invalid cursor."]})
    return value


def page_size(raw: str | None, *, default: int = 50, maximum: int = 100) -> int:
    if raw is None:
        return default
    try:
        value = int(raw)
    except (TypeError, ValueError) as exc:
        raise ValidationError({"limit": ["A whole number is required."]}) from exc
    if not 1 <= value <= maximum:
        raise ValidationError({"limit": [f"Must be between 1 and {maximum}."]})
    return value
