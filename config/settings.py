from datetime import timedelta
from pathlib import Path
import os
from django.core.exceptions import ImproperlyConfigured

BASE_DIR = Path(__file__).resolve().parent.parent
DJANGO_ENV = os.getenv("DJANGO_ENV", "").lower()
SECRET_KEY = os.getenv("DJANGO_SECRET_KEY", "unsafe-development-only-change-me")
DEBUG = os.getenv("DJANGO_DEBUG", "false").lower() == "true"
if SECRET_KEY == "unsafe-development-only-change-me" and DJANGO_ENV not in {"development", "dev", "local", "test"}:
    raise ImproperlyConfigured("DJANGO_SECRET_KEY must be set outside local development and tests")
ALLOWED_HOSTS = os.getenv("DJANGO_ALLOWED_HOSTS", "localhost,127.0.0.1").split(",")
INSTALLED_APPS = [
    "daphne", "django.contrib.admin", "django.contrib.auth", "django.contrib.contenttypes",
    "django.contrib.sessions", "django.contrib.messages", "django.contrib.staticfiles",
    "rest_framework", "rest_framework.authtoken", "drf_spectacular", "channels",
    "apps.accounts", "apps.desk.apps.DeskConfig", "apps.reports",
]
MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware", "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware", "config.middleware.RestrictedCORSMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware", "django.contrib.messages.middleware.MessageMiddleware",
]
ROOT_URLCONF = "config.urls"
TEMPLATES = [{"BACKEND": "django.template.backends.django.DjangoTemplates", "DIRS": [BASE_DIR / "frontend"], "APP_DIRS": True, "OPTIONS": {"context_processors": ["django.template.context_processors.request", "django.contrib.auth.context_processors.auth", "django.contrib.messages.context_processors.messages"]}}]
WSGI_APPLICATION = "config.wsgi.application"
ASGI_APPLICATION = "config.asgi.application"
# SQLite is intentionally opt-in for local unit tests only.  PostgreSQL is the
# default because ticket claim and idempotency correctness rely on its
# transaction semantics.
USE_SQLITE = os.getenv("USE_SQLITE") == "1"
if USE_SQLITE and os.getenv("DJANGO_ENV") != "test":
    raise ImproperlyConfigured("USE_SQLITE=1 is only supported for local test runs (DJANGO_ENV=test)")

DATABASES = {
    "default": (
        {"ENGINE": "django.db.backends.sqlite3", "NAME": str(BASE_DIR / "test.sqlite3")}
        if USE_SQLITE
        else {
            "ENGINE": "django.db.backends.postgresql",
            "NAME": os.getenv("POSTGRES_DB", "collabdesk"),
            "USER": os.getenv("POSTGRES_USER", "collabdesk"),
            "PASSWORD": os.getenv("POSTGRES_PASSWORD", "collabdesk"),
            "HOST": os.getenv("POSTGRES_HOST", "db"),
            "PORT": os.getenv("POSTGRES_PORT", "5432"),
            "CONN_MAX_AGE": 60,
            # An explicit, local-only test database avoids Django deriving a
            # name from an environment-specific database name.
            "TEST": {"NAME": os.getenv("POSTGRES_TEST_DB", "test_collabdesk")},
        }
    )
}
AUTH_USER_MODEL = "accounts.User"
REST_FRAMEWORK = {
    # Browser clients use the Django session cookie.  Non-browser clients may
    # use a short-lived, revocable Bearer token issued by the login endpoint.
    "DEFAULT_AUTHENTICATION_CLASSES": [
        "apps.accounts.authentication.ExpiringTokenAuthentication",
        "rest_framework.authentication.SessionAuthentication",
    ],
    "DEFAULT_PERMISSION_CLASSES": ["rest_framework.permissions.IsAuthenticated"],
    "DEFAULT_SCHEMA_CLASS": "drf_spectacular.openapi.AutoSchema",
}
SPECTACULAR_SETTINGS = {"TITLE": "CollabDesk Django API", "VERSION": "1.0.0"}
REDIS_URL = os.getenv("REDIS_URL", "redis://redis:6379/0")
# Cache keys are intentionally separate from Redis keys used by Channels and
# Celery. Bumping CACHE_VERSION provides an operational cache cut-over without
# a Redis flush; CACHE_NAMESPACE makes shared Redis deployments safe.
CACHE_NAMESPACE = os.getenv("CACHE_NAMESPACE", "collabdesk")
CACHE_VERSION = int(os.getenv("CACHE_VERSION", "1"))
TICKET_STATS_CACHE_TTL_SECONDS = int(os.getenv("TICKET_STATS_CACHE_TTL_SECONDS", "60"))
TICKET_STATS_CACHE_LOCK_TTL_SECONDS = int(os.getenv("TICKET_STATS_CACHE_LOCK_TTL_SECONDS", "3"))
TICKET_STATS_CACHE_LOCK_WAIT_SECONDS = float(os.getenv("TICKET_STATS_CACHE_LOCK_WAIT_SECONDS", "0.25"))
if (
    not CACHE_NAMESPACE
    or CACHE_VERSION < 1
    or TICKET_STATS_CACHE_TTL_SECONDS < 1
    or TICKET_STATS_CACHE_LOCK_TTL_SECONDS < 1
    or TICKET_STATS_CACHE_LOCK_WAIT_SECONDS < 0
):
    raise ImproperlyConfigured("Cache namespace, version, TTL, and lock settings must be positive")
