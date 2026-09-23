import { useEffect, useState } from 'react'
import { useParams } from 'react-router-dom'
import { api } from '../api.js'
import EventTranscript from '../components/EventTranscript.jsx'
import StatusBadge from '../components/StatusBadge.jsx'
import { formatTimestamp, runDuration } from '../format.js'

const POLL_INTERVAL_MS = 5000
const ACTIVE_STATUSES = new Set(['queued', 'running'])

function RatingsList({ ratings }) {
  if (!ratings) return null
  const letters = { '1.0': 'A', '2.0': 'B', '3.0': 'C', '4.0': 'D', '5.0': 'E' }
  const labels = {
    sqale_rating: 'Maintainability',
    security_rating: 'Security',
    reliability_rating: 'Reliability',
  }
  return (
    <ul className="ratings-list">
      {Object.entries(ratings).map(([metric, grade]) => (
        <li key={metric}>
          {labels[metric] || metric}: <strong>{letters[grade] || grade}</strong>
        </li>
      ))}
    </ul>
  )
}

function FinalReport({ report }) {
  return (
    <div className="final-report">
      <h3>Result</h3>
      <ul className="report-fields">
        {report.files_completed && (
          <li>
            Files changed ({report.files_completed.length}):{' '}
            {report.files_completed.length > 0 ? report.files_completed.join(', ') : 'none'}
          </li>
        )}
        {report.issues_fixed && <li>Issues fixed: {report.issues_fixed.length}</li>}
        {report.files_flagged_for_manual_review && report.files_flagged_for_manual_review.length > 0 && (
          <li>
            Flagged for manual review:
            <ul>
              {report.files_flagged_for_manual_review.map((f, i) => (
                <li key={i}>
                  {f.file} — {f.reason}
                </li>
              ))}
            </ul>
          </li>
        )}
        {typeof report.coverage_before === 'number' && (
          <li>
            Coverage: {report.coverage_before.toFixed(1)}% →{' '}
            {report.coverage_after != null ? `${report.coverage_after.toFixed(1)}%` : 'unknown'}
          </li>
        )}
        {typeof report.density_before === 'number' && (
          <li>
            Duplicated-lines density: {report.density_before.toFixed(1)}% →{' '}
            {report.density_after != null ? `${report.density_after.toFixed(1)}%` : 'unknown'}
          </li>
        )}
        {report.final_ratings && (
          <li>
            Final ratings: <RatingsList ratings={report.final_ratings} />
          </li>
        )}
        {report.push_result && <li>Push: {report.push_result}</li>}
        {typeof report.duration_seconds === 'number' && (
          <li>Duration: {Math.round(report.duration_seconds)}s</li>
        )}
        {report.tokens_consumed && <li>Tokens consumed: {report.tokens_consumed.total_tokens}</li>}
      </ul>
    </div>
  )
}

export default function RunDetailPage() {
  const { runId } = useParams()
  const [run, setRun] = useState(null)
  const [error, setError] = useState(null)

  useEffect(() => {
    let cancelled = false
    let interval

    async function refresh() {
      try {
        const data = await api.getRun(runId)
        if (cancelled) return
        setRun(data)
        // Stop polling once the run reaches a terminal state -- no point
        // refetching a run that will never change again.
        if (!ACTIVE_STATUSES.has(data.status) && interval) {
          clearInterval(interval)
        }
      } catch (err) {
        if (!cancelled) setError(err.message)
      }
    }

    refresh()
    interval = setInterval(refresh, POLL_INTERVAL_MS)
    return () => {
      cancelled = true
      clearInterval(interval)
    }
  }, [runId])

  if (error) return <p className="error">Failed to load run: {error}</p>
  if (run === null) return <p>Loading…</p>

  return (
    <div className="run-detail-page">
      <h2>
        {run.agent_type} run — <StatusBadge status={run.status} />
      </h2>
      <dl className="run-meta">
        <dt>Source</dt>
        <dd>
          {run.source}
          {run.source_branch ? ` (based on ${run.source_branch})` : ''}
        </dd>
        <dt>Branch</dt>
        <dd>
          {run.branch_name ? (
            run.sonar_dashboard_url ? (
              <a href={run.sonar_dashboard_url} target="_blank" rel="noreferrer">
                {run.branch_name} ↗ view in Sonar
              </a>
            ) : (
              run.branch_name
            )
          ) : (
            'not created yet'
          )}
        </dd>
        <dt>Created</dt>
        <dd>{formatTimestamp(run.created_at)}</dd>
        <dt>Duration</dt>
        <dd>{runDuration(run)}</dd>
      </dl>

      {run.status === 'failed' && run.error && (
        <p className="error">
          <strong>Error:</strong> {run.error}
        </p>
      )}

      {run.final_report && <FinalReport report={run.final_report} />}

      {ACTIVE_STATUSES.has(run.status) && <p className="section-note">Refreshing every few seconds…</p>}

      <EventTranscript runId={run.id} />
    </div>
  )
}
