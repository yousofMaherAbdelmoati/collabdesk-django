import asyncio
import json
import time
from collections.abc import AsyncIterator
from unittest.mock import patch

import pytest
from asgiref.testing import ApplicationCommunicator
from django.contrib.auth import get_user_model
from django.test import override_settings
from rest_framework.test import APIClient

from config.asgi import application
from apps.desk.models import Ticket
from apps.desk.streaming import (
    AssistantDemoRequest,
    DeterministicLocalProvider,
    assistant_sse_events,
    stream_metrics,
)


@pytest.fixture
def customer(db):
    return get_user_model().objects.create_user(username="sse-customer", password="x", role="customer")


def parse_frame(frame: bytes) -> tuple[str, dict]:
    """Assert the stream uses one correctly terminated SSE data event."""
    decoded = frame.decode("utf-8")
    assert decoded.endswith("\n\n")
    event, data = decoded[:-2].split("\n")
    assert event.startswith("event: ")
    assert data.startswith("data: ")
    return event.removeprefix("event: "), json.loads(data.removeprefix("data: "))


async def collect(stream: AsyncIterator[bytes]) -> list[tuple[str, dict]]:
    return [parse_frame(frame) async for frame in stream]


def test_local_provider_orders_tokens_and_completes():
    events = asyncio.run(
        collect(
            assistant_sse_events(
                DeterministicLocalProvider(delay_seconds=0),
                AssistantDemoRequest(ticket_id=7),
                timeout_seconds=1,
                max_chunks=32,
                max_output_chars=4096,
            )
        )
    )

    token_events = [payload for event, payload in events if event == "token"]
    assert [payload["sequence"] for payload in token_events] == list(range(len(token_events)))
    assert "".join(payload["text"] for payload in token_events).startswith("Demo stream only. Ticket 7 is selected.")
    assert events[-1] == ("complete", {"chunks": len(token_events), "truncated": False})


def test_stream_returns_error_events_for_provider_failure_and_timeout():
    class FailingProvider:
        closed = False

        async def aclose(self):
            self.closed = True

        async def stream(self, _prompt):
            raise RuntimeError("do-not-log-provider-detail")
            yield "unreachable"

    class SlowProvider(FailingProvider):
        async def stream(self, _prompt):
            await asyncio.Event().wait()
            yield "unreachable"

    failing = FailingProvider()
    with patch("apps.desk.streaming.logger.error") as error:
        failed_events = asyncio.run(
            collect(assistant_sse_events(failing, AssistantDemoRequest(1), timeout_seconds=1, max_chunks=1, max_output_chars=10))
        )
    slow = SlowProvider()
    timeout_events = asyncio.run(
        collect(assistant_sse_events(slow, AssistantDemoRequest(1), timeout_seconds=0.01, max_chunks=1, max_output_chars=10))
    )

    assert failed_events == [("error", {"code": "stream_failed"})]
    assert timeout_events == [("error", {"code": "stream_timeout"})]
    assert failing.closed and slow.closed
    assert "do-not-log-provider-detail" not in str(error.call_args_list)


def test_stream_enforces_chunk_and_character_limits():
    events = asyncio.run(
        collect(
            assistant_sse_events(
                DeterministicLocalProvider(delay_seconds=0),
                AssistantDemoRequest(1),
                timeout_seconds=1,
                max_chunks=2,
                max_output_chars=7,
            )
        )
    )
    tokens = [payload for event, payload in events if event == "token"]
    assert len(tokens) <= 2
    assert sum(len(payload["text"]) for payload in tokens) <= 7
    assert events[-1][0] == "complete"
    assert events[-1][1]["truncated"] is True


