import { useState } from 'react'
import { NavLink } from 'react-router-dom'
import { useAuth } from '../AuthContext.jsx'
import { ChevronIcon, HelpIcon, LinkIcon, ListIcon, UsersIcon } from './icons.jsx'

const STORAGE_KEY = 'sidebar-collapsed'

function readStoredCollapsed() {
  try {
    return localStorage.getItem(STORAGE_KEY) === 'true'
  } catch {
    // Private window / blocked site data -- just default to expanded.
    return false
  }
}

const linkClass = ({ isActive }) => (isActive ? 'active' : '')

export default function Sidebar() {
  const { user } = useAuth()
  const [collapsed, setCollapsed] = useState(readStoredCollapsed)
  if (!user) return null

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

  return (
    <nav className={collapsed ? 'sidebar collapsed' : 'sidebar'}>
      <NavLink to="/runs" className={linkClass} end title="Runs">
        <ListIcon />
        {!collapsed && <span>Runs</span>}
      </NavLink>
      <NavLink to="/connections" className={linkClass} title="Connections">
        <LinkIcon />
        {!collapsed && <span>Connections</span>}
      </NavLink>
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
