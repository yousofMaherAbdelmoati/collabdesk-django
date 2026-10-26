import concurrent.futures
import threading
import time
import uuid
from unittest.mock import patch

import pytest
from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.test import override_settings
from rest_framework.test import APIClient

from apps.desk import cache as desk_cache
from apps.desk.models import ConversationMessage, Ticket
from apps.desk.services import claim_ticket


TEST_CACHES = {
    "default": {
        "BACKEND": "django.core.cache.backends.locmem.LocMemCache",
        "LOCATION": "ticket-statistics-tests",
        "KEY_PREFIX": "fictional-collabdesk",
        "VERSION": 37,
    }
}


def client_for(user):
    client = APIClient()
    client.force_authenticate(user)
    return client


@pytest.fixture(autouse=True)
def isolated_cache(settings):
    settings.CACHES = TEST_CACHES
    cache.clear()
    yield
    cache.clear()


@pytest.fixture
def customer(db):
    return get_user_model().objects.create_user(username="cache-customer", password="x", role="customer")


@pytest.fixture
def other_customer(db):
    return get_user_model().objects.create_user(username="cache-other", password="x", role="customer")


@pytest.fixture
def agent(db):
    return get_user_model().objects.create_user(username="cache-agent", password="x", role="agent")


def test_ticket_statistics_are_cached_with_configured_namespace_and_version(customer):
    first = desk_cache.get_ticket_statistics(customer)
    key = desk_cache.ticket_statistics_cache_key(customer)

    assert first == {"total": 0, "by_status": {"open": 0, "claimed": 0, "resolved": 0}, "message_count": 0}
    assert cache.make_key(key).startswith("fictional-collabdesk:37:")

    with patch("apps.desk.cache._ticket_statistics_from_database", side_effect=AssertionError("expected cache hit")):
        assert desk_cache.get_ticket_statistics(customer) == first


@pytest.mark.django_db(transaction=True)
def test_ticket_and_message_changes_invalidate_statistics(customer, agent):
    before_key = desk_cache.ticket_statistics_cache_key(customer)
    assert desk_cache.get_ticket_statistics(customer)["total"] == 0

    ticket = Ticket.objects.create(customer=customer, title="fictional", description="fictional")
    after_ticket_key = desk_cache.ticket_statistics_cache_key(customer)
    assert after_ticket_key != before_key
    assert desk_cache.get_ticket_statistics(customer)["by_status"]["open"] == 1

    message = ConversationMessage.objects.create(ticket=ticket, sender=customer, client_message_id=uuid.uuid4(), body="fictional")
    after_message_key = desk_cache.ticket_statistics_cache_key(customer)
    assert after_message_key != after_ticket_key
    assert desk_cache.get_ticket_statistics(customer)["message_count"] == 1

    message.body = "edited fictional"
    message.save(update_fields=["body"])
    after_message_update_key = desk_cache.ticket_statistics_cache_key(customer)
    assert after_message_update_key != after_message_key
    message.delete()
    assert desk_cache.ticket_statistics_cache_key(customer) != after_message_update_key
    assert desk_cache.get_ticket_statistics(customer)["message_count"] == 0

    # Bulk workflow changes bypass Django model signals, so this verifies the
    # explicit post-commit invalidation in the service layer.
    agent_key = desk_cache.ticket_statistics_cache_key(agent)
    assert desk_cache.get_ticket_statistics(agent)["by_status"]["open"] == 1
    claim_ticket(ticket_id=ticket.id, agent=agent)
    assert desk_cache.ticket_statistics_cache_key(agent) != agent_key
    assert desk_cache.get_agent_statistics(agent)["by_status"]["claimed"] == 1


def test_statistics_keys_and_values_are_user_specific(customer, other_customer, agent):
    own = Ticket.objects.create(customer=customer, assigned_agent=agent, status=Ticket.Status.CLAIMED, title="own", description="d")
    Ticket.objects.create(customer=other_customer, title="private", description="d")
    ConversationMessage.objects.create(ticket=own, sender=customer, client_message_id=uuid.uuid4(), body="private fictional text")

    assert desk_cache.ticket_statistics_cache_key(customer) != desk_cache.ticket_statistics_cache_key(other_customer)
    assert desk_cache.get_ticket_statistics(customer)["total"] == 1
    assert desk_cache.get_ticket_statistics(other_customer)["total"] == 1
    assert desk_cache.get_ticket_statistics(customer)["message_count"] == 1
    assert desk_cache.get_ticket_statistics(other_customer)["message_count"] == 0

    response = client_for(customer).get("/api/tickets/stats")
    assert response.status_code == 200
    assert response.data["tickets"]["message_count"] == 1
    assert client_for(other_customer).get("/api/tickets/stats").data["tickets"]["message_count"] == 0


def test_agent_statistics_only_count_that_agents_assignments(customer, agent):
    other_agent = get_user_model().objects.create_user(username="cache-agent-2", password="x", role="agent")
    Ticket.objects.create(customer=customer, assigned_agent=agent, status=Ticket.Status.CLAIMED, title="mine", description="d")
    Ticket.objects.create(customer=customer, assigned_agent=other_agent, status=Ticket.Status.CLAIMED, title="theirs", description="d")

    assert desk_cache.agent_statistics_cache_key(agent) != desk_cache.agent_statistics_cache_key(other_agent)
    assert desk_cache.get_agent_statistics(agent)["total"] == 1
    response = client_for(agent).get("/api/tickets/stats")
    assert response.status_code == 200
    assert response.data["agent"]["total"] == 1


def test_concurrent_cache_miss_has_one_database_builder(settings):
    settings.TICKET_STATS_CACHE_LOCK_WAIT_SECONDS = 0.5
    calls = 0
    calls_lock = threading.Lock()

    def query():
        nonlocal calls
        with calls_lock:
            calls += 1
        time.sleep(0.05)
        return {"fictional": "value"}

    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
        values = list(pool.map(lambda _index: desk_cache._cache_aside("stampede-test", query), range(8)))

    assert values == [{"fictional": "value"}] * 8
    assert calls == 1


def test_cache_outage_falls_back_to_postgresql(customer):
    Ticket.objects.create(customer=customer, title="durable", description="database remains authoritative")

    with patch.object(desk_cache.cache, "get", side_effect=ConnectionError("fictional Redis outage")), patch.object(
        desk_cache.cache, "add", side_effect=ConnectionError("fictional Redis outage")
    ):
        result = desk_cache.get_ticket_statistics(customer)

    assert result["total"] == 1
    assert result["by_status"]["open"] == 1


def test_cache_metrics_record_hits_and_misses(customer):
    desk_cache.get_ticket_statistics(customer)
    desk_cache.get_ticket_statistics(customer)
    after = desk_cache.cache_metrics()

    assert after["misses"] >= 1
    assert after["hits"] >= 1
