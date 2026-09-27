import { useEffect, useMemo, useState } from 'react'

// Client-side pagination -- every list here is already fetched in full by
// the existing api.list*() calls (see firestore_db.py's own "sort/filter
// client-side, no composite indexes" philosophy on the backend), so there's
// no server-side page param to wire up; this just slices what's already in
// memory. Resets to the last valid page if the list shrinks out from under
// the current one (e.g. deleting the only row on the last page).
export function usePagination(items, pageSize = 10) {
  const [page, setPage] = useState(1)
  const pageCount = Math.max(1, Math.ceil(items.length / pageSize))

  useEffect(() => {
    if (page > pageCount) setPage(pageCount)
  }, [page, pageCount])

  const pageItems = useMemo(() => {
    const start = (page - 1) * pageSize
    return items.slice(start, start + pageSize)
  }, [items, page, pageSize])

  return { page, setPage, pageCount, pageItems }
}
