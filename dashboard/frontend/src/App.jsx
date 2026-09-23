import { Link, Navigate, Route, Routes, useLocation } from 'react-router-dom'
import { AuthProvider, useAuth } from './AuthContext.jsx'
import Logo from './components/Logo.jsx'
import Sidebar from './components/Sidebar.jsx'
import ThemeToggle from './components/ThemeToggle.jsx'
import { ThemeProvider } from './ThemeContext.jsx'
import ConnectionsPage from './pages/ConnectionsPage.jsx'
import GithubCredentialFormPage from './pages/GithubCredentialFormPage.jsx'
import HelpPage from './pages/HelpPage.jsx'
import LlmConfigFormPage from './pages/LlmConfigFormPage.jsx'
import LoginPage from './pages/LoginPage.jsx'
import NewRunPage from './pages/NewRunPage.jsx'
import ResetPasswordPage from './pages/ResetPasswordPage.jsx'
import RunDetailPage from './pages/RunDetailPage.jsx'
import RunsListPage from './pages/RunsListPage.jsx'
import SonarServerFormPage from './pages/SonarServerFormPage.jsx'
import UserFormPage from './pages/UserFormPage.jsx'
import UsersPage from './pages/UsersPage.jsx'

function RequireAuth({ children }) {
  const { user, loading } = useAuth()
  const location = useLocation()
  if (loading) return <p>Loading…</p>
  if (!user) return <Navigate to="/login" replace state={{ from: location.pathname }} />
  if (user.must_reset_password && location.pathname !== '/reset-password') {
    return <Navigate to="/reset-password" replace />
  }
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
      <Link to="/runs" className="app-brand">
        <Logo size={26} />
        <h1>Sonar Remediation Dashboard</h1>
      </Link>
      <span className="header-user">
        <ThemeToggle />
        {user.email}
        <button type="button" onClick={logout}>
          Log out
        </button>
      </span>
    </header>
  )
}

function Footer() {
  return (
    <footer className="app-footer">
      Sonar Remediation Dashboard &copy; {new Date().getFullYear()}
    </footer>
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
        path="/connections/sonar-servers/new"
        element={
          <RequireAuth>
            <SonarServerFormPage />
          </RequireAuth>
        }
      />
      <Route
        path="/connections/sonar-servers/:id/edit"
        element={
          <RequireAuth>
            <SonarServerFormPage />
          </RequireAuth>
        }
      />
      <Route
        path="/connections/github-credentials/new"
        element={
          <RequireAuth>
            <GithubCredentialFormPage />
          </RequireAuth>
        }
      />
      <Route
        path="/connections/github-credentials/:id/edit"
        element={
          <RequireAuth>
            <GithubCredentialFormPage />
          </RequireAuth>
        }
      />
      <Route
        path="/connections/llm-configs/new"
        element={
          <RequireAuth>
            <LlmConfigFormPage />
          </RequireAuth>
        }
      />
      <Route
        path="/connections/llm-configs/:id/edit"
        element={
          <RequireAuth>
            <LlmConfigFormPage />
          </RequireAuth>
        }
      />
      <Route
        path="/help"
        element={
          <RequireAuth>
            <HelpPage />
          </RequireAuth>
        }
      />
      <Route
        path="/reset-password"
        element={
          <RequireAuth>
            <ResetPasswordPage />
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
      <Route
        path="/users/new"
        element={
          <RequireAuth>
            <RequireAdmin>
              <UserFormPage />
            </RequireAdmin>
          </RequireAuth>
        }
      />
    </Routes>
  )
}

export default function App() {
  return (
    <ThemeProvider>
      <AuthProvider>
        <div className="app">
          <Header />
          <div className="app-body">
            <Sidebar />
            <main className="app-main">
              <AppRoutes />
            </main>
          </div>
          <Footer />
        </div>
      </AuthProvider>
    </ThemeProvider>
  )
}
