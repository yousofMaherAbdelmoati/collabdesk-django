"""Redis integration coverage for the cache lock; run with Compose services."""
import concurrent.futures
import threading
import time
import uuid

import pytest
from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.test import override_settings

from apps.desk import cache as desk_cache
from apps.desk.cache import get_ticket_statistics, ticket_statistics_cache_key


@pytest.mark.redis
@pytest.mark.django_db
def test_redis_cache_writes_statistics_and_releases_only_its_own_lock():
    # A unique prefix keeps this local integration test isolated from Channels,
    # Celery, and any other fictional local test run sharing Redis.
    prefix = f"collabdesk-cache-test-{uuid.uuid4().hex}"
    caches = {
        "default": {
            "BACKEND": "django_redis.cache.RedisCache",
            "LOCATION": settings.REDIS_URL,
            "KEY_PREFIX": prefix,
            "VERSION": settings.CACHE_VERSION,
            "OPTIONS": {
                "CLIENT_CLASS": "django_redis.client.DefaultClient",
                "SOCKET_CONNECT_TIMEOUT": 0.25,
                "SOCKET_TIMEOUT": 0.25,
                "IGNORE_EXCEPTIONS": False,
            },
        }
    }
    with override_settings(CACHES=caches):
        customer = get_user_model().objects.create_user(username=f"redis-cache-{uuid.uuid4().hex}", password="x", role="customer")
        result = get_ticket_statistics(customer)
        key = ticket_statistics_cache_key(customer)
        assert result["total"] == 0
        assert cache.get(key) == result
        # The lock is removed with Redis compare-and-delete, not a racy delete.
        assert cache.get(f"{key}:lock") is None

        calls = 0
        calls_lock = threading.Lock()

        def query():
            nonlocal calls
            with calls_lock:
                calls += 1
            time.sleep(0.05)
            return {"source": "fictional-local-test"}

        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
            values = list(pool.map(lambda _index: desk_cache._cache_aside("redis-stampede", query), range(8)))
        assert values == [{"source": "fictional-local-test"}] * 8
        assert calls == 1
