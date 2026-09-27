"""Bridges a Firestore real-time listener into SSE lines for one run's live
event transcript -- identical logic to the old event_stream.py, wrapped in
a class so it satisfies event_stream.EventStreamer alongside
event_stream_azure.CosmosEventStreamer. See core/tools/run_status_gcp.py's
report_event() for what writes runs/{run_id}/events/{event_id} in the
first place.

This is the one piece of the whole dashboard that crosses a thread
boundary: Firestore's Python client runs on_snapshot() callbacks on its own
background thread (a persistent gRPC stream it manages internally), never
on the asyncio event loop FastAPI itself runs on."""

import asyncio
import json

from .storage_gcp import FirestoreStore
from .event_stream import EventStreamer


class FirestoreEventStreamer(EventStreamer):
    async def stream_run(self, run_id: str):
        """Every existing event immediately (Firestore's on_snapshot()
        fires its first callback with the full current result set, so an
        already-finished run's whole history arrives at once), then any
        new ones live, then a final `done` line once the run reaches a
        terminal status -- at which point this closes the connection
        itself rather than holding it open forever."""
        loop = asyncio.get_event_loop()
        queue: asyncio.Queue = asyncio.Queue()

        client = FirestoreStore()._get_client()
        events_ref = (
            client.collection("runs").document(run_id).collection("events").order_by("timestamp")
        )
        run_ref = client.collection("runs").document(run_id)

        def on_events(collection_snapshot, changes, read_time):
            for change in changes:
                if change.type.name in ("ADDED", "MODIFIED"):
                    doc = change.document.to_dict() or {}
                    doc["id"] = change.document.id
                    # Firestore's callback runs on its OWN background thread,
                    # never the asyncio loop -- asyncio.Queue.put_nowait() is
                    # not thread-safe to call directly from there.
                    # call_soon_threadsafe is the required hand-off.
                    loop.call_soon_threadsafe(queue.put_nowait, ("event", doc))

        def on_run(doc_snapshots, changes, read_time):
            for snap in doc_snapshots:
                if not snap.exists:
                    continue
                status = (snap.to_dict() or {}).get("status")
                if status in ("succeeded", "failed"):
                    loop.call_soon_threadsafe(queue.put_nowait, ("done", status))

        events_watch = events_ref.on_snapshot(on_events)
        run_watch = run_ref.on_snapshot(on_run)
        try:
            while True:
                kind, payload = await queue.get()
                if kind == "event":
                    yield f"data: {json.dumps(payload)}\n\n"
                else:
                    yield f"event: done\ndata: {json.dumps({'status': payload})}\n\n"
                    return
        finally:
            # Runs even when the client disconnects early (Starlette closes
            # this generator via GeneratorExit/cancellation in that case) --
            # without this, an abandoned browser tab leaks a live Firestore
            # listener (a persistent gRPC stream) on the backend forever.
            events_watch.unsubscribe()
            run_watch.unsubscribe()
