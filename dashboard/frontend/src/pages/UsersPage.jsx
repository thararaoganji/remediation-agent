import { useEffect, useState } from 'react'
import { Link } from 'react-router-dom'
import { api } from '../api.js'
import { useAuth } from '../AuthContext.jsx'
import ConfirmDialog from '../components/ConfirmDialog.jsx'
import { PlusIcon, TrashIcon } from '../components/icons.jsx'
import Pagination from '../components/Pagination.jsx'
import { usePagination } from '../usePagination.js'
import { usePageTitle } from '../usePageTitle.js'

const PAGE_SIZE = 10

function UserRow({ user, isSelf, onChanged }) {
  const [confirming, setConfirming] = useState(false)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState(null)

  async function handleDelete() {
    setBusy(true)
    setError(null)
    try {
      await api.deleteUser(user.email)
      onChanged()
    } catch (err) {
      setError(err.message)
      setBusy(false)
      setConfirming(false)
    }
  }

  return (
    <tr>
      <td>
        <strong>{user.email}</strong>
        {isSelf && ' (you)'}
      </td>
      <td>
        {user.role}
        {user.must_reset_password && <span className="warning"> · reset pending</span>}
      </td>
      <td>
        <div className="connection-actions">
          {!isSelf && (
            <button onClick={() => setConfirming(true)} className="icon-action danger" aria-label="Delete" title="Delete">
              <TrashIcon />
            </button>
          )}
          {error && <p className="error">{error}</p>}
        </div>
        <ConfirmDialog
          open={confirming}
          title="Delete user"
          message={`Delete "${user.email}"? Their runs and connections are unaffected.`}
          busy={busy}
          onConfirm={handleDelete}
          onCancel={() => setConfirming(false)}
        />
      </td>
    </tr>
  )
}

export default function UsersPage() {
  usePageTitle('Users')
  const { user: currentUser } = useAuth()
  const [users, setUsers] = useState([])
  const [error, setError] = useState(null)
  const { page, setPage, pageCount, pageItems } = usePagination(users, PAGE_SIZE)

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
      <div className="page-head-row">
        <h2>Users</h2>
        <Link to="/users/new" className="button-link icon-button-link" aria-label="Create User" title="Create User">
          <PlusIcon size={18} />
        </Link>
      </div>
      <p className="section-note">Admin-only — there's no self-signup, so this is how accounts get created.</p>
      {error && <p className="error">Failed to load: {error}</p>}
      <table className="data-table">
        <thead>
          <tr>
            <th>Email</th>
            <th>Role</th>
            <th></th>
          </tr>
        </thead>
        <tbody>
          {pageItems.map((u) => (
            <UserRow key={u.email} user={u} isSelf={u.email === currentUser?.email} onChanged={refresh} />
          ))}
          {users.length === 0 && !error && (
            <tr>
              <td className="empty-note" colSpan={3}>
                No users yet.
              </td>
            </tr>
          )}
        </tbody>
      </table>
      <Pagination page={page} pageCount={pageCount} onPageChange={setPage} />
    </div>
  )
}
