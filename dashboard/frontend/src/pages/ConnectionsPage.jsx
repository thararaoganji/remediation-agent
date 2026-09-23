import { useEffect, useState } from 'react'
import { api } from '../api.js'
import { useAuth } from '../AuthContext.jsx'

function SonarServerForm({ onCreated }) {
  const [name, setName] = useState('')
  const [baseUrl, setBaseUrl] = useState('')
  const [ceEdition, setCeEdition] = useState(true)
  const [token, setToken] = useState('')
  const [error, setError] = useState(null)
  const [submitting, setSubmitting] = useState(false)

  async function handleSubmit(e) {
    e.preventDefault()
    setSubmitting(true)
    setError(null)
    try {
      await api.createSonarServer({ name, base_url: baseUrl, ce_edition: ceEdition, token })
      setName('')
      setBaseUrl('')
      setToken('')
      setCeEdition(true)
      onCreated()
    } catch (err) {
      setError(err.message)
    } finally {
      setSubmitting(false)
    }
  }

  return (
    <form className="add-form" onSubmit={handleSubmit}>
      <input placeholder="Name (e.g. Prod Sonar)" value={name} onChange={(e) => setName(e.target.value)} required />
      <input
        placeholder="Base URL (e.g. https://sonar.example.com)"
        value={baseUrl}
        onChange={(e) => setBaseUrl(e.target.value)}
        required
      />
      <label className="checkbox-label">
        <input type="checkbox" checked={ceEdition} onChange={(e) => setCeEdition(e.target.checked)} />
        Community Edition
      </label>
      <input placeholder="Token" type="password" value={token} onChange={(e) => setToken(e.target.value)} required />
      <button type="submit" disabled={submitting}>
        Add Sonar Server
      </button>
      {error && <p className="error">{error}</p>}
    </form>
  )
}

