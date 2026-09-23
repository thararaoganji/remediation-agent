import { useEffect, useState } from 'react'
import { Link, useNavigate, useParams } from 'react-router-dom'
import { api } from '../api.js'
import { usePageTitle } from '../usePageTitle.js'

const VENDORS = [
  { value: 'google', label: 'Google', modelPlaceholder: 'e.g. gemini-3.7-flash' },
  { value: 'openai', label: 'OpenAI', modelPlaceholder: 'e.g. gpt-4o' },
  { value: 'anthropic', label: 'Anthropic', modelPlaceholder: 'e.g. claude-3-5-sonnet-20241022' },
]

export default function LlmConfigFormPage() {
  const { id } = useParams()
  const isEdit = Boolean(id)
  usePageTitle(isEdit ? 'Edit LLM API Key' : 'Add LLM API Key')
  const navigate = useNavigate()

  const [vendor, setVendor] = useState(VENDORS[0].value)
  const [model, setModel] = useState('')
  const [apiKey, setApiKey] = useState('')

  const [loading, setLoading] = useState(isEdit)
  const [loadError, setLoadError] = useState(null)
  const [submitting, setSubmitting] = useState(false)
  const [error, setError] = useState(null)

  useEffect(() => {
    if (!isEdit) return
    api
      .listLlmConfigs()
      .then((configs) => {
        const config = configs.find((c) => c.id === id)
        if (!config) {
          setLoadError("Not found, or you don't have access to it.")
          return
        }
        setVendor(config.vendor)
        setModel(config.model)
      })
      .catch((err) => setLoadError(err.message))
      .finally(() => setLoading(false))
  }, [id, isEdit])

  async function handleSubmit(e) {
    e.preventDefault()
    setSubmitting(true)
    setError(null)
    try {
      if (isEdit) {
        const body = { vendor, model }
        if (apiKey.trim()) body.api_key = apiKey.trim()
        await api.updateLlmConfig(id, body)
      } else {
        await api.createLlmConfig({ vendor, model, api_key: apiKey })
      }
      navigate('/connections')
    } catch (err) {
      setError(err.message)
    } finally {
      setSubmitting(false)
    }
  }

  const modelPlaceholder = VENDORS.find((v) => v.value === vendor)?.modelPlaceholder

  return (
    <div className="form-page">
      <Link to="/connections" className="back-link">
        ← Back to Connections
      </Link>
      <h2>{isEdit ? 'Edit LLM API Key' : 'Add LLM API Key'}</h2>

      {loading ? (
        <p>Loading…</p>
      ) : loadError ? (
        <p className="error">{loadError}</p>
      ) : (
        <form className="new-run-form" onSubmit={handleSubmit}>
          <label>
            Vendor
            <select value={vendor} onChange={(e) => setVendor(e.target.value)}>
              {VENDORS.map((v) => (
                <option key={v.value} value={v.value}>
                  {v.label}
                </option>
              ))}
            </select>
          </label>
          <label>
            Model
            <input placeholder={modelPlaceholder} value={model} onChange={(e) => setModel(e.target.value)} required />
          </label>
          <label>
            {isEdit ? 'New API key (leave blank to keep the current one)' : 'API key'}
            <input
              placeholder="API key"
              type="password"
              value={apiKey}
              onChange={(e) => setApiKey(e.target.value)}
              required={!isEdit}
            />
          </label>
          <button type="submit" disabled={submitting}>
            {submitting ? 'Saving…' : isEdit ? 'Save changes' : 'Add LLM API Key'}
          </button>
          {error && <p className="error">{error}</p>}
        </form>
      )}
    </div>
  )
}
