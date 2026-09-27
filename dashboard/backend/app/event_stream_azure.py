"""Polls Cosmos DB for one run's live event transcript, in SSE lines --
Cosmos's Change Feed is the nearest thing to Firestore's on_snapshot()
push listener, but it needs a lease container plus a processor host to
consume, real overkill for one low-traffic admin dashboard. Polling every
~1.5s is far simpler and the latency difference (a couple seconds vs
near-instant push) doesn't matter for a human watching a live run's
transcript. See core/tools/run_status_azure.py's report_event() for what
writes the `events` container (partition key /run_id) in the first place.

Uses asyncio.to_thread for every Cosmos call -- the SDK is synchronous, and
this runs inside an async generator on FastAPI's own event loop, so a
blocking call here would stall every other request the whole time it's
waiting on the network."""

import asyncio
import json

from azure.cosmos import exceptions

from .storage_azure import CosmosStore, _DATABASE_NAME
from .event_stream import EventStreamer

_POLL_INTERVAL_SECONDS = 1.5


class CosmosEventStreamer(EventStreamer):
    async def stream_run(self, run_id: str):
        # One client/container set for the whole stream, not re-created
        # per poll -- CosmosStore's own _get_client() is only cheap to call
        # repeatedly because it's cached on the instance, so this keeps one
        # instance (and one authenticated client) alive for the connection.
        client = CosmosStore()._get_client()
        database = client.get_database_client(_DATABASE_NAME)
        events_container = database.get_container_client("events")
        runs_container = database.get_container_client("runs")

        last_sent: dict[str, dict] = {}
        while True:
            events = await asyncio.to_thread(
                lambda: list(
                    events_container.query_items(
                        query="SELECT * FROM c WHERE c.run_id = @run_id ORDER BY c.timestamp",
                        parameters=[{"name": "@run_id", "value": run_id}],
                        partition_key=run_id,
                    )
                )
            )
            for doc in events:
                if last_sent.get(doc["id"]) != doc:
                    last_sent[doc["id"]] = doc
                    yield f"data: {json.dumps(doc)}\n\n"

            def _read_run():
                try:
                    return runs_container.read_item(item=run_id, partition_key=run_id)
                except exceptions.CosmosResourceNotFoundError:
                    return None

            run_doc = await asyncio.to_thread(_read_run)
            status = (run_doc or {}).get("status")
            if status in ("succeeded", "failed"):
                yield f"event: done\ndata: {json.dumps({'status': status})}\n\n"
                return

            await asyncio.sleep(_POLL_INTERVAL_SECONDS)
