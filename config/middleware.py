from django.conf import settings
from django.http import HttpResponse, JsonResponse
from django.utils.cache import patch_vary_headers


class RestrictedCORSMiddleware:
    """Small explicit CORS policy for the API's cookie-authenticated SPA."""

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        origin = request.headers.get("Origin", "").rstrip("/")
        allowed = bool(origin and origin in settings.CORS_ALLOWED_ORIGINS)

        if request.method == "OPTIONS" and origin:
            if not allowed:
                return JsonResponse({"detail": "Origin is not allowed"}, status=403)
            response = HttpResponse(status=204)
            return self._add_headers(response, origin)

        # An untrusted browser origin must not make a cookie-authenticated
        # state-changing request. Requests without Origin are API clients and
        # are authenticated independently (e.g. a Bearer token).
        if origin and not allowed and request.method not in {"GET", "HEAD", "OPTIONS"}:
            return JsonResponse({"detail": "Origin is not allowed"}, status=403)

        response = self.get_response(request)
        return self._add_headers(response, origin) if allowed else response

    @staticmethod
    def _add_headers(response, origin):
        response["Access-Control-Allow-Origin"] = origin
        response["Access-Control-Allow-Credentials"] = "true"
        response["Access-Control-Allow-Methods"] = ", ".join(settings.CORS_ALLOWED_METHODS)
        response["Access-Control-Allow-Headers"] = ", ".join(settings.CORS_ALLOWED_HEADERS)
        patch_vary_headers(response, ("Origin",))
        return response
