import { useCallback, useEffect, useRef, useState } from 'react'

const AUTO_DISMISS_MS = 4000

// Shared by every connections page (SonarServers/GithubCredentials/
// LlmConfigs) to show a generic success/failure popup after add/update/
// delete -- one hook instead of copy-pasting the same timer-management
// logic three times. A second call while one toast is still showing
// replaces it and restarts the timer, rather than queuing, since only one
// action's outcome is ever relevant at a time on these pages.
export function useToast() {
  const [toast, setToast] = useState(null)
  const timerRef = useRef(null)

  const showToast = useCallback((type, message) => {
    clearTimeout(timerRef.current)
    setToast({ type, message })
    timerRef.current = setTimeout(() => setToast(null), AUTO_DISMISS_MS)
  }, [])

  useEffect(() => () => clearTimeout(timerRef.current), [])

  return { toast, showToast }
}
