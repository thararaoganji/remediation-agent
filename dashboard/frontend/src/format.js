// Mirrors core/agents/report.py's _format_duration on the backend --
// same "Xh Ym Zs, dropping leading zero units" shape, for consistency
// between what a run's own text report says and what this UI shows.
export function formatDuration(ms) {
  const totalSeconds = Math.floor(ms / 1000)
  const hours = Math.floor(totalSeconds / 3600)
  const minutes = Math.floor((totalSeconds % 3600) / 60)
  const seconds = totalSeconds % 60
  if (hours) return `${hours}h ${minutes}m ${seconds}s`
  if (minutes) return `${minutes}m ${seconds}s`
  return `${seconds}s`
}

export function runDuration(run) {
  if (!run.started_at) return '—'
  const start = new Date(run.started_at)
  const end = run.finished_at ? new Date(run.finished_at) : new Date()
  return formatDuration(end - start)
}

export function formatTimestamp(iso) {
  if (!iso) return '—'
  return new Date(iso).toLocaleString()
}

// A push failure doesn't unwind the run (fixes are already committed
// locally, see git_tools.push_branch's docstring for why that's
// deliberate) -- but the dashboard must not let that read as unqualified
// success either. Detected from the exact "failed — {reason}" shape
// PushStep (core/agents/report.py) always uses. Shared by RunsListPage
// (a compact indicator) and RunDetailPage (the full warning banner).
export function pushFailed(finalReport) {
  return typeof finalReport?.push_result === 'string' && finalReport.push_result.startsWith('failed')
}