CACHES = {"default": {"BACKEND": "django_redis.cache.RedisCache", "LOCATION": REDIS_URL, "KEY_PREFIX": CACHE_NAMESPACE, "VERSION": CACHE_VERSION, "OPTIONS": {"CLIENT_CLASS": "django_redis.client.DefaultClient", "SOCKET_CONNECT_TIMEOUT": 0.25, "SOCKET_TIMEOUT": 0.25, "IGNORE_EXCEPTIONS": True}}}
# Live socket notifications are intentionally bounded and non-durable.  The
# database message log plus websocket history cursors is the recovery path if
# a slow consumer's Redis channel reaches this capacity.
CHANNEL_LAYERS = {"default": {"BACKEND": "channels_redis.core.RedisChannelLayer", "CONFIG": {"hosts": [REDIS_URL], "capacity": int(os.getenv("CHANNEL_LAYER_CAPACITY", "100"))}}}
CELERY_BROKER_URL = REDIS_URL
CELERY_RESULT_BACKEND = None
CELERY_TASK_ACKS_LATE = True
CELERY_TASK_REJECT_ON_WORKER_LOST = True
CELERY_TASK_TIME_LIMIT = 300
# Reports are DB-owned work.  Celery is an at-least-once transport, with the
# database outbox and watchdog providing recovery when Redis is unavailable.
REPORT_JOB_LEASE_SECONDS = int(os.getenv("REPORT_JOB_LEASE_SECONDS", "90"))
REPORT_HEARTBEAT_SECONDS = int(os.getenv("REPORT_HEARTBEAT_SECONDS", "15"))
REPORT_MAX_ATTEMPTS = int(os.getenv("REPORT_MAX_ATTEMPTS", "3"))
REPORT_RETRY_BACKOFF_SECONDS = int(os.getenv("REPORT_RETRY_BACKOFF_SECONDS", "5"))
REPORT_RETRY_MAX_SECONDS = int(os.getenv("REPORT_RETRY_MAX_SECONDS", "300"))
REPORT_RECOVERY_BATCH_SIZE = int(os.getenv("REPORT_RECOVERY_BATCH_SIZE", "100"))
OUTBOX_LEASE_SECONDS = int(os.getenv("OUTBOX_LEASE_SECONDS", "30"))
OUTBOX_MAX_ATTEMPTS = int(os.getenv("OUTBOX_MAX_ATTEMPTS", "12"))
OUTBOX_RETRY_BACKOFF_SECONDS = int(os.getenv("OUTBOX_RETRY_BACKOFF_SECONDS", "5"))
OUTBOX_RETRY_MAX_SECONDS = int(os.getenv("OUTBOX_RETRY_MAX_SECONDS", "300"))
OUTBOX_PUBLISH_BATCH_SIZE = int(os.getenv("OUTBOX_PUBLISH_BATCH_SIZE", "100"))
OUTBOX_PUBLISH_INTERVAL_SECONDS = int(os.getenv("OUTBOX_PUBLISH_INTERVAL_SECONDS", "5"))
REPORT_RECOVERY_INTERVAL_SECONDS = int(os.getenv("REPORT_RECOVERY_INTERVAL_SECONDS", "15"))
REPORT_RESULT_TTL_SECONDS = int(os.getenv("REPORT_RESULT_TTL_SECONDS", str(24 * 60 * 60)))
REPORT_SUMMARY_REFRESH_INTERVAL_SECONDS = int(os.getenv("REPORT_SUMMARY_REFRESH_INTERVAL_SECONDS", str(60 * 60)))
if (
    REPORT_JOB_LEASE_SECONDS < 2
    or REPORT_HEARTBEAT_SECONDS < 1
    or REPORT_HEARTBEAT_SECONDS >= REPORT_JOB_LEASE_SECONDS
    or REPORT_MAX_ATTEMPTS < 1
    or REPORT_RETRY_BACKOFF_SECONDS < 1
    or REPORT_RETRY_MAX_SECONDS < REPORT_RETRY_BACKOFF_SECONDS
    or REPORT_RECOVERY_BATCH_SIZE < 1
    or OUTBOX_LEASE_SECONDS < 1
    or OUTBOX_MAX_ATTEMPTS < 1
    or OUTBOX_RETRY_BACKOFF_SECONDS < 1
    or OUTBOX_RETRY_MAX_SECONDS < OUTBOX_RETRY_BACKOFF_SECONDS
    or OUTBOX_PUBLISH_BATCH_SIZE < 1
    or OUTBOX_PUBLISH_INTERVAL_SECONDS < 1
    or REPORT_RECOVERY_INTERVAL_SECONDS < 1
    or REPORT_RESULT_TTL_SECONDS < 1
    or REPORT_SUMMARY_REFRESH_INTERVAL_SECONDS < 1
):
    raise ImproperlyConfigured("Report and outbox lease, retry, batch, and interval settings are invalid")
