import { useEffect, useState } from 'react'

// Renders one Part the way adk web would -- a plain text bubble, a
// collapsed "thinking" block (Part.thought: true marks a reasoning chunk,
// not a separate part type -- see core/tools/run_status.py's docstring),
// or a tool call/response block. Mirrors exactly what
// event.model_dump(mode="json", exclude_none=True) actually puts on the
// wire (core/tools/run_status.py's report_event()) -- no fields invented
// here that aren't really there.
function PartRenderer({ part }) {
  if (part.thought) {
    return (
      <details className="event-part event-part-thought">
        <summary>🤔 Thinking</summary>
        <p>{part.text}</p>
      </details>
    )
  }
  if (part.function_call) {
    return (
      <div className="event-part event-part-call">
        <code>
          called <strong>{part.function_call.name}</strong>({JSON.stringify(part.function_call.args ?? {})})
        </code>
      </div>
    )
  }
  if (part.function_response) {
    return (
      <div className="event-part event-part-response">
        <code>
          <strong>{part.function_response.name}</strong> → {JSON.stringify(part.function_response.response ?? {})}
        </code>
      </div>
    )
  }
  if (part.text) {
    return <p className="event-part event-part-text">{part.text}</p>
  }
  return null
}

function StateDelta({ stateDelta }) {
  const entries = Object.entries(stateDelta || {})
  if (entries.length === 0) return null
  return (
    <details className="event-part event-part-state">
      <summary>
        State updated ({entries.length} key{entries.length > 1 ? 's' : ''})
      </summary>
      <ul>
        {entries.map(([key, value]) => (
          <li key={key}>
            <code>{key}</code>: {typeof value === 'object' ? JSON.stringify(value) : String(value)}
          </li>
        ))}
      </ul>
    </details>
  )
}

function EventCard({ event }) {
  const parts = event.content?.parts || []
  return (
    <li className="transcript-event">
      <div className="transcript-event-header">
        <strong>{event.author}</strong>
        {typeof event.timestamp === 'number' && (
          <span className="transcript-event-time">{new Date(event.timestamp * 1000).toLocaleTimeString()}</span>
        )}
      </div>
      {event.error_message && <p className="error">{event.error_message}</p>}
      {parts.map((part, i) => (
        <PartRenderer key={i} part={part} />
      ))}
      <StateDelta stateDelta={event.actions?.state_delta} />
    </li>
  )
}

// Downloads the exact events this transcript has received, unmodified,
// as one JSON file -- the same shape adk web's own trace export uses
// (a plain array of `event.model_dump()` dicts), so it round-trips with
// any tooling built against that format.
function downloadEventsAsJson(runId, events) {
  const payload = { run_id: runId, exported_at: new Date().toISOString(), events }
  const blob = new Blob([JSON.stringify(payload, null, 2)], { type: 'application/json' })
  const url = URL.createObjectURL(blob)
  const link = document.createElement('a')
  link.href = url
  link.download = `run-${runId}-events.json`
  document.body.appendChild(link)
  link.click()
  link.remove()
  URL.revokeObjectURL(url)
}

// Opens one SSE connection per mount (see dashboard/backend/app/event_stream.py
// for the Firestore-real-time-listener bridge on the other end). Works
// identically whether the run is still going (events arrive live, the
// connection closes itself once the backend sees a terminal status) or
// already finished (the full history arrives immediately, then it closes
// right away) -- no separate "live" vs "history" code path needed here.
export default function EventTranscript({ runId }) {
  // Kept in arrival (chronological) order -- newest-last -- since that's
  // the natural order for the JSON export and for reasoning about what
  // happened when. Only the rendered list below reverses it.
  const [events, setEvents] = useState([])
  const [connected, setConnected] = useState(false)

  useEffect(() => {
    setEvents([])
    const source = new EventSource(`/api/runs/${runId}/stream`)

    source.onopen = () => setConnected(true)

    source.onmessage = (e) => {
      try {
        const event = JSON.parse(e.data)
        setEvents((prev) => [...prev, event])
      } catch {
        // Ignore one malformed line rather than crash the whole transcript.
      }
    }

    source.addEventListener('done', () => {
      setConnected(false)
      source.close()
    })

    // EventSource auto-retries on a transient network error; if the run
    // had already finished server-side, the `done` listener above will
    // already have closed this cleanly before onerror ever has a chance
    // to fire from that.
    source.onerror = () => setConnected(false)

    return () => source.close()
  }, [runId])

  return (
    <div className="event-transcript">
      <h3>
        Live transcript
        {connected && <span className="transcript-live-dot" title="Live" />}
        <button
          type="button"
          className="transcript-export-btn"
          onClick={() => downloadEventsAsJson(runId, events)}
          disabled={events.length === 0}
        >
          Export JSON
        </button>
      </h3>
      {events.length === 0 ? (
        <p className="empty-note">Waiting for the run to produce events…</p>
      ) : (
        <ul className="transcript-list">
          {/* Newest first, so the latest activity is visible without scrolling. */}
          {[...events].reverse().map((event, i) => (
            <EventCard key={event.id || i} event={event} />
          ))}
        </ul>
      )}
    </div>
  )
}