function SonarServerRow({ server, showOwner, onChanged }) {
  const [rotating, setRotating] = useState(false)
  const [newToken, setNewToken] = useState('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState(null)
  const [justRotated, setJustRotated] = useState(false)

  async function handleRotate(e) {
    e.preventDefault()
    setBusy(true)
    setError(null)
    try {
      await api.updateSonarServer(server.id, { token: newToken })
      setNewToken('')
      setRotating(false)
      setJustRotated(true)
      onChanged()
    } catch (err) {
      setError(err.message)
    } finally {
      setBusy(false)
    }
  }

  async function handleDelete() {
    if (!window.confirm(`Delete Sonar server "${server.name}"? Runs already using it are unaffected.`)) return
    setBusy(true)
    setError(null)
    try {
      await api.deleteSonarServer(server.id)
      onChanged()
    } catch (err) {
      setError(err.message)
      setBusy(false)
    }
  }

  return (
    <li className="connection-row">
      <div className="connection-info">
        <strong>{server.name}</strong> — {server.base_url}
        {server.ce_edition ? ' (Community Edition)' : ''}
        {showOwner && <span className="field-note"> · owner: {server.owner_email}</span>}
        {justRotated && !rotating && <span className="success"> ✓ Token rotated</span>}
      </div>
      <div className="connection-actions">
        {rotating ? (
          <form onSubmit={handleRotate} className="rotate-form">
            <input
              placeholder="New token"
              type="password"
              value={newToken}
              onChange={(e) => setNewToken(e.target.value)}
              disabled={busy}
              required
            />
            <button type="submit" disabled={busy}>
              {busy ? 'Saving…' : 'Save'}
            </button>
            <button type="button" onClick={() => setRotating(false)} disabled={busy}>
              Cancel
            </button>
            {error && <p className="error">{error}</p>}
          </form>
        ) : (
          <>
            <button
              onClick={() => {
                setRotating(true)
                setJustRotated(false)
                setError(null)
              }}
              disabled={busy}
            >
              Rotate token
            </button>
            <button onClick={handleDelete} className="danger" disabled={busy}>
              {busy ? 'Deleting…' : 'Delete'}
            </button>
            {error && <p className="error">{error}</p>}
          </>
        )}
      </div>
    </li>
  )
}

function GithubCredentialForm({ onCreated }) {
  const [name, setName] = useState('')
  const [apiBaseUrl, setApiBaseUrl] = useState('')
  const [token, setToken] = useState('')
  const [error, setError] = useState(null)
  const [submitting, setSubmitting] = useState(false)

  async function handleSubmit(e) {
    e.preventDefault()
    setSubmitting(true)
    setError(null)
    try {
      const body = { name, token }
      if (apiBaseUrl.trim()) body.api_base_url = apiBaseUrl.trim()
      await api.createGithubCredential(body)
      setName('')
      setApiBaseUrl('')
      setToken('')
      onCreated()
    } catch (err) {
      setError(err.message)
    } finally {
      setSubmitting(false)
    }
  }

  return (
    <form className="add-form" onSubmit={handleSubmit}>
      <input placeholder="Name (e.g. Acme Org)" value={name} onChange={(e) => setName(e.target.value)} required />
      <input
        placeholder="API base URL (leave blank for github.com)"
        value={apiBaseUrl}
        onChange={(e) => setApiBaseUrl(e.target.value)}
      />
      <input placeholder="Token" type="password" value={token} onChange={(e) => setToken(e.target.value)} required />
      <button type="submit" disabled={submitting}>
        Add GitHub Credential
      </button>
      {error && <p className="error">{error}</p>}
    </form>
  )
}

function GithubCredentialRow({ credential, showOwner, onChanged }) {
  const [rotating, setRotating] = useState(false)
  const [newToken, setNewToken] = useState('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState(null)
  const [justRotated, setJustRotated] = useState(false)

  async function handleRotate(e) {
    e.preventDefault()
    setBusy(true)
    setError(null)
    try {
      await api.updateGithubCredential(credential.id, { token: newToken })
      setNewToken('')
      setRotating(false)
      setJustRotated(true)
      onChanged()
    } catch (err) {
      setError(err.message)
    } finally {
      setBusy(false)
    }
  }

  async function handleDelete() {
    if (!window.confirm(`Delete GitHub credential "${credential.name}"? Runs already using it are unaffected.`)) return
    setBusy(true)
    setError(null)
    try {
      await api.deleteGithubCredential(credential.id)
      onChanged()
    } catch (err) {
      setError(err.message)
      setBusy(false)
    }
  }

  return (
    <li className="connection-row">
      <div className="connection-info">
        <strong>{credential.name}</strong> — {credential.api_base_url}
        {showOwner && <span className="field-note"> · owner: {credential.owner_email}</span>}
        {justRotated && !rotating && <span className="success"> ✓ Token rotated</span>}
      </div>
      <div className="connection-actions">
        {rotating ? (
          <form onSubmit={handleRotate} className="rotate-form">
            <input
              placeholder="New token"
              type="password"
              value={newToken}
              onChange={(e) => setNewToken(e.target.value)}
              disabled={busy}
              required
            />
            <button type="submit" disabled={busy}>
              {busy ? 'Saving…' : 'Save'}
            </button>
            <button type="button" onClick={() => setRotating(false)} disabled={busy}>
              Cancel
            </button>
            {error && <p className="error">{error}</p>}
          </form>
        ) : (
          <>
            <button
              onClick={() => {
                setRotating(true)
                setJustRotated(false)
                setError(null)
              }}
              disabled={busy}
            >
              Rotate token
            </button>
            <button onClick={handleDelete} className="danger" disabled={busy}>
              {busy ? 'Deleting…' : 'Delete'}
            </button>
            {error && <p className="error">{error}</p>}
          </>
        )}
      </div>
    </li>
  )
}

function GoogleApiKeySection() {
  const [configured, setConfigured] = useState(null)
  const [value, setValue] = useState('')
  const [loadError, setLoadError] = useState(null)
  const [submitError, setSubmitError] = useState(null)
  const [submitting, setSubmitting] = useState(false)
  const [justUpdated, setJustUpdated] = useState(false)

  async function refresh() {
    try {
      const status = await api.getGoogleApiKeyStatus()
      setConfigured(status.configured)
      setLoadError(null)
    } catch (err) {
      setLoadError(err.message)
    }
  }

  useEffect(() => {
    refresh()
  }, [])

  async function handleSubmit(e) {
    e.preventDefault()
    setSubmitting(true)
    setSubmitError(null)
    try {
      await api.setGoogleApiKey(value)
      setValue('')
      await refresh()
      setJustUpdated(true)
    } catch (err) {
      setSubmitError(err.message)
    } finally {
      setSubmitting(false)
    }
  }

  let statusText = '…'
  if (configured !== null) {
    statusText = configured ? '✓ configured' : '✗ not set'
  } else if (loadError) {
    statusText = `failed to load (${loadError})`
  }

  return (
    <section className="connections-section">
      <h2>Google API Key</h2>
      <p className="section-note">
        Single global key for Gemini access (used by every run, regardless of which Sonar server or GitHub
        credential it uses). Status: {statusText}
        {justUpdated && <span className="success"> ✓ Saved</span>}
      </p>
      <form className="add-form" onSubmit={handleSubmit}>
        <input
          placeholder={configured ? 'Replace key' : 'Google API key'}
          type="password"
          value={value}
          onChange={(e) => {
            setValue(e.target.value)
            setJustUpdated(false)
          }}
          required
        />
        <button type="submit" disabled={submitting}>
          {submitting ? 'Saving…' : configured ? 'Replace' : 'Save'}
        </button>
        {submitError && <p className="error">{submitError}</p>}
      </form>
    </section>
  )
}

export default function ConnectionsPage() {
  const { user } = useAuth()
  const isAdmin = user?.role === 'admin'
  const [sonarServers, setSonarServers] = useState([])
  const [sonarServersError, setSonarServersError] = useState(null)
  const [githubCredentials, setGithubCredentials] = useState([])
  const [githubCredentialsError, setGithubCredentialsError] = useState(null)

  async function refreshSonarServers() {
    try {
      setSonarServers(await api.listSonarServers())
      setSonarServersError(null)
    } catch (err) {
      setSonarServersError(err.message)
    }
  }

  async function refreshGithubCredentials() {
    try {
      setGithubCredentials(await api.listGithubCredentials())
      setGithubCredentialsError(null)
    } catch (err) {
      setGithubCredentialsError(err.message)
    }
  }

  useEffect(() => {
    refreshSonarServers()
    refreshGithubCredentials()
  }, [])

  return (
    <div className="connections-page">
      <section className="connections-section">
        <h2>SonarQube Servers</h2>
        {sonarServersError && <p className="error">Failed to load: {sonarServersError}</p>}
        <ul className="connection-list">
          {sonarServers.map((s) => (
            <SonarServerRow key={s.id} server={s} showOwner={isAdmin} onChanged={refreshSonarServers} />
          ))}
          {sonarServers.length === 0 && !sonarServersError && (
            <li className="empty-note">No Sonar servers configured yet.</li>
          )}
        </ul>
        <SonarServerForm onCreated={refreshSonarServers} />
      </section>

      <section className="connections-section">
        <h2>GitHub Credentials</h2>
        {githubCredentialsError && <p className="error">Failed to load: {githubCredentialsError}</p>}
        <ul className="connection-list">
          {githubCredentials.map((c) => (
            <GithubCredentialRow key={c.id} credential={c} showOwner={isAdmin} onChanged={refreshGithubCredentials} />
          ))}
          {githubCredentials.length === 0 && !githubCredentialsError && (
            <li className="empty-note">No GitHub credentials configured yet.</li>
          )}
        </ul>
        <GithubCredentialForm onCreated={refreshGithubCredentials} />
      </section>

      {isAdmin && <GoogleApiKeySection />}
    </div>
  )
}
