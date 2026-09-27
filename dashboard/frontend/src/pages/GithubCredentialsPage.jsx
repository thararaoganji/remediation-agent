import { useEffect, useState } from 'react'
import { api } from '../api.js'
import { useAuth } from '../AuthContext.jsx'
import ConfirmDialog from '../components/ConfirmDialog.jsx'
import { EditIcon, GitBranchIcon, PlusIcon, TrashIcon } from '../components/icons.jsx'
import Pagination from '../components/Pagination.jsx'
import { usePagination } from '../usePagination.js'
import { usePageTitle } from '../usePageTitle.js'
import { isHttpUrl, isNonEmpty } from '../validation.js'

const PAGE_SIZE = 10

function GithubCredentialRow({ credential, showOwner, onEdit, onChanged }) {
  const [confirming, setConfirming] = useState(false)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState(null)

  async function handleDelete() {
    setBusy(true)
    setError(null)
    try {
      await api.deleteGithubCredential(credential.id)
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
        <strong>{credential.name}</strong>
      </td>
      <td>{credential.api_base_url}</td>
      {showOwner && <td>{credential.owner_email}</td>}
      <td>
        <div className="connection-actions">
          <button onClick={() => onEdit(credential)} className="icon-action" aria-label="Edit" title="Edit">
            <EditIcon />
          </button>
          <button onClick={() => setConfirming(true)} className="icon-action danger" aria-label="Delete" title="Delete">
            <TrashIcon />
          </button>
          {error && <p className="error">{error}</p>}
        </div>
        <ConfirmDialog
          open={confirming}
          title="Delete GitHub credential"
          message={`Delete "${credential.name}"? Runs already using it are unaffected.`}
          busy={busy}
          onConfirm={handleDelete}
          onCancel={() => setConfirming(false)}
        />
      </td>
    </tr>
  )
}

const BLANK_FORM = { name: '', apiBaseUrl: '', token: '' }

export default function GithubCredentialsPage() {
  usePageTitle('GitHub Credentials')
  const { user } = useAuth()
  const isAdmin = user?.role === 'admin'
  const [githubCredentials, setGithubCredentials] = useState([])
  const [githubCredentialsError, setGithubCredentialsError] = useState(null)
  const githubPagination = usePagination(githubCredentials, PAGE_SIZE)

  const [formOpen, setFormOpen] = useState(false)
  const [editingId, setEditingId] = useState(null)
  const [form, setForm] = useState(BLANK_FORM)
  const [submitting, setSubmitting] = useState(false)
  const [formError, setFormError] = useState(null)

  async function refreshGithubCredentials() {
    try {
      setGithubCredentials(await api.listGithubCredentials())
      setGithubCredentialsError(null)
    } catch (err) {
      setGithubCredentialsError(err.message)
    }
  }

  useEffect(() => {
    refreshGithubCredentials()
  }, [])

  function openCreate() {
    setEditingId(null)
    setForm(BLANK_FORM)
    setFormError(null)
    setFormOpen(true)
  }

  function openEdit(credential) {
    setEditingId(credential.id)
    setForm({ name: credential.name, apiBaseUrl: credential.api_base_url || '', token: '' })
    setFormError(null)
    setFormOpen(true)
  }

  function closeForm() {
    setFormOpen(false)
  }

  async function handleSubmit(e) {
    e.preventDefault()
    const isEdit = Boolean(editingId)
    if (!isNonEmpty(form.name)) {
      setFormError('Name is required.')
      return
    }
    // Blank is allowed (defaults to github.com) -- only validate it as a
    // URL when something was actually typed.
    if (isNonEmpty(form.apiBaseUrl) && !isHttpUrl(form.apiBaseUrl)) {
      setFormError('API base URL must start with http:// or https://')
      return
    }
    if (!isEdit && !isNonEmpty(form.token)) {
      setFormError('Token is required.')
      return
    }
    setSubmitting(true)
    setFormError(null)
    try {
      if (isEdit) {
        const body = { name: form.name.trim() }
        // Omitted (not sent as "") when blank -- an empty string isn't a
        // valid URL, and clearing the box means "don't change it", not
        // "set it to nothing".
        if (isNonEmpty(form.apiBaseUrl)) body.api_base_url = form.apiBaseUrl.trim()
        if (form.token.trim()) body.token = form.token.trim()
        await api.updateGithubCredential(editingId, body)
      } else {
        const body = { name: form.name.trim(), token: form.token }
        if (isNonEmpty(form.apiBaseUrl)) body.api_base_url = form.apiBaseUrl.trim()
        await api.createGithubCredential(body)
      }
      setFormOpen(false)
      await refreshGithubCredentials()
    } catch (err) {
      setFormError(err.message)
    } finally {
      setSubmitting(false)
    }
  }

  const isEdit = Boolean(editingId)

  return (
    <div className="connections-page">
      <section className="connections-section">
        <div className="page-head-row">
          <h2>
            <GitBranchIcon /> GitHub Credentials
          </h2>
          <button onClick={openCreate} className="icon-action" aria-label="Add GitHub Credential" title="Add GitHub Credential">
            <PlusIcon size={18} />
          </button>
        </div>

        {formOpen && (
          <div className="inline-form">
            <form className="new-run-form" onSubmit={handleSubmit}>
              <label>
                Name
                <input
                  placeholder="e.g. Acme Org"
                  value={form.name}
                  onChange={(e) => setForm((f) => ({ ...f, name: e.target.value }))}
                  required
                  autoFocus
                />
              </label>
              <label>
                API base URL
                <input
                  placeholder="Leave blank for github.com"
                  value={form.apiBaseUrl}
                  onChange={(e) => setForm((f) => ({ ...f, apiBaseUrl: e.target.value }))}
                />
              </label>
              <label>
                {isEdit ? 'New token (leave blank to keep the current one)' : 'Token'}
                <input
                  placeholder="Token"
                  type="password"
                  value={form.token}
                  onChange={(e) => setForm((f) => ({ ...f, token: e.target.value }))}
                  required={!isEdit}
                />
              </label>
              <div className="inline-form-actions">
                <button type="submit" disabled={submitting}>
                  {submitting ? 'Saving…' : isEdit ? 'Save changes' : 'Add GitHub Credential'}
                </button>
                <button type="button" onClick={closeForm} disabled={submitting}>
                  Cancel
                </button>
              </div>
              {formError && <p className="error">{formError}</p>}
            </form>
          </div>
        )}

        {githubCredentialsError && <p className="error">Failed to load: {githubCredentialsError}</p>}
        <table className="data-table">
          <thead>
            <tr>
              <th>Name</th>
              <th>API base URL</th>
              {isAdmin && <th>Owner</th>}
              <th></th>
            </tr>
          </thead>
          <tbody>
            {githubPagination.pageItems.map((c) => (
              <GithubCredentialRow key={c.id} credential={c} showOwner={isAdmin} onEdit={openEdit} onChanged={refreshGithubCredentials} />
            ))}
            {githubCredentials.length === 0 && !githubCredentialsError && (
              <tr>
                <td className="empty-note" colSpan={isAdmin ? 4 : 3}>
                  No GitHub credentials configured yet.
                </td>
              </tr>
            )}
          </tbody>
        </table>
        <Pagination page={githubPagination.page} pageCount={githubPagination.pageCount} onPageChange={githubPagination.setPage} />
      </section>
    </div>
  )
}
