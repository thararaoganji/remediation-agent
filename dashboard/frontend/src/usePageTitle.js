import { useEffect } from 'react'

const APP_NAME = 'Sonar Remediation Dashboard'

export function usePageTitle(title) {
  useEffect(() => {
    document.title = title ? `${title} · ${APP_NAME}` : APP_NAME
  }, [title])
}
