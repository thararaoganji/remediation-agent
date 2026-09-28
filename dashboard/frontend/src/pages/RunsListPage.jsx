import { useEffect, useState } from 'react'
import { Link } from 'react-router-dom'
import { api } from '../api.js'
import { useAuth } from '../AuthContext.jsx'
import ConfirmDialog from '../components/ConfirmDialog.jsx'
import { PlayIcon, TrashIcon } from '../components/icons.jsx'
import Pagination from '../components/Pagination.jsx'
import StatusBadge from '../components/StatusBadge.jsx'
import { formatTimestamp, pushFailed, runDuration } from '../format.js'
import { usePagination } from '../usePagination.js'
import { usePageTitle } from '../usePageTitle.js'
import { VendorBadge } from '../vendors.jsx'

const POLL_INTERVAL_MS = 5000
const PAGE_SIZE = 10

function DeleteRunButton({ run, onDeleted }) {
  const [confirming, setConfirming] = useState(false)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState(null)

  async function handleDelete() {
    setBusy(true)
    setError(null)
    try {
      await api.deleteRun(run.id)
      onDeleted(run.id)
    } catch (err) {
      setError(err.message)
      setBusy(false)
      setConfirming(false)
    }
  }

  return (
    <>
      <button onClick={() => setConfirming(true)} className="icon-action danger" aria-label="Delete run" title="Delete run">
        <TrashIcon />
      </button>
      {error && <p className="error">{error}</p>}
      <ConfirmDialog
        open={confirming}
        title="Delete run"
        message={`Delete this ${run.agent_type} run against "${run.source}"? This only removes it from the dashboard -- it doesn't affect anything already pushed.`}
        busy={busy}
        onConfirm={handleDelete}
        onCancel={() => setConfirming(false)}
      />
    </>
  )
}

export default function RunsListPage() {
  usePageTitle('Runs')
  const { user } = useAuth()
  const isAdmin = user?.role === 'admin'
  const [runs, setRuns] = useState(null)
  const [error, setError] = useState(null)
  const { page, setPage, pageCount, pageItems } = usePagination(runs || [], PAGE_SIZE)

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

  function handleRunDeleted(runId) {
    setRuns((current) => (current || []).filter((r) => r.id !== runId))
  }

  if (error) return <p className="error">Failed to load runs: {error}</p>
  if (runs === null) return <p>Loading…</p>

  return (
    <div className="runs-list-page">
      <div className="page-head-row">
        <h2>Runs</h2>
        <Link to="/runs/new" className="button-link icon-button-link" aria-label="New Run" title="New Run">
          <PlayIcon size={18} />
        </Link>
      </div>
      <div className="table-scroll">
        <table className="data-table">
          <thead>
            <tr>
              <th>Agent</th>
              {isAdmin && <th>Owner</th>}
              <th>Source</th>
              <th>Branch</th>
              <th>LLM</th>
              <th>Status</th>
              <th>Created</th>
              <th>Duration</th>
              <th></th>
              <th></th>
            </tr>
          </thead>
          <tbody>
            {pageItems.map((run) => (
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
                  <VendorBadge vendor={run.llm_vendor} model={run.llm_model} />
                </td>
                <td>
                  <StatusBadge status={run.status} />
                  {pushFailed(run.final_report) && (
                    <span className="warning" title={run.final_report.push_result}>
                      {' '}
                      ⚠ push failed
                    </span>
                  )}
                </td>
                <td>{formatTimestamp(run.created_at)}</td>
                <td>{runDuration(run)}</td>
                <td>
                  <Link to={`/runs/${run.id}`}>Details</Link>
                </td>
                <td>
                  <DeleteRunButton run={run} onDeleted={handleRunDeleted} />
                </td>
              </tr>
            ))}
            {runs.length === 0 && (
              <tr>
                <td className="empty-note" colSpan={isAdmin ? 10 : 9}>
                  No runs yet — <Link to="/runs/new">start one</Link>.
                </td>
              </tr>
            )}
          </tbody>
        </table>
      </div>
      <Pagination page={page} pageCount={pageCount} onPageChange={setPage} />
    </div>
  )
}
