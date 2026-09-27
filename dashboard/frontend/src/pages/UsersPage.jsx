import { useEffect, useState } from 'react'
import { api } from '../api.js'
import { useAuth } from '../AuthContext.jsx'
import ConfirmDialog from '../components/ConfirmDialog.jsx'
import { PlusIcon, TrashIcon } from '../components/icons.jsx'
import Pagination from '../components/Pagination.jsx'
import { usePagination } from '../usePagination.js'
import { usePageTitle } from '../usePageTitle.js'
import { isValidEmail, MIN_PASSWORD_LENGTH } from '../validation.js'

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

const BLANK_FORM = { email: '', password: '', role: 'user' }

export default function UsersPage() {
  usePageTitle('Users')
  const { user: currentUser } = useAuth()
  const [users, setUsers] = useState([])
  const [error, setError] = useState(null)
  const { page, setPage, pageCount, pageItems } = usePagination(users, PAGE_SIZE)

  const [formOpen, setFormOpen] = useState(false)
  const [form, setForm] = useState(BLANK_FORM)
  const [submitting, setSubmitting] = useState(false)
  const [formError, setFormError] = useState(null)

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

  function openCreate() {
    setForm(BLANK_FORM)
    setFormError(null)
    setFormOpen(true)
  }

  function closeForm() {
    setFormOpen(false)
  }

  async function handleSubmit(e) {
    e.preventDefault()
    if (!isValidEmail(form.email)) {
      setFormError('Enter a valid email address.')
      return
    }
    if (form.password.length < MIN_PASSWORD_LENGTH) {
      setFormError(`Password must be at least ${MIN_PASSWORD_LENGTH} characters.`)
      return
    }
    setSubmitting(true)
    setFormError(null)
    try {
      await api.createUser({ email: form.email.trim(), password: form.password, role: form.role })
      setFormOpen(false)
      await refresh()
    } catch (err) {
      setFormError(err.message)
    } finally {
      setSubmitting(false)
    }
  }

  return (
    <div className="users-page">
      <div className="page-head-row">
        <h2>Users</h2>
        <button onClick={openCreate} className="icon-action" aria-label="Create User" title="Create User">
          <PlusIcon size={18} />
        </button>
      </div>
      <p className="section-note">Admin-only — there's no self-signup, so this is how accounts get created.</p>

      {formOpen && (
        <div className="inline-form">
          <form className="new-run-form" onSubmit={handleSubmit}>
            <label>
              Email
              <input
                type="email"
                value={form.email}
                onChange={(e) => setForm((f) => ({ ...f, email: e.target.value }))}
                required
                autoFocus
              />
            </label>
            <label>
              Password
              <input
                type="password"
                value={form.password}
                onChange={(e) => setForm((f) => ({ ...f, password: e.target.value }))}
                required
              />
              <span className="field-note">At least {MIN_PASSWORD_LENGTH} characters.</span>
            </label>
            <label>
              Role
              <select value={form.role} onChange={(e) => setForm((f) => ({ ...f, role: e.target.value }))}>
                <option value="user">user</option>
                <option value="admin">admin</option>
              </select>
            </label>
            <div className="inline-form-actions">
              <button type="submit" disabled={submitting}>
                {submitting ? 'Creating…' : 'Create user'}
              </button>
              <button type="button" onClick={closeForm} disabled={submitting}>
                Cancel
              </button>
            </div>
            {formError && <p className="error">{formError}</p>}
          </form>
        </div>
      )}

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
