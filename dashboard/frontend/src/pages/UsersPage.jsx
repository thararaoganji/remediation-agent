import { useEffect, useState } from 'react'
import { api } from '../api.js'
import { useAuth } from '../AuthContext.jsx'

function UserCreateForm({ onCreated }) {
  const [email, setEmail] = useState('')
  const [password, setPassword] = useState('')
  const [role, setRole] = useState('user')
  const [error, setError] = useState(null)
  const [submitting, setSubmitting] = useState(false)

  async function handleSubmit(e) {
    e.preventDefault()
    setSubmitting(true)
    setError(null)
    try {
      await api.createUser({ email, password, role })
      setEmail('')
      setPassword('')
      setRole('user')
      onCreated()
    } catch (err) {
      setError(err.message)
    } finally {
      setSubmitting(false)
    }
  }

  return (
    <form className="add-form" onSubmit={handleSubmit}>
      <input type="email" placeholder="Email" value={email} onChange={(e) => setEmail(e.target.value)} required />
      <input
        type="password"
        placeholder="Password"
        value={password}
        onChange={(e) => setPassword(e.target.value)}
        required
      />
      <select value={role} onChange={(e) => setRole(e.target.value)}>
        <option value="user">user</option>
        <option value="admin">admin</option>
      </select>
      <button type="submit" disabled={submitting}>
        {submitting ? 'Creating…' : 'Create user'}
      </button>
      {error && <p className="error">{error}</p>}
    </form>
  )
}

function UserRow({ user, isSelf, onChanged }) {
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState(null)

  async function handleDelete() {
    if (!window.confirm(`Delete user "${user.email}"? Their runs and connections are unaffected.`)) return
    setBusy(true)
    setError(null)
    try {
      await api.deleteUser(user.email)
      onChanged()
    } catch (err) {
      setError(err.message)
      setBusy(false)
    }
  }

  return (
    <li className="connection-row">
      <div className="connection-info">
        <strong>{user.email}</strong> — {user.role}
        {isSelf && ' (you)'}
      </div>
      <div className="connection-actions">
        {!isSelf && (
          <button onClick={handleDelete} className="danger" disabled={busy}>
            {busy ? 'Deleting…' : 'Delete'}
          </button>
        )}
        {error && <p className="error">{error}</p>}
      </div>
    </li>
  )
}

export default function UsersPage() {
  const { user: currentUser } = useAuth()
  const [users, setUsers] = useState([])
  const [error, setError] = useState(null)

  async function refresh() {
    try {
      setUsers(await api.listUsers())
      setError(null)
    } catch (err) {
      setError(err.message)
    }
  }

  useEffect(() => {
    refresh()
  }, [])

  return (
    <div className="users-page">
      <h2>Users</h2>
      <p className="section-note">Admin-only — there's no self-signup, so this is how accounts get created.</p>
      {error && <p className="error">Failed to load: {error}</p>}
      <ul className="connection-list">
        {users.map((u) => (
          <UserRow key={u.email} user={u} isSelf={u.email === currentUser?.email} onChanged={refresh} />
        ))}
        {users.length === 0 && !error && <li className="empty-note">No users yet.</li>}
      </ul>
      <UserCreateForm onCreated={refresh} />
    </div>
  )
}