CELERY_BEAT_SCHEDULE = {
    "publish-database-outbox": {
        "task": "apps.reports.publish_outbox_events",
        "schedule": timedelta(seconds=OUTBOX_PUBLISH_INTERVAL_SECONDS),
    },
    "recover-stale-report-jobs": {
        "task": "apps.reports.recover_stale_report_jobs",
        "schedule": timedelta(seconds=REPORT_RECOVERY_INTERVAL_SECONDS),
    },
    "refresh-report-analytics-summaries": {
        "task": "apps.reports.refresh_analytics_summaries",
        "schedule": timedelta(seconds=REPORT_SUMMARY_REFRESH_INTERVAL_SECONDS),
    },
    "expire-report-results": {
        "task": "apps.reports.expire_report_results",
        "schedule": timedelta(seconds=REPORT_SUMMARY_REFRESH_INTERVAL_SECONDS),
    },
}
STATIC_URL = "/static/"
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"
def _origins(name: str, default: str) -> list[str]:
    return [origin.strip().rstrip("/") for origin in os.getenv(name, default).split(",") if origin.strip()]


# Keep browser origins explicit.  CORS does not authorize a request; CSRF
# protects unsafe requests made with the HttpOnly session cookie.
# The small demonstration client is served by this Django process in Compose.
# Keep its explicit local origins alongside the optional separate port-3000
# development client; deployment environments still supply their own lists.
CSRF_TRUSTED_ORIGINS = _origins(
    "CSRF_TRUSTED_ORIGINS",
    "http://localhost:3000,http://localhost:8000,http://127.0.0.1:8000",
)
CORS_ALLOWED_ORIGINS = _origins(
    "CORS_ALLOWED_ORIGINS",
    "http://localhost:3000,http://localhost:8000,http://127.0.0.1:8000",
)
WEBSOCKET_ALLOWED_ORIGINS = _origins(
    "WEBSOCKET_ALLOWED_ORIGINS",
    "http://localhost:3000,http://localhost:8000,http://127.0.0.1:8000",
)
CORS_ALLOW_CREDENTIALS = True
CORS_ALLOWED_METHODS = ("GET", "POST", "OPTIONS")
CORS_ALLOWED_HEADERS = ("authorization", "content-type", "idempotency-key", "x-csrftoken")
if not set(CORS_ALLOWED_ORIGINS).issubset(CSRF_TRUSTED_ORIGINS):
    raise ImproperlyConfigured("Every credentialed CORS origin must also be a CSRF trusted origin")

