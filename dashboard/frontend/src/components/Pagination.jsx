export default function Pagination({ page, pageCount, onPageChange }) {
  if (pageCount <= 1) return null

  return (
    <div className="pagination">
      <button type="button" onClick={() => onPageChange(page - 1)} disabled={page <= 1}>
        ← Prev
      </button>
      <span>
        Page {page} of {pageCount}
      </span>
      <button type="button" onClick={() => onPageChange(page + 1)} disabled={page >= pageCount}>
        Next →
      </button>
    </div>
  )
}
