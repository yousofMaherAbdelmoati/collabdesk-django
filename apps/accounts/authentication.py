import hashlib
import secrets
from datetime import timedelta

from django.conf import settings
from django.utils import timezone
from rest_framework import authentication, exceptions

from .models import ExpiringToken


def digest_token(raw_token: str) -> str:
    return hashlib.sha256(raw_token.encode("utf-8")).hexdigest()


def issue_token(user):
    raw_token = secrets.token_urlsafe(32)
    token = ExpiringToken.objects.create(
        user=user,
        key_digest=digest_token(raw_token),
        expires_at=timezone.now() + timedelta(seconds=settings.AUTH_TOKEN_TTL_SECONDS),
    )
    return raw_token, token


class ExpiringTokenAuthentication(authentication.BaseAuthentication):
    keyword = "Bearer"

    def authenticate(self, request):
        header = authentication.get_authorization_header(request).split()
        if not header:
            return None
        if header[0].lower() != self.keyword.lower().encode():
            return None
        if len(header) != 2:
            raise exceptions.AuthenticationFailed("Invalid Authorization header")
        try:
            raw_token = header[1].decode("ascii")
        except UnicodeError as exc:
            raise exceptions.AuthenticationFailed("Invalid token") from exc
        try:
            token = ExpiringToken.objects.select_related("user").get(key_digest=digest_token(raw_token))
        except ExpiringToken.DoesNotExist as exc:
            raise exceptions.AuthenticationFailed("Invalid token") from exc
        if token.revoked_at is not None or token.expires_at <= timezone.now() or not token.user.is_active:
            raise exceptions.AuthenticationFailed("Token is expired or revoked")
        ExpiringToken.objects.filter(pk=token.pk).update(last_used_at=timezone.now())
        return token.user, token

    def authenticate_header(self, request):
        return self.keyword
