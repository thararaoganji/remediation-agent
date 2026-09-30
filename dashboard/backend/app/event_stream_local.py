"""Polls MongoDB for one run's live event transcript, in SSE lines --
local-dev equivalent of event_stream_azure.py's polling approach (Mongo
Change Streams would work too, but only against a replica set, which is
one more thing to stand up just for local dev; polling every ~1.5s is
plenty responsive for a human watching a live run and needs nothing beyond
a single plain `mongo` container). See core/tools/run_status_local.py's
report_event() for what writes the `events` collection in the first
place."""

import asyncio
import json
import os

from .storage_local import MongoStore, _strip_mongo_id
from .event_stream import EventStreamer

_POLL_INTERVAL_SECONDS = 1.5


class MongoEventStreamer(EventStreamer):
    async def stream_run(self, run_id: str):
        store = MongoStore()
        db_name = os.environ.get("MONGO_DB", "dashboard")
        events_collection = store._get_client()[db_name]["events"]
        runs_collection = store._get_client()[db_name]["runs"]

        last_sent: dict[str, dict] = {}
        while True:
            docs = await asyncio.to_thread(
                lambda: list(events_collection.find({"run_id": run_id}).sort("timestamp", 1))
            )
            for raw in docs:
                doc = _strip_mongo_id(raw)
                if last_sent.get(doc["id"]) != doc:
                    last_sent[doc["id"]] = doc
                    yield f"data: {json.dumps(doc)}\n\n"

            run_doc = await asyncio.to_thread(lambda: runs_collection.find_one({"_id": run_id}))
            status = (run_doc or {}).get("status")
            if status in ("succeeded", "failed"):
                yield f"event: done\ndata: {json.dumps({'status': status})}\n\n"
                return

            await asyncio.sleep(_POLL_INTERVAL_SECONDS)
