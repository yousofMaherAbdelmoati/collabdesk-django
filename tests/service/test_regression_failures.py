"""Real-service regressions kept separate from isolated unit tests.

These checks use the test database created by pytest-django.  The child
processes are pointed at that database explicitly; they never touch the
Compose application's ``collabdesk`` database.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import uuid
from pathlib import Path

import pytest
from asgiref.sync import async_to_sync
from celery import Celery
from channels.layers import channel_layers
from channels.testing import WebsocketCommunicator
from django.conf import settings
from django.contrib.auth import get_user_model
from django.contrib.sessions.models import Session
from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.test import Client, override_settings

from apps.desk import cache as desk_cache
from apps.desk.models import ConversationMessage, IdempotencyRecord, Ticket
from apps.reports.models import ReportJob
from apps.reports.tasks import run_report
from config.asgi import application


PROJECT_ROOT = Path(__file__).resolve().parents[2]
WORKER = r'''
import json
import os
import uuid

import django

django.setup()

from django.contrib.auth import get_user_model
from django.db import connections
from apps.desk.models import ConversationMessage
from apps.desk.services import claim_ticket, create_ticket_idempotently
from apps.reports.services import request_report

argument = json.loads(os.environ["DJ009_ARGUMENT"])
try:
    if argument["action"] == "ticket":
        user = get_user_model().objects.get(pk=argument["user_id"])
        body, status, replayed = create_ticket_idempotently(
            user=user, key=argument["key"], payload=argument["payload"]
        )
        result = {"id": body["id"], "status": status, "replayed": replayed}
    elif argument["action"] == "claim":
        agent = get_user_model().objects.get(pk=argument["agent_id"])
        try:
            ticket = claim_ticket(ticket_id=argument["ticket_id"], agent=agent)
        except Exception as exc:
            result = {"outcome": type(exc).__name__}
        else:
            result = {"outcome": "claimed", "agent_id": ticket.assigned_agent_id}
    elif argument["action"] == "message":
        message, created = ConversationMessage.objects.get_or_create(
            ticket_id=argument["ticket_id"],
            client_message_id=uuid.UUID(argument["client_message_id"]),
            defaults={"sender_id": argument["sender_id"], "body": argument["body"]},
        )
        result = {"id": message.id, "created": created}
    elif argument["action"] == "report":
        # Redis/Celery is intentionally not involved in this database race;
        # the live broker suite covers dispatch separately.  A wake-up is only
        # a best-effort hint and must not affect durable request deduplication.
        from unittest.mock import patch
        user = get_user_model().objects.get(pk=argument["user_id"])
        with patch("apps.reports.tasks.wake_outbox_publisher"):
            job, created = request_report(user=user, params=argument["params"])
        result = {"id": job.id, "created": created}
    else:
        raise ValueError("unknown DJ-009 action")
finally:
    connections.close_all()

print(json.dumps(result, sort_keys=True))
'''


def _run_processes(arguments: list[dict]) -> list[dict]:
    """Run each contention attempt in a fresh interpreter and DB connection."""
    environment = os.environ.copy()
    # pytest-django switches the parent connection to the explicitly named
    # test database. A fresh interpreter otherwise defaults to `collabdesk`.
    environment.update(
        {
            "DJANGO_ENV": "test",
            "POSTGRES_DB": connection.settings_dict["NAME"],
            "POSTGRES_TEST_DB": connection.settings_dict["NAME"],
        }
    )
    processes = []
    for argument in arguments:
        child_environment = environment | {"DJ009_ARGUMENT": json.dumps(argument)}
        processes.append(
            subprocess.Popen(
                [sys.executable, "-c", WORKER],
                cwd=PROJECT_ROOT,
                env=child_environment,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
        )

    results = []
    for process in processes:
        stdout, stderr = process.communicate(timeout=30)
        assert process.returncode == 0, stderr or stdout
        results.append(json.loads(stdout.strip().splitlines()[-1]))
    return results


def _session_socket(user, ticket_id: int) -> WebsocketCommunicator:
    client = Client()
    client.force_login(user)
    session_id = client.cookies[settings.SESSION_COOKIE_NAME].value
    return WebsocketCommunicator(
        application,
        f"/ws/tickets/{ticket_id}/",
        headers=[
            (b"origin", b"http://localhost:3000"),
            (b"cookie", f"{settings.SESSION_COOKIE_NAME}={session_id}".encode()),
        ],
    )


def _test_broker_url() -> str:
    base, _separator, _database = settings.REDIS_URL.rpartition("/")
    return f"{base}/15"


def _start_test_worker(*, queue: str, broker_url: str) -> subprocess.Popen:
    environment = os.environ.copy()
    environment.update(
        {
            "DJANGO_ENV": "test",
            "POSTGRES_DB": connection.settings_dict["NAME"],
            "POSTGRES_TEST_DB": connection.settings_dict["NAME"],
            # A dedicated local Redis DB prevents these test messages from
            # reaching the long-running Compose demonstration worker.
            "REDIS_URL": broker_url,
        }
    )
    return subprocess.Popen(
        [
            sys.executable, "-m", "celery", "-A", "config", "worker",
            "--pool=solo", "--concurrency=1", "--without-gossip",
            "--without-mingle", "--without-heartbeat", "--loglevel=WARNING",
            "--queues", queue, "--hostname", f"dj009-{uuid.uuid4().hex}@%h",
        ],
        cwd=PROJECT_ROOT,
        env=environment,
        text=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
    )


def _wait_for_completion(job: ReportJob, worker: subprocess.Popen) -> None:
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        job.refresh_from_db()
        if job.status == ReportJob.Status.COMPLETED:
            return
        if worker.poll() is not None:
            _stdout, stderr = worker.communicate()
            pytest.fail(f"Celery worker exited before report completion: {stderr}")
        time.sleep(0.1)
    pytest.fail("Timed out waiting for local Celery worker to complete report")


def _stop_worker(worker: subprocess.Popen) -> None:
    worker.terminate()
    try:
        worker.communicate(timeout=10)
    except subprocess.TimeoutExpired:
        worker.kill()
        worker.communicate(timeout=10)


@pytest.mark.service
@pytest.mark.postgres
@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize("round_number", range(2), ids=lambda number: f"round-{number + 1}")
def test_twenty_process_ticket_requests_are_exactly_once(round_number):
    user = get_user_model().objects.create_user(
        username=f"dj009-ticket-{round_number}", password="x", role="customer"
    )
    key = f"dj009-ticket-{round_number}-{uuid.uuid4()}"
    results = _run_processes(
        [{"action": "ticket", "user_id": user.id, "key": key, "payload": {"title": "fictional", "description": "process race"}}]
        * 20
    )

    assert len({result["id"] for result in results}) == 1
    assert sum(not result["replayed"] for result in results) == 1
    assert Ticket.objects.filter(customer=user).count() == 1
    assert IdempotencyRecord.objects.filter(user=user, key=key).count() == 1


@pytest.mark.service
@pytest.mark.postgres
@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize("round_number", range(2), ids=lambda number: f"round-{number + 1}")
def test_twenty_process_claims_have_one_winner(round_number):
    User = get_user_model()
    customer = User.objects.create_user(username=f"dj009-claim-customer-{round_number}", password="x", role="customer")
    ticket = Ticket.objects.create(customer=customer, title="fictional", description="process claim race")
    agents = [
        User.objects.create_user(username=f"dj009-claim-{round_number}-{index}", password="x", role="agent")
        for index in range(20)
    ]
    results = _run_processes(
        [{"action": "claim", "ticket_id": ticket.id, "agent_id": agent.id} for agent in agents]
    )

    assert [result["outcome"] for result in results].count("claimed") == 1
    assert [result["outcome"] for result in results].count("TicketConflict") == 19
    ticket.refresh_from_db()
    assert ticket.assigned_agent_id in {agent.id for agent in agents}


@pytest.mark.service
@pytest.mark.postgres
@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize("round_number", range(2), ids=lambda number: f"round-{number + 1}")
def test_twenty_process_message_retries_persist_one_message(round_number):
    User = get_user_model()
    customer = User.objects.create_user(username=f"dj009-message-{round_number}", password="x", role="customer")
    ticket = Ticket.objects.create(customer=customer, title="fictional", description="message process race")
    client_message_id = str(uuid.uuid4())
    results = _run_processes(
        [
            {
                "action": "message",
                "ticket_id": ticket.id,
                "sender_id": customer.id,
                "client_message_id": client_message_id,
                "body": "fictional retry",
            }
        ]
        * 20
    )

    assert len({result["id"] for result in results}) == 1
    assert sum(result["created"] for result in results) == 1
    assert ConversationMessage.objects.filter(ticket=ticket).count() == 1


@pytest.mark.service
@pytest.mark.postgres
@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize("round_number", range(2), ids=lambda number: f"round-{number + 1}")
def test_twenty_process_report_requests_create_one_active_job(round_number):
    admin = get_user_model().objects.create_user(
        username=f"dj009-report-{round_number}", password="x", role="admin"
    )
    params = {}
    results = _run_processes(
        [{"action": "report", "user_id": admin.id, "params": params}] * 20
    )

    assert sum(result["created"] for result in results) == 1
    assert len({result["id"] for result in results}) == 1
    assert ReportJob.objects.filter(parameters=params, status=ReportJob.Status.QUEUED).count() == 1


@pytest.mark.service
@pytest.mark.redis
@pytest.mark.django_db(transaction=True)
def test_redis_channel_layer_broadcast_survives_reconnect_with_history():
    """This intentionally uses channels_redis, unlike isolated channel tests."""
    channel_layers.backends.clear()
    User = get_user_model()
    customer = User.objects.create_user(username="dj009-ws-customer", password="x", role="customer")
    agent = User.objects.create_user(username="dj009-ws-agent", password="x", role="agent")
    ticket = Ticket.objects.create(
        customer=customer, assigned_agent=agent, status=Ticket.Status.CLAIMED,
        title="fictional", description="Redis broadcast",
    )
    customer_socket = _session_socket(customer, ticket.id)
    agent_socket = _session_socket(agent, ticket.id)
    reconnect_socket = _session_socket(agent, ticket.id)

    async def scenario():
        assert (await customer_socket.connect())[0]
        assert (await agent_socket.connect())[0]
        try:
            await customer_socket.send_json_to(
                {"type": "message", "client_message_id": str(uuid.uuid4()), "body": "redis fan-out"}
            )
            customer_event = await customer_socket.receive_json_from(timeout=3)
            agent_event = await agent_socket.receive_json_from(timeout=3)
            assert customer_event == agent_event
            await agent_socket.disconnect()
            await reconnect_socket.connect()
            await reconnect_socket.send_json_to({"type": "history", "after": 0, "limit": 10})
            history = await reconnect_socket.receive_json_from(timeout=3)
            assert [row["body"] for row in history["results"]] == ["redis fan-out"]
        finally:
            await customer_socket.disconnect()
            await reconnect_socket.disconnect()

    async_to_sync(scenario)()
    channel_layers.backends.clear()


@pytest.mark.service
@pytest.mark.postgres
@pytest.mark.django_db(transaction=True)
def test_redis_connection_refusal_falls_back_to_postgresql():
    """Use a deliberately unused local port, without interrupting Compose Redis."""
    customer = get_user_model().objects.create_user(username="dj009-redis-outage", password="x", role="customer")
    Ticket.objects.create(customer=customer, title="durable", description="database remains authoritative")
    unavailable_cache = {
        "default": {
            "BACKEND": "django_redis.cache.RedisCache",
            "LOCATION": "redis://127.0.0.1:1/15",
            "KEY_PREFIX": "dj009-outage",
            "VERSION": 1,
            "OPTIONS": {
                "CLIENT_CLASS": "django_redis.client.DefaultClient",
                "SOCKET_CONNECT_TIMEOUT": 0.05,
                "SOCKET_TIMEOUT": 0.05,
                "IGNORE_EXCEPTIONS": True,
            },
        }
    }
    with override_settings(CACHES=unavailable_cache):
        result = desk_cache.get_ticket_statistics(customer)

    assert result["total"] == 1
    assert result["by_status"]["open"] == 1


@pytest.mark.service
@pytest.mark.postgres
@pytest.mark.redis
@pytest.mark.django_db(transaction=True)
def test_real_celery_worker_handles_duplicate_delivery_across_restart():
    """Use an isolated Redis queue and two short-lived local Celery workers."""
    admin = get_user_model().objects.create_user(username="dj009-celery-admin", password="x", role="admin")
    broker_url = _test_broker_url()
    queue = f"dj009-{uuid.uuid4().hex}"
    producer = Celery("dj009-test-producer", broker=broker_url)
    first_worker = second_worker = None
    try:
        first = ReportJob.objects.create(requested_by=admin, fingerprint="a" * 64, parameters={})
        # An at-least-once broker may hand both messages to the same worker;
        # the lease/token state machine must persist only one completed run.
        producer.send_task(run_report.name, args=[first.id], queue=queue)
        producer.send_task(run_report.name, args=[first.id], queue=queue)
        first_worker = _start_test_worker(queue=queue, broker_url=broker_url)
        _wait_for_completion(first, first_worker)
        time.sleep(0.3)  # allow the queued duplicate to be consumed harmlessly
        first.refresh_from_db()
        assert first.attempts == 1
        _stop_worker(first_worker)
        first_worker = None

        # Restarting a worker must continue consuming durable work from the
        # same PostgreSQL test database and isolated Redis queue.
        second = ReportJob.objects.create(requested_by=admin, fingerprint="b" * 64, parameters={})
        second_worker = _start_test_worker(queue=queue, broker_url=broker_url)
        producer.send_task(run_report.name, args=[second.id], queue=queue)
        _wait_for_completion(second, second_worker)
        second.refresh_from_db()
        assert second.attempts == 1
    finally:
        if first_worker is not None:
            _stop_worker(first_worker)
        if second_worker is not None:
            _stop_worker(second_worker)
        producer.close()


@pytest.mark.service
@pytest.mark.postgres
@pytest.mark.django_db(transaction=True)
def test_test_database_has_no_unapplied_migrations():
    """pytest-django applied the real migration graph; require it to be clean."""
    executor = MigrationExecutor(connection)
    assert executor.migration_plan(executor.loader.graph.leaf_nodes()) == []
