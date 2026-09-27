"""Tests for event_stream.stream_run() -- the Firestore real-time listener
bridge behind the dashboard's live per-run event transcript.

Drives the async generator directly with asyncio.run() (matching this
repo's established pattern for testing async code without pytest-asyncio,
e.g. sonar/setup.py's tests), rather than only through a real HTTP client.
tests/conftest.py's fake Firestore client invokes on_snapshot callbacks
synchronously in the calling thread -- exactly like the generator's own
call_soon_threadsafe hand-off expects, just without a real background
thread actually involved."""

import asyncio
import json

from app import event_stream


def _drain(run_id):
    async def _run():
        lines = []
        async for line in event_stream.stream_run(run_id):
            lines.append(line)
        return lines
    return asyncio.run(_run())


def _event_doc(text):
    return {"author": "fix_llm_agent", "content": {"role": "model", "parts": [{"text": text}]}}


def test_stream_run_yields_existing_events_then_done_for_finished_run(fake_firestore):
    # Simulates opening a run's detail page AFTER it already finished --
    # everything should arrive as one immediate backfill, no waiting.
    fake_firestore.collection("runs").document("run-1").set({"status": "succeeded"})
    fake_firestore.collection("runs").document("run-1").collection("events").document("evt-1").set(_event_doc("first"))
    fake_firestore.collection("runs").document("run-1").collection("events").document("evt-2").set(_event_doc("second"))

    lines = _drain("run-1")

    assert len(lines) == 3
    assert lines[0].startswith("data: ")
    assert json.loads(lines[0][len("data: "):])["id"] == "evt-1"
    assert json.loads(lines[1][len("data: "):])["id"] == "evt-2"
    assert lines[2] == 'event: done\ndata: {"status": "succeeded"}\n\n'


def test_stream_run_no_events_still_closes_on_terminal_status(fake_firestore):
    fake_firestore.collection("runs").document("run-1").set({"status": "failed"})
    lines = _drain("run-1")
    assert lines == ['event: done\ndata: {"status": "failed"}\n\n']


def test_stream_run_delivers_new_events_live_then_closes_on_finish(fake_firestore):
    # A run that's still going: nothing queued at first (status isn't
    # terminal, no events exist yet). A new event arrives mid-stream, then
    # the run finishes -- both must be delivered, in order, before closing.
    fake_firestore.collection("runs").document("run-1").set({"status": "running"})

    async def _run():
        results = []

        async def consume():
            async for line in event_stream.stream_run("run-1"):
                results.append(line)

        task = asyncio.ensure_future(consume())
        await asyncio.sleep(0)  # let the generator subscribe both watches and start waiting

        fake_firestore.collection("runs").document("run-1").collection("events").document("evt-1").set(
            _event_doc("live event")
        )
        await asyncio.sleep(0)  # let the queued item get processed and yielded

        fake_firestore.collection("runs").document("run-1").set({"status": "succeeded"})
        await task
        return results

    lines = asyncio.run(_run())

    assert len(lines) == 2
    assert json.loads(lines[0][len("data: "):])["content"]["parts"][0]["text"] == "live event"
    assert lines[1] == 'event: done\ndata: {"status": "succeeded"}\n\n'


def test_stream_run_unsubscribes_both_watches_when_run_finishes(fake_firestore):
    fake_firestore.collection("runs").document("run-1").set({"status": "succeeded"})
    _drain("run-1")

    # Both watches' callbacks are removed from the registry once
    # stream_run's finally block runs -- confirms the cleanup actually
    # happens, not just that the generator returns.
    assert fake_firestore._watches.get("runs/run-1/events", []) == []
    assert fake_firestore._watches.get("runs/run-1", []) == []


def test_stream_run_unsubscribes_both_watches_on_early_close(fake_firestore):
    # Simulates the browser closing the EventSource connection before the
    # run finishes -- Starlette closes the generator in that case
    # (GeneratorExit), and the finally block must still run, or an
    # abandoned browser tab leaks a live Firestore listener forever.
    fake_firestore.collection("runs").document("run-1").set({"status": "running"})

    async def _run():
        gen = event_stream.stream_run("run-1")
        task = asyncio.ensure_future(gen.__anext__())
        await asyncio.sleep(0)  # let it subscribe both watches and start waiting on the empty queue
        task.cancel()
        try:
            await task  # let the cancellation fully settle before touching gen again
        except asyncio.CancelledError:
            pass
        await gen.aclose()

    asyncio.run(_run())

    assert fake_firestore._watches.get("runs/run-1/events", []) == []
    assert fake_firestore._watches.get("runs/run-1", []) == []
