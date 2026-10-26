import os
import pytest
from asgiref.sync import SyncToAsync
from django.db import connections


def pytest_configure(config):
    """Do not retain thread-local PostgreSQL connections between test calls."""
    from django.conf import settings

    settings.DATABASES["default"]["CONN_MAX_AGE"] = 0


def pytest_collection_modifyitems(config, items):
    if os.getenv("USE_SQLITE") == "1":
        marker = pytest.mark.skip(reason="requires PostgreSQL concurrent transaction semantics")
        redis_marker = pytest.mark.skip(reason="requires a reachable Redis service")
        for item in items:
            if "postgres" in item.keywords:
                item.add_marker(marker)
            if "redis" in item.keywords:
                item.add_marker(redis_marker)


@pytest.fixture(scope="session", autouse=True)
def close_connections_before_test_database_teardown(django_db_setup, django_db_blocker):
    """Release ASGI's worker-thread connection before pytest drops PostgreSQL.

    ``database_sync_to_async`` uses asgiref's long-lived thread-sensitive
    executor. Closing only the main thread's Django connection can leave that
    executor attached to ``test_collabdesk`` when pytest-django tears the
    database down after a WebSocket/service run.
    """
    yield
    with django_db_blocker.unblock():
        connections.close_all()
        SyncToAsync.single_thread_executor.submit(connections.close_all).result()
