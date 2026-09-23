import { useEffect, useState } from 'react'
import { Link, useNavigate, useParams } from 'react-router-dom'
import { api } from '../api.js'
import { usePageTitle } from '../usePageTitle.js'

export default function SonarServerFormPage() {
  const { id } = useParams()
  const isEdit = Boolean(id)
  usePageTitle(isEdit ? 'Edit Sonar Server' : 'Add Sonar Server')
  const navigate = useNavigate()

  const [name, setName] = useState('')
  const [baseUrl, setBaseUrl] = useState('')
  const [ceEdition, setCeEdition] = useState(true)
  const [token, setToken] = useState('')

  const [loading, setLoading] = useState(isEdit)
  const [loadError, setLoadError] = useState(null)
  const [submitting, setSubmitting] = useState(false)
  const [error, setError] = useState(null)

  useEffect(() => {
    if (!isEdit) return
    api
      .listSonarServers()
      .then((servers) => {
        const server = servers.find((s) => s.id === id)
        if (!server) {
          setLoadError("Not found, or you don't have access to it.")
          return
        }
        setName(server.name)
        setBaseUrl(server.base_url)
        setCeEdition(server.ce_edition)
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
        const body = { name, base_url: baseUrl, ce_edition: ceEdition }
        if (token.trim()) body.token = token.trim()
        await api.updateSonarServer(id, body)
      } else {
        await api.createSonarServer({ name, base_url: baseUrl, ce_edition: ceEdition, token })
      }
      navigate('/connections')
    } catch (err) {
      setError(err.message)
    } finally {
      setSubmitting(false)
    }
  }

  return (
    <div className="form-page">
      <Link to="/connections" className="back-link">
        ← Back to Connections
      </Link>
      <h2>{isEdit ? 'Edit Sonar Server' : 'Add Sonar Server'}</h2>

      {loading ? (
        <p>Loading…</p>
      ) : loadError ? (
        <p className="error">{loadError}</p>
      ) : (
        <form className="new-run-form" onSubmit={handleSubmit}>
          <label>
            Name
            <input placeholder="e.g. Prod Sonar" value={name} onChange={(e) => setName(e.target.value)} required />
          </label>
          <label>
            Base URL
            <input
              placeholder="e.g. https://sonar.example.com"
              value={baseUrl}
              onChange={(e) => setBaseUrl(e.target.value)}
              required
            />
          </label>
          <label className="checkbox-label">
            <input type="checkbox" checked={ceEdition} onChange={(e) => setCeEdition(e.target.checked)} />
            Community Edition
          </label>
          <label>
            {isEdit ? 'New token (leave blank to keep the current one)' : 'Token'}
            <input placeholder="Token" type="password" value={token} onChange={(e) => setToken(e.target.value)} required={!isEdit} />
          </label>
          <button type="submit" disabled={submitting}>
            {submitting ? 'Saving…' : isEdit ? 'Save changes' : 'Add Sonar Server'}
          </button>
          {error && <p className="error">{error}</p>}
        </form>
      )}
    </div>
  )
}
