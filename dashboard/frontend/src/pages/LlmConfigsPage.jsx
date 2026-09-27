import { useEffect, useState } from 'react'
import { Navigate } from 'react-router-dom'
import { api } from '../api.js'
import { useAuth } from '../AuthContext.jsx'
import ConfirmDialog from '../components/ConfirmDialog.jsx'
import { CheckIcon, EditIcon, PlusIcon, SparkleIcon, TrashIcon } from '../components/icons.jsx'
import Pagination from '../components/Pagination.jsx'
import { usePagination } from '../usePagination.js'
import { usePageTitle } from '../usePageTitle.js'
import { isNonEmpty } from '../validation.js'
import { VENDOR_ICONS, VENDOR_LABELS } from '../vendors.jsx'

const PAGE_SIZE = 10

const VENDORS = [
  { value: 'google', label: 'Google', modelPlaceholder: 'e.g. gemini-3.7-flash' },
  { value: 'openai', label: 'OpenAI', modelPlaceholder: 'e.g. gpt-4o' },
  { value: 'anthropic', label: 'Anthropic', modelPlaceholder: 'e.g. claude-3-5-sonnet-20241022' },
]

function LlmConfigRow({ config, onEdit, onChanged }) {
  const [confirming, setConfirming] = useState(false)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState(null)

  async function handleDelete() {
    setBusy(true)
    setError(null)
    try {
      await api.deleteLlmConfig(config.id)
      onChanged()
    } catch (err) {
      setError(err.message)
      setBusy(false)
      setConfirming(false)
    }
  }

  async function handleActivate() {
    setBusy(true)
    setError(null)
    try {
      await api.activateLlmConfig(config.id)
      onChanged()
    } catch (err) {
      setError(err.message)
    } finally {
      setBusy(false)
    }
  }

  const VendorIcon = VENDOR_ICONS[config.vendor]

  return (
    <tr>
      <td>
        <span className="vendor-label">
          {VendorIcon && <VendorIcon />}
          <strong>{VENDOR_LABELS[config.vendor] || config.vendor}</strong>
        </span>
      </td>
      <td>{config.model}</td>
      <td>{config.is_active ? <span className="success">✓ Active</span> : '—'}</td>
      <td>
        <div className="connection-actions">
          {!config.is_active && (
            <button onClick={handleActivate} className="icon-action" aria-label="Activate" title="Activate" disabled={busy}>
              <CheckIcon />
            </button>
          )}
          <button onClick={() => onEdit(config)} className="icon-action" aria-label="Edit" title="Edit">
            <EditIcon />
          </button>
          <button
            onClick={() => setConfirming(true)}
            className="icon-action danger"
            aria-label="Delete"
            title="Delete"
            disabled={busy}
          >
            <TrashIcon />
          </button>
          {error && <p className="error">{error}</p>}
        </div>
        <ConfirmDialog
          open={confirming}
          title="Delete LLM API key"
          message={`Delete the ${VENDOR_LABELS[config.vendor] || config.vendor} config "${config.model}"?`}
          busy={busy}
          onConfirm={handleDelete}
          onCancel={() => setConfirming(false)}
        />
      </td>
    </tr>
  )
}

const BLANK_FORM = { vendor: VENDORS[0].value, model: '', apiKey: '' }

