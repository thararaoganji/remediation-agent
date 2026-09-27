import { Link, matchPath, useLocation } from 'react-router-dom'
import { useAuth } from '../AuthContext.jsx'

// One config, matched against the current URL -- rendered once, globally
// (see App.jsx), instead of every page building its own trail by hand.
// First match wins; add a new page here and every route gets it for free.
const ROUTES = [
  { path: '/runs', trail: () => [{ label: 'Runs' }] },
  { path: '/runs/new', trail: () => [{ label: 'Runs', to: '/runs' }, { label: 'New Run' }] },
  { path: '/runs/:runId', trail: () => [{ label: 'Runs', to: '/runs' }, { label: 'Run Details' }] },
  {
    path: '/connections/sonar-servers',
    trail: () => [{ label: 'Connections', to: '/connections/sonar-servers' }, { label: 'SonarQube Servers' }],
  },
  {
    path: '/connections/github-credentials',
    trail: () => [{ label: 'Connections', to: '/connections/sonar-servers' }, { label: 'GitHub Credentials' }],
  },
  {
    path: '/connections/llm-configs',
    trail: () => [{ label: 'Connections', to: '/connections/sonar-servers' }, { label: 'LLM API Keys' }],
  },
  { path: '/users', trail: () => [{ label: 'Users' }] },
  { path: '/help', trail: () => [{ label: 'Help' }] },
  { path: '/reset-password', trail: () => [{ label: 'Reset Password' }] },
]

export default function Breadcrumb() {
  const { user } = useAuth()
  const location = useLocation()
  if (!user) return null

  const matched = ROUTES.find((r) => matchPath({ path: r.path, end: true }, location.pathname))
  if (!matched) return null
  const items = matched.trail()
  // A single-segment trail (Runs, Users, Help, ...) has nowhere to link to
  // and just repeats the page's own heading -- only worth showing once
  // there's an actual parent to navigate back to.
  if (items.length <= 1) return null

  return (
    <nav className="breadcrumb" aria-label="Breadcrumb">
      {items.map((item, i) => {
        const isLast = i === items.length - 1
        return (
          <span key={item.label} className="breadcrumb-item">
            {!isLast && item.to ? <Link to={item.to}>{item.label}</Link> : <span aria-current="page">{item.label}</span>}
            {!isLast && <span className="breadcrumb-sep">›</span>}
          </span>
        )
      })}
    </nav>
  )
}
