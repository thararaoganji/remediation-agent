import { useEffect, useState } from 'react'
import { Link, useNavigate, useParams } from 'react-router-dom'
import { api } from '../api.js'
import { usePageTitle } from '../usePageTitle.js'
import { isHttpUrl, isNonEmpty } from '../validation.js'

export default function GithubCredentialFormPage() {
  const { id } = useParams()
  const isEdit = Boolean(id)
  usePageTitle(isEdit ? 'Edit GitHub Credential' : 'Add GitHub Credential')
  const navigate = useNavigate()

  const [name, setName] = useState('')
  const [apiBaseUrl, setApiBaseUrl] = useState('')
  const [token, setToken] = useState('')

  const [loading, setLoading] = useState(isEdit)
  const [loadError, setLoadError] = useState(null)
  const [submitting, setSubmitting] = useState(false)
  const [error, setError] = useState(null)

  useEffect(() => {
    if (!isEdit) return
    api
      .listGithubCredentials()
      .then((creds) => {
        const cred = creds.find((c) => c.id === id)
        if (!cred) {
          setLoadError("Not found, or you don't have access to it.")
          return
        }
        setName(cred.name)
        setApiBaseUrl(cred.api_base_url)
      })
      .catch((err) => setLoadError(err.message))
      .finally(() => setLoading(false))
  }, [id, isEdit])

  async function handleSubmit(e) {
    e.preventDefault()
    if (!isNonEmpty(name)) {
      setError('Name is required.')
      return
    }
    // Blank is allowed (defaults to github.com) -- only validate it as a
    // URL when something was actually typed.
    if (isNonEmpty(apiBaseUrl) && !isHttpUrl(apiBaseUrl)) {
      setError('API base URL must start with http:// or https://')
      return
    }
    if (!isEdit && !isNonEmpty(token)) {
      setError('Token is required.')
      return
    }
    setSubmitting(true)
    setError(null)
    try {
      if (isEdit) {
        const body = { name: name.trim() }
        // Omitted (not sent as "") when blank -- an empty string isn't a
        // valid URL, and clearing the box means "don't change it", not
        // "set it to nothing".
        if (isNonEmpty(apiBaseUrl)) body.api_base_url = apiBaseUrl.trim()
        if (token.trim()) body.token = token.trim()
        await api.updateGithubCredential(id, body)
      } else {
        const body = { name: name.trim(), token }
        if (isNonEmpty(apiBaseUrl)) body.api_base_url = apiBaseUrl.trim()
        await api.createGithubCredential(body)
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
      <h2>{isEdit ? 'Edit GitHub Credential' : 'Add GitHub Credential'}</h2>

      {loading ? (
        <p>Loading…</p>
      ) : loadError ? (
        <p className="error">{loadError}</p>
      ) : (
        <form className="new-run-form" onSubmit={handleSubmit}>
          <label>
            Name
            <input placeholder="e.g. Acme Org" value={name} onChange={(e) => setName(e.target.value)} required />
          </label>
          <label>
            API base URL
            <input
              placeholder="Leave blank for github.com"
              value={apiBaseUrl}
              onChange={(e) => setApiBaseUrl(e.target.value)}
            />
          </label>
          <label>
            {isEdit ? 'New token (leave blank to keep the current one)' : 'Token'}
            <input placeholder="Token" type="password" value={token} onChange={(e) => setToken(e.target.value)} required={!isEdit} />
          </label>
          <button type="submit" disabled={submitting}>
            {submitting ? 'Saving…' : isEdit ? 'Save changes' : 'Add GitHub Credential'}
          </button>
          {error && <p className="error">{error}</p>}
        </form>
      )}
    </div>
  )
}