# Sessions are the only browser authentication mechanism.  Secure cookies
# are enabled outside development; local HTTP remains usable in Compose.
SESSION_COOKIE_HTTPONLY = True
SESSION_COOKIE_SAMESITE = "Lax"
SESSION_COOKIE_SECURE = os.getenv("SESSION_COOKIE_SECURE", "true" if not DEBUG else "false").lower() == "true"
CSRF_COOKIE_SECURE = os.getenv("CSRF_COOKIE_SECURE", "true" if not DEBUG else "false").lower() == "true"
SESSION_COOKIE_AGE = int(os.getenv("SESSION_COOKIE_AGE", str(8 * 60 * 60)))
SESSION_SAVE_EVERY_REQUEST = False
AUTH_TOKEN_TTL_SECONDS = int(os.getenv("AUTH_TOKEN_TTL_SECONDS", str(15 * 60)))
LOGIN_RATE_LIMIT_ATTEMPTS = int(os.getenv("LOGIN_RATE_LIMIT_ATTEMPTS", "5"))
LOGIN_RATE_LIMIT_SECONDS = int(os.getenv("LOGIN_RATE_LIMIT_SECONDS", "60"))
# The local assistant stream remains bounded even if a future provider behaves
# badly. Values are intentionally configurable for deployment-specific budgets.
ASSISTANT_STREAM_TIMEOUT_SECONDS = float(os.getenv("ASSISTANT_STREAM_TIMEOUT_SECONDS", "15"))
ASSISTANT_STREAM_MAX_CHUNKS = int(os.getenv("ASSISTANT_STREAM_MAX_CHUNKS", "32"))
ASSISTANT_STREAM_MAX_OUTPUT_CHARS = int(os.getenv("ASSISTANT_STREAM_MAX_OUTPUT_CHARS", "4096"))
ASSISTANT_STREAM_TOKEN_DELAY_SECONDS = float(os.getenv("ASSISTANT_STREAM_TOKEN_DELAY_SECONDS", "0.005"))
if (
    ASSISTANT_STREAM_TIMEOUT_SECONDS <= 0
    or ASSISTANT_STREAM_MAX_CHUNKS <= 0
    or ASSISTANT_STREAM_MAX_OUTPUT_CHARS <= 0
    or ASSISTANT_STREAM_TOKEN_DELAY_SECONDS < 0
):
    raise ImproperlyConfigured("Assistant stream limits must be positive (delay may be zero)")
LOCAL_DEMO_SEED_ENABLED = DJANGO_ENV in {"development", "dev", "local", "test"}

# Ticket API audit events deliberately contain operation/outcome only. Do not
# add request bodies, idempotency keys, user identifiers, message text, or
# ticket text here: those are not needed to diagnose API behavior.
LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "formatters": {"json": {"()": "config.logging.JsonFormatter"}},
    "handlers": {"ticket_api": {"class": "logging.StreamHandler", "formatter": "json"}},
    "loggers": {"collabdesk.ticket_api": {"handlers": ["ticket_api"], "level": "INFO", "propagate": False}},
}
