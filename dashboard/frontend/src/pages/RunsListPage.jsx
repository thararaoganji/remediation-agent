import { useEffect, useState } from 'react'
import { Link } from 'react-router-dom'
import { api } from '../api.js'
import { useAuth } from '../AuthContext.jsx'
import StatusBadge from '../components/StatusBadge.jsx'
import { formatTimestamp, runDuration } from '../format.js'

const POLL_INTERVAL_MS = 5000

export default function RunsListPage() {
  const { user } = useAuth()
  const isAdmin = user?.role === 'admin'
  const [runs, setRuns] = useState(null)
  const [error, setError] = useState(null)

  useEffect(() => {
    let cancelled = false

    async function refresh() {
      try {
        const data = await api.listRuns()
        if (!cancelled) setRuns(data)
      } catch (err) {
        if (!cancelled) setError(err.message)
      }
    }

    refresh()
    // Simple polling, not WebSockets/SSE -- runs last minutes, not
    // milliseconds, so a 5s refresh is plenty responsive without new
    // infrastructure (see the dashboard plan's Phase C notes).
    const interval = setInterval(refresh, POLL_INTERVAL_MS)
    return () => {
      cancelled = true
      clearInterval(interval)
    }
  }, [])

  if (error) return <p className="error">Failed to load runs: {error}</p>
  if (runs === null) return <p>Loading…</p>

  return (
    <div className="runs-list-page">
      <h2>Runs</h2>
      {runs.length === 0 ? (
        <p className="empty-note">
          No runs yet — <Link to="/runs/new">start one</Link>.
        </p>
      ) : (
        <table className="runs-table">
          <thead>
            <tr>
              <th>Agent</th>
              {isAdmin && <th>Owner</th>}
              <th>Source</th>
              <th>Branch</th>
              <th>Status</th>
              <th>Created</th>
              <th>Duration</th>
              <th></th>
            </tr>
          </thead>
          <tbody>
            {runs.map((run) => (
              <tr key={run.id}>
                <td>{run.agent_type}</td>
                {isAdmin && <td>{run.owner_email || '—'}</td>}
                <td>{run.source}</td>
                <td>
                  {run.sonar_dashboard_url ? (
                    <a href={run.sonar_dashboard_url} target="_blank" rel="noreferrer">
                      {run.branch_name}
                    </a>
                  ) : (
                    run.branch_name || '—'
                  )}
                </td>
                <td>
                  <StatusBadge status={run.status} />
                </td>
                <td>{formatTimestamp(run.created_at)}</td>
                <td>{runDuration(run)}</td>
                <td>
                  <Link to={`/runs/${run.id}`}>Details</Link>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </div>
  )
}
