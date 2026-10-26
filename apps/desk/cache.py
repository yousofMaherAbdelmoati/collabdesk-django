"""Best-effort, cache-aside statistics for the desk.

PostgreSQL remains authoritative. Every function in this module can return a
fresh database result when Redis is slow, unavailable, or contended. Cache
contents are never used for authorization decisions.
"""
from __future__ import annotations

import secrets
import threading
import time
from typing import Callable

from django.conf import settings
from django.core.cache import cache
from django.db.models import Count, Q, QuerySet

from .models import ConversationMessage, Ticket


_REVISION_KEY = "ticket-stats:revision"
_METRIC_HITS_KEY = "ticket-stats:metrics:hits"
_METRIC_MISSES_KEY = "ticket-stats:metrics:misses"
_local_metrics = {"hits": 0, "misses": 0}
_metrics_lock = threading.Lock()


def _statuses() -> dict[str, int]:
    return {choice: 0 for choice, _label in Ticket.Status.choices}


def _cache_key(kind: str, user_id: int, role: str, revision: int) -> str:
    """Return a logical key; Django adds configured namespace and version."""
    return f"ticket-stats:{kind}:user:{user_id}:role:{role}:revision:{revision}"


def ticket_statistics_cache_key(user, revision: int | None = None) -> str:
    """Public key builder used by operations and tests; it contains no PII."""
    return _cache_key("tickets", user.id, user.role, _cache_revision() if revision is None else revision)


def agent_statistics_cache_key(user, revision: int | None = None) -> str:
    return _cache_key("agent", user.id, user.role, _cache_revision() if revision is None else revision)


def _cache_revision() -> int:
    """Obtain a generation number, treating cache trouble as generation zero."""
    try:
        revision = cache.get(_REVISION_KEY)
        if isinstance(revision, int) and revision >= 1:
            return revision
        cache.add(_REVISION_KEY, 1, timeout=None)
        revision = cache.get(_REVISION_KEY)
        return revision if isinstance(revision, int) and revision >= 1 else 0
    except Exception:
        return 0


def invalidate_ticket_statistics() -> None:
    """Advance the cache generation after a committed ticket/message change.

    Old values are left to their TTL. Because their generation is part of each
    lookup key, they cannot be returned after a successful invalidation.
    """
    try:
        # Initializing first means the invalidating write always advances from
        # the generation a concurrent reader could have observed.
        cache.add(_REVISION_KEY, 1, timeout=None)
        cache.incr(_REVISION_KEY)
    except Exception:
        # A cache outage must not roll back or alter the durable write.
        return


def _increment_metric(name: str) -> None:
    with _metrics_lock:
        _local_metrics[name] += 1
    key = _METRIC_HITS_KEY if name == "hits" else _METRIC_MISSES_KEY
    try:
        cache.add(key, 0, timeout=None)
        cache.incr(key)
    except Exception:
        pass


def cache_metrics() -> dict[str, int]:
    """Return Redis counters where possible, otherwise process-local counters."""
    with _metrics_lock:
        fallback = dict(_local_metrics)
    try:
        hits, misses = cache.get(_METRIC_HITS_KEY), cache.get(_METRIC_MISSES_KEY)
        if isinstance(hits, int) and isinstance(misses, int):
            return {"hits": hits, "misses": misses}
    except Exception:
        pass
    return fallback


def _visible_ticket_queryset(user) -> QuerySet:
    if user.role == "admin":
        return Ticket.objects.all()
    if user.role == "customer":
        return Ticket.objects.filter(customer_id=user.id)
    if user.role == "agent":
        return Ticket.objects.filter(
            Q(status=Ticket.Status.OPEN, assigned_agent__isnull=True) | Q(assigned_agent_id=user.id)
        )
    return Ticket.objects.none()


def _participant_ticket_queryset(user) -> QuerySet:
    # An agent can discover open tickets but cannot read their conversations.
    # Message totals therefore use participant access, not ticket-list access.
    if user.role == "admin":
        return Ticket.objects.all()
    return Ticket.objects.filter(Q(customer_id=user.id) | Q(assigned_agent_id=user.id))


def _statistics(ticket_queryset: QuerySet, message_queryset: QuerySet) -> dict:
    by_status = _statuses()
    for row in ticket_queryset.values("status").annotate(total=Count("id")):
        by_status[row["status"]] = row["total"]
    return {
        "total": sum(by_status.values()),
        "by_status": by_status,
        "message_count": ConversationMessage.objects.filter(
            ticket_id__in=message_queryset.values("id")
        ).count(),
    }


def _ticket_statistics_from_database(user) -> dict:
    return _statistics(_visible_ticket_queryset(user), _participant_ticket_queryset(user))


def _agent_statistics_from_database(user) -> dict:
    # This is deliberately scoped to one agent. A cache entry never carries
    # aggregate workload data from another agent.
    assigned = Ticket.objects.filter(assigned_agent_id=user.id)
    return _statistics(assigned, assigned)


def _release_redis_lock(lock_key: str, token: str) -> None:
    """Delete only our Redis lock using an atomic compare-and-delete script."""
    try:
        client_api = cache.client
        client = client_api.get_client(write=True)
        encoded_token = client_api.encode(token)
        client.eval(
            "if redis.call('get', KEYS[1]) == ARGV[1] then "
            "return redis.call('del', KEYS[1]) else return 0 end",
            1,
            client_api.make_key(lock_key),
            encoded_token,
        )
    except Exception:
        # The lock has a short TTL. Never use an unsafe unconditional delete.
        return


def _cache_aside(key: str, query: Callable[[], dict]) -> dict:
    try:
        cached = cache.get(key)
        if cached is not None:
            _increment_metric("hits")
            return cached
        _increment_metric("misses")

        lock_key, token = f"{key}:lock", secrets.token_urlsafe(16)
        if cache.add(lock_key, token, timeout=settings.TICKET_STATS_CACHE_LOCK_TTL_SECONDS):
            try:
                value = query()
                # A failed set is harmless: the database result is still safe.
                cache.set(key, value, timeout=settings.TICKET_STATS_CACHE_TTL_SECONDS)
                return value
            finally:
                _release_redis_lock(lock_key, token)

        # Another request is filling this key. Bound the wait, then fall back
        # to PostgreSQL if it did not finish: availability beats stale data.
        deadline = time.monotonic() + settings.TICKET_STATS_CACHE_LOCK_WAIT_SECONDS
        while time.monotonic() < deadline:
            time.sleep(0.01)
            cached = cache.get(key)
            if cached is not None:
                _increment_metric("hits")
                return cached
        return query()
    except Exception:
        # django-redis can swallow connection errors, but retain this guard for
        # alternative cache backends and mocked outages.
        return query()


def get_ticket_statistics(user) -> dict:
    """Return ticket-list statistics visible to this authenticated user."""
    return _cache_aside(ticket_statistics_cache_key(user), lambda: _ticket_statistics_from_database(user))


def get_agent_statistics(user) -> dict:
    """Return assignment statistics for one agent, never another user."""
    if user.role != "agent":
        return {"total": 0, "by_status": _statuses(), "message_count": 0}
    return _cache_aside(agent_statistics_cache_key(user), lambda: _agent_statistics_from_database(user))
