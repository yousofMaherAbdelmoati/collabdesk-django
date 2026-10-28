"""Async, bounded SSE support for the local assistant demonstration.

The provider contract deliberately contains no HTTP or model concerns.  A real
provider can implement ``stream()`` and ``aclose()`` later without changing the
SSE lifecycle or its safety limits.
"""

from __future__ import annotations

import asyncio
import json
import logging
import threading
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Protocol


logger = logging.getLogger("collabdesk.ticket_api")


@dataclass(frozen=True)
class AssistantDemoRequest:
    ticket_id: int


class AsyncResponseProvider(Protocol):
    """A response source which cooperatively releases resources on close."""

    def stream(self, request: AssistantDemoRequest) -> AsyncIterator[str]: ...

    async def aclose(self) -> None: ...


class DeterministicLocalProvider:
    """Local-only provider used for demos and tests; it never calls an AI API."""

    def __init__(self, *, delay_seconds: float = 0.005):
        self.delay_seconds = delay_seconds
        self.closed = False

    async def aclose(self) -> None:
        self.closed = True

    async def stream(self, request: AssistantDemoRequest) -> AsyncIterator[str]:
        response = (
            f"Demo stream only. Ticket {request.ticket_id} is selected. "
            "This fixed local response exists solely to demonstrate SSE delivery and cancellation. "
            "A support agent will provide next steps."
        )
        for word in response.split():
            # asyncio.sleep yields control to the ASGI event loop.  This is
            # intentionally unlike time.sleep(), which would serialize streams.
            if self.delay_seconds:
                await asyncio.sleep(self.delay_seconds)
            yield f"{word} "


class StreamMetrics:
    """Small process-local counters safe to expose to an observability adapter.

    Stream payloads, ticket ids, and user ids are deliberately not
    retained.  The counters diagnose capacity and failure mode without logging
    message content.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.reset()

    def reset(self) -> None:
        with self._lock:
            self.started = 0
            self.completed = 0
            self.failed = 0
            self.timed_out = 0
            self.disconnected = 0
            self.limited = 0
            self.active = 0
            self.max_active = 0

    def begin(self) -> None:
        with self._lock:
            self.started += 1
            self.active += 1
            self.max_active = max(self.max_active, self.active)

    def finish(self, outcome: str) -> None:
        with self._lock:
            self.active -= 1
            if outcome == "complete":
                self.completed += 1
            elif outcome == "error":
                self.failed += 1
            elif outcome == "timeout":
                self.timed_out += 1
            elif outcome == "disconnect":
                self.disconnected += 1
            elif outcome == "limited":
                self.limited += 1

    def snapshot(self) -> dict[str, int]:
        with self._lock:
            return {
                "started": self.started,
                "completed": self.completed,
                "failed": self.failed,
                "timed_out": self.timed_out,
                "disconnected": self.disconnected,
                "limited": self.limited,
                "active": self.active,
                "max_active": self.max_active,
            }


stream_metrics = StreamMetrics()


def sse_frame(event: str, payload: dict[str, object]) -> bytes:
    """Return one RFC-compatible SSE event with a JSON data field."""
    return f"event: {event}\ndata: {json.dumps(payload, ensure_ascii=False, separators=(',', ':'))}\n\n".encode("utf-8")


async def assistant_sse_events(
    provider: AsyncResponseProvider,
    request: AssistantDemoRequest,
    *,
    timeout_seconds: float,
    max_chunks: int,
    max_output_chars: int,
) -> AsyncIterator[bytes]:
    """Translate provider output into bounded SSE events and clean up reliably."""
    if timeout_seconds <= 0 or max_chunks <= 0 or max_output_chars <= 0:
        raise ValueError("SSE timeout and output limits must be positive")

    chunks = 0
    output_chars = 0
    outcome = "error"
    stream_metrics.begin()
    try:
        try:
            async with asyncio.timeout(timeout_seconds):
                async for text in provider.stream(request):
                    if chunks >= max_chunks or output_chars >= max_output_chars:
                        outcome = "limited"
                        break
                    remaining = max_output_chars - output_chars
                    bounded_text = text[:remaining]
                    if not bounded_text:
                        outcome = "limited"
                        break
                    yield sse_frame("token", {"sequence": chunks, "text": bounded_text})
                    chunks += 1
                    output_chars += len(bounded_text)
                    if len(bounded_text) != len(text):
                        outcome = "limited"
                        break
                else:
                    outcome = "complete"

            # A normal maximum is an expected terminal state, not an internal
            # error. Clients can render partial output and know why it stopped.
            limited = outcome == "limited"
            yield sse_frame("complete", {"chunks": chunks, "truncated": limited})
            logger.info(
                "assistant_stream",
                extra={"event": "assistant_stream", "outcome": outcome, "chunks": chunks},
            )
        except TimeoutError:
            outcome = "timeout"
            logger.warning("assistant_stream", extra={"event": "assistant_stream", "outcome": outcome})
            yield sse_frame("error", {"code": "stream_timeout"})
        except asyncio.CancelledError:
            outcome = "disconnect"
            logger.info("assistant_stream", extra={"event": "assistant_stream", "outcome": outcome})
            raise
        except GeneratorExit:
            outcome = "disconnect"
            logger.info("assistant_stream", extra={"event": "assistant_stream", "outcome": outcome})
            raise
        except Exception:
            outcome = "error"
            # Do not attach provider exception text: a provider may include
            # response content in its exception message.
            logger.error("assistant_stream", extra={"event": "assistant_stream", "outcome": outcome})
            yield sse_frame("error", {"code": "stream_failed"})
    finally:
        try:
            await provider.aclose()
        except Exception:
            # Closing is best effort. Do not replace a disconnect/cancellation
            # with a provider implementation detail.
            logger.warning("assistant_stream_close", extra={"event": "assistant_stream_close", "outcome": "error"})
        stream_metrics.finish(outcome)
