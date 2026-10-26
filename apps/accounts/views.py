import hashlib
from datetime import timedelta

from django.conf import settings
from django.contrib.auth import authenticate, login, logout
from django.db import transaction
from django.utils import timezone
from django.utils.decorators import method_decorator
from django.views.decorators.csrf import ensure_csrf_cookie
from rest_framework import status
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import AllowAny
from rest_framework.response import Response

from .authentication import issue_token
from .models import ExpiringToken
from .serializers import LoginSerializer


def _ip_fingerprint(request):
    # REMOTE_ADDR is intentionally used rather than a spoofable forwarded
    # header. A deployment behind a proxy must set REMOTE_ADDR safely there.
    value = request.META.get("REMOTE_ADDR", "unknown")
    return hashlib.sha256(f"{settings.SECRET_KEY}:login:{value}".encode()).hexdigest()


@api_view(["GET"])
@permission_classes([AllowAny])
@ensure_csrf_cookie
def csrf(request):
    """Sets a CSRF cookie for session-authenticated browser clients."""
    return Response({"detail": "CSRF cookie set"})


@api_view(["POST"])
@permission_classes([AllowAny])
def login_view(request):
    from .models import LoginRateLimit

    now = timezone.now()
    fingerprint = _ip_fingerprint(request)
    with transaction.atomic():
        limiter, _ = LoginRateLimit.objects.select_for_update().get_or_create(
            fingerprint=fingerprint, defaults={"window_started_at": now, "attempts": 0}
        )
        if limiter.window_started_at <= now - timedelta(seconds=settings.LOGIN_RATE_LIMIT_SECONDS):
            limiter.window_started_at, limiter.attempts = now, 0
        if limiter.attempts >= settings.LOGIN_RATE_LIMIT_ATTEMPTS:
            retry_after = max(1, int((limiter.window_started_at + timedelta(seconds=settings.LOGIN_RATE_LIMIT_SECONDS) - now).total_seconds()))
            limiter.save(update_fields=["window_started_at", "attempts"])
            return Response({"detail": "Too many login attempts"}, status=status.HTTP_429_TOO_MANY_REQUESTS, headers={"Retry-After": str(retry_after)})
        limiter.attempts += 1
        limiter.save(update_fields=["window_started_at", "attempts"])

    serializer = LoginSerializer(data=request.data)
    serializer.is_valid(raise_exception=True)
    user = authenticate(request, username=serializer.validated_data["username"], password=serializer.validated_data["password"])
    if user is None or not user.is_active:
        return Response({"detail": "Invalid username or password"}, status=status.HTTP_400_BAD_REQUEST)

    # Successful logins do not remain in the failure budget.
    LoginRateLimit.objects.filter(fingerprint=fingerprint).update(attempts=0, window_started_at=now)
    login(request, user)
    payload = {"user": {"id": user.id, "username": user.username, "role": user.role}}
    if serializer.validated_data["issue_token"]:
        raw_token, token = issue_token(user)
        payload["token"] = raw_token
        payload["token_expires_at"] = token.expires_at
    return Response(payload)


@api_view(["POST"])
def logout_view(request):
    # Revoke only the bearer credential presented by this request. Other API
    # clients remain usable until individually revoked/expired.
    if isinstance(request.auth, ExpiringToken):
        ExpiringToken.objects.filter(pk=request.auth.pk, revoked_at__isnull=True).update(revoked_at=timezone.now())
    logout(request)
    return Response(status=status.HTTP_204_NO_CONTENT)
