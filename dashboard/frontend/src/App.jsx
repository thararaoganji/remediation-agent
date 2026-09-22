import { NavLink, Navigate, Route, Routes } from 'react-router-dom'
import ConnectionsPage from './pages/ConnectionsPage.jsx'
import NewRunPage from './pages/NewRunPage.jsx'
import RunDetailPage from './pages/RunDetailPage.jsx'
import RunsListPage from './pages/RunsListPage.jsx'

export default function App() {
  return (
    <div className="app">
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
        </nav>
      </header>
      <main className="app-main">
        <Routes>
          <Route path="/" element={<Navigate to="/runs" replace />} />
          <Route path="/runs" element={<RunsListPage />} />
          <Route path="/runs/new" element={<NewRunPage />} />
          <Route path="/runs/:runId" element={<RunDetailPage />} />
          <Route path="/connections" element={<ConnectionsPage />} />
        </Routes>
      </main>
    </div>
  )
}
