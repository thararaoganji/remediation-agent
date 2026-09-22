const COLORS = {
  queued: '#9aa0a6',
  running: '#1a73e8',
  succeeded: '#188038',
  failed: '#d93025',
}

export default function StatusBadge({ status }) {
  const color = COLORS[status] || '#9aa0a6'
  return (
    <span className="status-badge" style={{ backgroundColor: color }}>
      {status}
    </span>
  )
}
