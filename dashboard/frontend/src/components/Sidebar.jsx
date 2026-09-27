import { useState } from 'react'
import { NavLink, useLocation } from 'react-router-dom'
import { useAuth } from '../AuthContext.jsx'
import { ChevronIcon, GitBranchIcon, HelpIcon, LinkIcon, ListIcon, SonarIcon, SparkleIcon, UsersIcon } from './icons.jsx'

const STORAGE_KEY = 'sidebar-collapsed'
const CONNECTIONS_EXPANDED_KEY = 'sidebar-connections-expanded'

function readStoredCollapsed() {
  try {
    return localStorage.getItem(STORAGE_KEY) === 'true'
  } catch {
    // Private window / blocked site data -- just default to expanded.
    return false
  }
}

function readStoredConnectionsExpanded() {
  try {
    const value = localStorage.getItem(CONNECTIONS_EXPANDED_KEY)
    return value === null ? true : value === 'true'
  } catch {
    return true
  }
}

const linkClass = ({ isActive }) => (isActive ? 'active' : '')

export default function Sidebar() {
  const { user } = useAuth()
  const location = useLocation()
  const [collapsed, setCollapsed] = useState(readStoredCollapsed)
  const [connectionsExpanded, setConnectionsExpanded] = useState(readStoredConnectionsExpanded)
  if (!user) return null

  const onConnectionsPage = location.pathname.startsWith('/connections')

  function toggle() {
    setCollapsed((prev) => {
      const next = !prev
      try {
        localStorage.setItem(STORAGE_KEY, String(next))
      } catch {
        // Nothing to persist to -- the toggle still works for this visit.
      }
      return next
    })
  }

  function toggleConnections() {
    setConnectionsExpanded((prev) => {
      const next = !prev
      try {
        localStorage.setItem(CONNECTIONS_EXPANDED_KEY, String(next))
      } catch {
        // Nothing to persist to -- the toggle still works for this visit.
      }
      return next
    })
  }

  return (
    <nav className={collapsed ? 'sidebar collapsed' : 'sidebar'}>
      <NavLink to="/runs" className={linkClass} end title="Runs">
        <ListIcon />
        {!collapsed && <span>Runs</span>}
      </NavLink>
      <div className="sidebar-item-row">
        <NavLink to="/connections/sonar-servers" className={onConnectionsPage ? 'active' : ''} title="Connections">
          <LinkIcon />
          {!collapsed && <span>Connections</span>}
        </NavLink>
        {!collapsed && (
          <button
            type="button"
            className="sidebar-subnav-toggle"
            onClick={toggleConnections}
            aria-label={connectionsExpanded ? 'Collapse Connections' : 'Expand Connections'}
            title={connectionsExpanded ? 'Collapse' : 'Expand'}
          >
            <ChevronIcon size={14} flipped={!connectionsExpanded} />
          </button>
        )}
      </div>
      {!collapsed && connectionsExpanded && (
        <div className="sidebar-subnav">
          <NavLink to="/connections/sonar-servers" className={linkClass} title="SonarQube Servers">
            <SonarIcon size={16} />
            <span>SonarQube</span>
          </NavLink>
          <NavLink to="/connections/github-credentials" className={linkClass} title="GitHub Credentials">
            <GitBranchIcon size={16} />
            <span>GitHub</span>
          </NavLink>
          {user.role === 'admin' && (
            <NavLink to="/connections/llm-configs" className={linkClass} title="LLM API Keys">
              <SparkleIcon size={16} />
              <span>LLM</span>
            </NavLink>
          )}
        </div>
      )}
      {user.role === 'admin' && (
        <NavLink to="/users" className={linkClass} title="Users">
          <UsersIcon />
          {!collapsed && <span>Users</span>}
        </NavLink>
      )}
      <NavLink to="/help" className={linkClass} title="Help">
        <HelpIcon />
        {!collapsed && <span>Help</span>}
      </NavLink>
      <button
        type="button"
        className="sidebar-toggle"
        onClick={toggle}
        aria-label={collapsed ? 'Expand sidebar' : 'Collapse sidebar'}
        title={collapsed ? 'Expand' : 'Collapse'}
      >
        <ChevronIcon flipped={collapsed} />
      </button>
    </nav>
  )
}