export default function LlmConfigsPage() {
  usePageTitle('LLM API Keys')
  const { user } = useAuth()
  const isAdmin = user?.role === 'admin'
  const [llmConfigs, setLlmConfigs] = useState([])
  const [llmConfigsError, setLlmConfigsError] = useState(null)
  const llmPagination = usePagination(llmConfigs, PAGE_SIZE)

  const [formOpen, setFormOpen] = useState(false)
  const [editingId, setEditingId] = useState(null)
  const [form, setForm] = useState(BLANK_FORM)
  const [submitting, setSubmitting] = useState(false)
  const [formError, setFormError] = useState(null)

  async function refreshLlmConfigs() {
    try {
      setLlmConfigs(await api.listLlmConfigs())
      setLlmConfigsError(null)
    } catch (err) {
      setLlmConfigsError(err.message)
    }
  }

  useEffect(() => {
    if (isAdmin) refreshLlmConfigs()
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [isAdmin])

  function openCreate() {
    setEditingId(null)
    setForm(BLANK_FORM)
    setFormError(null)
    setFormOpen(true)
  }

  function openEdit(config) {
    setEditingId(config.id)
    setForm({ vendor: config.vendor, model: config.model, apiKey: '' })
    setFormError(null)
    setFormOpen(true)
  }

  function closeForm() {
    setFormOpen(false)
  }

  async function handleSubmit(e) {
    e.preventDefault()
    const isEdit = Boolean(editingId)
    if (!isNonEmpty(form.model)) {
      setFormError('Model is required.')
      return
    }
    if (!isEdit && !isNonEmpty(form.apiKey)) {
      setFormError('API key is required.')
      return
    }
    setSubmitting(true)
    setFormError(null)
    try {
      if (isEdit) {
        const body = { vendor: form.vendor, model: form.model.trim() }
        if (form.apiKey.trim()) body.api_key = form.apiKey.trim()
        await api.updateLlmConfig(editingId, body)
      } else {
        await api.createLlmConfig({ vendor: form.vendor, model: form.model.trim(), api_key: form.apiKey })
      }
      setFormOpen(false)
      await refreshLlmConfigs()
    } catch (err) {
      setFormError(err.message)
    } finally {
      setSubmitting(false)
    }
  }

  // LLM API keys are admin-only (same gate the old combined page used) --
  // a non-admin landing here directly (e.g. a stale bookmark) goes to a
  // page they can actually use instead of an empty/broken one.
  if (!isAdmin) return <Navigate to="/connections/sonar-servers" replace />

  const isEdit = Boolean(editingId)
  const modelPlaceholder = VENDORS.find((v) => v.value === form.vendor)?.modelPlaceholder

  return (
    <div className="connections-page">
      <section className="connections-section">
        <div className="page-head-row">
          <h2>
            <SparkleIcon /> LLM API Keys
          </h2>
          <button onClick={openCreate} className="icon-action" aria-label="Add LLM API Key" title="Add LLM API Key">
            <PlusIcon size={18} />
          </button>
        </div>

        {formOpen && (
          <div className="inline-form">
            <form className="new-run-form" onSubmit={handleSubmit}>
              <label>
                Vendor
                <select value={form.vendor} onChange={(e) => setForm((f) => ({ ...f, vendor: e.target.value }))}>
                  {VENDORS.map((v) => (
                    <option key={v.value} value={v.value}>
                      {v.label}
                    </option>
                  ))}
                </select>
              </label>
              <label>
                Model
                <input
                  placeholder={modelPlaceholder}
                  value={form.model}
                  onChange={(e) => setForm((f) => ({ ...f, model: e.target.value }))}
                  required
                  autoFocus
                />
              </label>
              <label>
                {isEdit ? 'New API key (leave blank to keep the current one)' : 'API key'}
                <input
                  placeholder="API key"
                  type="password"
                  value={form.apiKey}
                  onChange={(e) => setForm((f) => ({ ...f, apiKey: e.target.value }))}
                  required={!isEdit}
                />
              </label>
              <div className="inline-form-actions">
                <button type="submit" disabled={submitting}>
                  {submitting ? 'Saving…' : isEdit ? 'Save changes' : 'Add LLM API Key'}
                </button>
                <button type="button" onClick={closeForm} disabled={submitting}>
                  Cancel
                </button>
              </div>
              {formError && <p className="error">{formError}</p>}
            </form>
          </div>
        )}

        {llmConfigsError && <p className="error">Failed to load: {llmConfigsError}</p>}
        <table className="data-table">
          <thead>
            <tr>
              <th>Vendor</th>
              <th>Model</th>
              <th>Status</th>
              <th></th>
            </tr>
          </thead>
          <tbody>
            {llmPagination.pageItems.map((c) => (
              <LlmConfigRow key={c.id} config={c} onEdit={openEdit} onChanged={refreshLlmConfigs} />
            ))}
            {llmConfigs.length === 0 && !llmConfigsError && (
              <tr>
                <td className="empty-note" colSpan={4}>
                  No LLM API keys configured yet — runs can't start until one is added.
                </td>
              </tr>
            )}
          </tbody>
        </table>
        <Pagination page={llmPagination.page} pageCount={llmPagination.pageCount} onPageChange={llmPagination.setPage} />
      </section>
    </div>
  )
}
