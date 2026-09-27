import { useEffect, useState } from 'react'
import { api } from '../api.js'
import { useAuth } from '../AuthContext.jsx'
import ConfirmDialog from '../components/ConfirmDialog.jsx'
import { EditIcon, PlusIcon, SonarIcon, TrashIcon } from '../components/icons.jsx'
import Pagination from '../components/Pagination.jsx'
import { usePagination } from '../usePagination.js'
import { usePageTitle } from '../usePageTitle.js'
import { isHttpUrl, isNonEmpty } from '../validation.js'

const PAGE_SIZE = 10

function SonarServerRow({ server, showOwner, onEdit, onChanged }) {
  const [confirming, setConfirming] = useState(false)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState(null)

  async function handleDelete() {
    setBusy(true)
    setError(null)
    try {
      await api.deleteSonarServer(server.id)
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
        <strong>{server.name}</strong>
      </td>
      <td>
        {server.base_url}
        {server.ce_edition ? ' (Community Edition)' : ''}
      </td>
      {showOwner && <td>{server.owner_email}</td>}
      <td>
        <div className="connection-actions">
          <button onClick={() => onEdit(server)} className="icon-action" aria-label="Edit" title="Edit">
            <EditIcon />
          </button>
          <button onClick={() => setConfirming(true)} className="icon-action danger" aria-label="Delete" title="Delete">
            <TrashIcon />
          </button>
          {error && <p className="error">{error}</p>}
        </div>
        <ConfirmDialog
          open={confirming}
          title="Delete Sonar server"
          message={`Delete "${server.name}"? Runs already using it are unaffected.`}
          busy={busy}
          onConfirm={handleDelete}
          onCancel={() => setConfirming(false)}
        />
      </td>
    </tr>
  )
}

const BLANK_FORM = { name: '', baseUrl: '', ceEdition: true, token: '' }

export default function SonarServersPage() {
  usePageTitle('SonarQube Servers')
  const { user } = useAuth()
  const isAdmin = user?.role === 'admin'
  const [sonarServers, setSonarServers] = useState([])
  const [sonarServersError, setSonarServersError] = useState(null)
  const sonarPagination = usePagination(sonarServers, PAGE_SIZE)

  const [formOpen, setFormOpen] = useState(false)
  const [editingId, setEditingId] = useState(null)
  const [form, setForm] = useState(BLANK_FORM)
  const [submitting, setSubmitting] = useState(false)
  const [formError, setFormError] = useState(null)

  async function refreshSonarServers() {
    try {
      setSonarServers(await api.listSonarServers())
      setSonarServersError(null)
    } catch (err) {
      setSonarServersError(err.message)
    }
  }

  useEffect(() => {
    refreshSonarServers()
  }, [])

  function openCreate() {
    setEditingId(null)
    setForm(BLANK_FORM)
    setFormError(null)
    setFormOpen(true)
  }

  function openEdit(server) {
    setEditingId(server.id)
    setForm({ name: server.name, baseUrl: server.base_url, ceEdition: server.ce_edition, token: '' })
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
    if (!isHttpUrl(form.baseUrl)) {
      setFormError('Base URL must start with http:// or https://')
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
        const body = { name: form.name.trim(), base_url: form.baseUrl.trim(), ce_edition: form.ceEdition }
        if (form.token.trim()) body.token = form.token.trim()
        await api.updateSonarServer(editingId, body)
      } else {
        await api.createSonarServer({
          name: form.name.trim(),
          base_url: form.baseUrl.trim(),
          ce_edition: form.ceEdition,
          token: form.token,
        })
      }
      setFormOpen(false)
      await refreshSonarServers()
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
            <SonarIcon /> SonarQube Servers
          </h2>
          <button onClick={openCreate} className="icon-action" aria-label="Add Sonar Server" title="Add Sonar Server">
            <PlusIcon size={18} />
          </button>
        </div>

        {formOpen && (
          <div className="inline-form">
            <form className="new-run-form" onSubmit={handleSubmit}>
              <label>
                Name
                <input
                  placeholder="e.g. Prod Sonar"
                  value={form.name}
                  onChange={(e) => setForm((f) => ({ ...f, name: e.target.value }))}
                  required
                  autoFocus
                />
              </label>
              <label>
                Base URL
                <input
                  placeholder="e.g. https://sonar.example.com"
                  value={form.baseUrl}
                  onChange={(e) => setForm((f) => ({ ...f, baseUrl: e.target.value }))}
                  required
                />
              </label>
              <label className="checkbox-field">
                Community Edition
                <input
                  type="checkbox"
                  checked={form.ceEdition}
                  onChange={(e) => setForm((f) => ({ ...f, ceEdition: e.target.checked }))}
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
                  {submitting ? 'Saving…' : isEdit ? 'Save changes' : 'Add Sonar Server'}
                </button>
                <button type="button" onClick={closeForm} disabled={submitting}>
                  Cancel
                </button>
              </div>
              {formError && <p className="error">{formError}</p>}
            </form>
          </div>
        )}

        {sonarServersError && <p className="error">Failed to load: {sonarServersError}</p>}
        <table className="data-table">
          <thead>
            <tr>
              <th>Name</th>
              <th>Base URL</th>
              {isAdmin && <th>Owner</th>}
              <th></th>
            </tr>
          </thead>
          <tbody>
            {sonarPagination.pageItems.map((s) => (
              <SonarServerRow key={s.id} server={s} showOwner={isAdmin} onEdit={openEdit} onChanged={refreshSonarServers} />
            ))}
            {sonarServers.length === 0 && !sonarServersError && (
              <tr>
                <td className="empty-note" colSpan={isAdmin ? 4 : 3}>
                  No Sonar servers configured yet.
                </td>
              </tr>
            )}
          </tbody>
        </table>
        <Pagination page={sonarPagination.page} pageCount={sonarPagination.pageCount} onPageChange={sonarPagination.setPage} />
      </section>
    </div>
  )
}
