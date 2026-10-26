import os

# Django must initialize its app registry before importing websocket routing:
# the consumer imports ORM-backed token and desk models at module load time.
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")

from channels.auth import AuthMiddlewareStack
from channels.routing import ProtocolTypeRouter, URLRouter
from channels.security.websocket import OriginValidator
from django.core.asgi import get_asgi_application
from django.conf import settings

django_asgi_application = get_asgi_application()

from apps.desk.routing import websocket_urlpatterns

application = ProtocolTypeRouter({
    "http": django_asgi_application,
    # Browser sockets authenticate with the HttpOnly Django session cookie.
    # OriginValidator rejects cross-site websocket handshakes before routing.
    "websocket": OriginValidator(AuthMiddlewareStack(URLRouter(websocket_urlpatterns)), settings.WEBSOCKET_ALLOWED_ORIGINS),
})
