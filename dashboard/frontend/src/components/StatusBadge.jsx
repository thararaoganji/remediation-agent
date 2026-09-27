const KNOWN_STATUSES = new Set(['queued', 'running', 'succeeded', 'failed'])

export default function StatusBadge({ status }) {
  const statusClass = KNOWN_STATUSES.has(status) ? status : 'queued'
  return <span className={`status-badge ${statusClass}`}>{status}</span>
}
