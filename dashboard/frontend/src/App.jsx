import { Navigate, NavLink, Route, Routes, useLocation } from 'react-router-dom'
import { AuthProvider, useAuth } from './AuthContext.jsx'
import ConnectionsPage from './pages/ConnectionsPage.jsx'
import LoginPage from './pages/LoginPage.jsx'
import NewRunPage from './pages/NewRunPage.jsx'
import RunDetailPage from './pages/RunDetailPage.jsx'
import RunsListPage from './pages/RunsListPage.jsx'
import UsersPage from './pages/UsersPage.jsx'

function RequireAuth({ children }) {
  const { user, loading } = useAuth()
  const location = useLocation()
  if (loading) return <p>Loading…</p>
  if (!user) return <Navigate to="/login" replace state={{ from: location.pathname }} />
  return children
}

function RequireAdmin({ children }) {
  const { user, loading } = useAuth()
  if (loading) return <p>Loading…</p>
  if (user?.role !== 'admin') return <Navigate to="/runs" replace />
  return children
}

function Header() {
  const { user, logout } = useAuth()
  if (!user) return null
  return (
    <header className="app-header">
      <h1>Sonar Remediation Dashboard</h1>
      <nav>
        <NavLink to="/runs" className={({ isActive }) => (isActive ? 'active' : '')} end>
          Runs
        </NavLink>
        <NavLink to="/runs/new" className={({ isActive }) => (isActive ? 'active' : '')}>
          New Run
        </NavLink>
        <NavLink to="/connections" className={({ isActive }) => (isActive ? 'active' : '')}>
          Connections
        </NavLink>
        {user.role === 'admin' && (
          <NavLink to="/users" className={({ isActive }) => (isActive ? 'active' : '')}>
            Users
          </NavLink>
        )}
      </nav>
      <span className="header-user">
        {user.email}
        <button type="button" onClick={logout}>
          Log out
        </button>
      </span>
    </header>
  )
}

function AppRoutes() {
  return (
    <Routes>
      <Route path="/login" element={<LoginPage />} />
      <Route path="/" element={<Navigate to="/runs" replace />} />
      <Route
        path="/runs"
        element={
          <RequireAuth>
            <RunsListPage />
          </RequireAuth>
        }
      />
      <Route
        path="/runs/new"
        element={
          <RequireAuth>
            <NewRunPage />
          </RequireAuth>
        }
      />
      <Route
        path="/runs/:runId"
        element={
          <RequireAuth>
            <RunDetailPage />
          </RequireAuth>
        }
      />
      <Route
        path="/connections"
        element={
          <RequireAuth>
            <ConnectionsPage />
          </RequireAuth>
        }
      />
      <Route
        path="/users"
        element={
          <RequireAuth>
            <RequireAdmin>
              <UsersPage />
            </RequireAdmin>
          </RequireAuth>
        }
      />
    </Routes>
  )
}

export default function App() {
  return (
    <AuthProvider>
      <div className="app">
        <Header />
        <main className="app-main">
          <AppRoutes />
        </main>
      </div>
    </AuthProvider>
  )
}
