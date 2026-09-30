"""Live-transcript SSE interface + provider registry -- same pattern as
storage.py/secrets.py/job_runner.py. Every existing call site keeps calling
stream_run(run_id) exactly as it did when this module contained the
Firestore-specific logic directly; that logic now lives in
event_stream_gcp.py, with event_stream_azure.py as the Cosmos DB
equivalent.

Each provider's stream_run() is an async generator yielding already
SSE-formatted lines ("data: ...\\n\\n" / "event: done\\ndata: ...\\n\\n")
-- the facade just forwards them, so the route calling this (runs.py's
GET .../events) never needs to know which provider is behind it."""

import os
from abc import ABC, abstractmethod
from typing import AsyncGenerator


class EventStreamer(ABC):
    @abstractmethod
    def stream_run(self, run_id: str) -> AsyncGenerator[str, None]: ...


def _gcp():
    from .event_stream_gcp import FirestoreEventStreamer

    return FirestoreEventStreamer


def _azure():
    from .event_stream_azure import CosmosEventStreamer

    return CosmosEventStreamer


def _local():
    from .event_stream_local import MongoEventStreamer

    return MongoEventStreamer


EVENT_STREAM_REGISTRY = {
    "gcp": _gcp,
    "azure": _azure,
    "local": _local,
}

_instance: EventStreamer | None = None


def get_event_streamer() -> EventStreamer:
    global _instance
    if _instance is None:
        provider = os.environ.get("CLOUD_PROVIDER", "gcp")
        if provider not in EVENT_STREAM_REGISTRY:
            raise ValueError(f"No EventStreamer registered for CLOUD_PROVIDER={provider!r}, expected one of {sorted(EVENT_STREAM_REGISTRY)}")
        _instance = EVENT_STREAM_REGISTRY[provider]()()
    return _instance


async def stream_run(run_id: str):
    async for line in get_event_streamer().stream_run(run_id):
        yield line