def test_disconnect_cancels_pending_provider_and_closes_within_bound():
    class BlockingProvider:
        def __init__(self):
            self.waiting = asyncio.Event()
            self.closed = False

        async def aclose(self):
            self.closed = True

        async def stream(self, _prompt):
            yield "first "
            self.waiting.set()
            await asyncio.Event().wait()
            yield "unreachable"

    async def disconnect() -> None:
        provider = BlockingProvider()
        stream = assistant_sse_events(provider, AssistantDemoRequest(1), timeout_seconds=10, max_chunks=32, max_output_chars=100)
        assert parse_frame(await anext(stream))[0] == "token"
        pending = asyncio.create_task(anext(stream))
        await provider.waiting.wait()
        pending.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(pending, timeout=0.2)
        assert provider.closed

    asyncio.run(disconnect())


def test_twelve_concurrent_async_streams_do_not_serialize_and_do_not_log_request_content():
    async def run_concurrently() -> tuple[float, list[list[tuple[str, dict]]]]:
        stream_metrics.reset()
        started = time.monotonic()
        streams = [
            collect(
                assistant_sse_events(
                    DeterministicLocalProvider(delay_seconds=0.02),
                    AssistantDemoRequest(index),
                    timeout_seconds=2,
                    max_chunks=4,
                    max_output_chars=4096,
                )
            )
            for index in range(12)
        ]
        results = await asyncio.wait_for(asyncio.gather(*streams), timeout=0.6)
        return time.monotonic() - started, results

    with patch("apps.desk.streaming.logger.info") as info:
        elapsed, results = asyncio.run(run_concurrently())

    # Each stream needs about 0.10s (four emitted chunks plus the limit check).
    # A blocking sleep would serialize twelve streams to about 1.2s.
    assert elapsed < 0.6
    assert all(events[-1][0] == "complete" for events in results)
    assert stream_metrics.snapshot()["max_active"] >= 12
    assert "ticket_id" not in str(info.call_args_list)


@pytest.mark.django_db(transaction=True)
@override_settings(ASSISTANT_STREAM_TOKEN_DELAY_SECONDS=0)
def test_twelve_concurrent_sse_requests_stream_through_asgi(customer):
    """Exercise Django's ASGI response iterator, not merely the provider."""
    ticket = Ticket.objects.create(customer=customer, title="t", description="d")
    browser = APIClient()
    browser.force_login(customer)
    browser.get("/api/auth/csrf/")
    cookie_header = "; ".join(f"{name}={morsel.value}" for name, morsel in browser.cookies.items()).encode()
    csrf_token = browser.cookies["csrftoken"].value.encode()
    body = json.dumps({"ticket_id": ticket.id}).encode()

    def communicator() -> ApplicationCommunicator:
        return ApplicationCommunicator(
            application,
            {
                "type": "http",
                "http_version": "1.1",
                "method": "POST",
                "scheme": "http",
                "path": "/api/assistant/stream",
                "raw_path": b"/api/assistant/stream",
                "query_string": b"",
                "headers": [
                    (b"host", b"testserver"),
                    (b"content-type", b"application/json"),
                    (b"content-length", str(len(body)).encode()),
                    (b"cookie", cookie_header),
                    (b"x-csrftoken", csrf_token),
                ],
                "client": ("127.0.0.1", 12345),
                "server": ("testserver", 80),
            },
        )

    async def read_response(stream: ApplicationCommunicator) -> list[bytes]:
        start = await stream.receive_output(timeout=1)
        assert start["type"] == "http.response.start"
        assert start["status"] == 200
        frames = []
        while True:
            message = await stream.receive_output(timeout=1)
            if message["type"] != "http.response.body":
                continue
            if message.get("body"):
                frames.append(message["body"])
            if not message.get("more_body"):
                await stream.wait(timeout=1)
                return frames

    async def run_requests():
        streams = [communicator() for _ in range(12)]
        for stream in streams:
            await stream.send_input({"type": "http.request", "body": body})
        started = time.monotonic()
        responses = await asyncio.wait_for(asyncio.gather(*(read_response(stream) for stream in streams)), timeout=0.8)
        return time.monotonic() - started, responses

    elapsed, responses = asyncio.run(run_requests())
    assert elapsed < 0.8
    assert all(parse_frame(response[-1])[0] == "complete" for response in responses)
