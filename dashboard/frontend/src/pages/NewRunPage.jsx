import { useEffect, useState } from 'react'
import { Link, useNavigate } from 'react-router-dom'
import { api } from '../api.js'
import { usePageTitle } from '../usePageTitle.js'
import { isNonEmpty } from '../validation.js'

const AGENT_TYPES = [
  { value: 'techdebt', label: 'Tech-Debt (Security / Reliability / Maintainability)' },
  { value: 'coverage', label: 'Coverage (generates JUnit tests)' },
  { value: 'duplicate', label: 'Duplication (extracts shared logic)' },
]

// Kept as an open list, not hardcoded to only Java's two build tools --
// core/adapters/base.py's ADAPTER_REGISTRY already anticipates more
// languages (e.g. a queued JavaScript/TypeScript adapter); adding one
// there is all that's needed for it to show up here too.
const LANGUAGES = [
  { value: 'java', label: 'Java (auto-detect Maven/Gradle)' },
  { value: 'java-maven', label: 'Java (Maven)' },
  { value: 'java-gradle', label: 'Java (Gradle)' },
]

export default function NewRunPage() {
  usePageTitle('New Run')
  const navigate = useNavigate()
  const [sonarServers, setSonarServers] = useState([])
  const [githubCredentials, setGithubCredentials] = useState([])

  const [agentType, setAgentType] = useState(AGENT_TYPES[0].value)
  const [sourceType, setSourceType] = useState('github')
  const [source, setSource] = useState('')
  const [sourceBranch, setSourceBranch] = useState('')
  const [language, setLanguage] = useState(LANGUAGES[0].value)
  const [sonarServerId, setSonarServerId] = useState('')
  const [githubCredentialId, setGithubCredentialId] = useState('')

  const [submitting, setSubmitting] = useState(false)
  const [error, setError] = useState(null)

  useEffect(() => {
    api
      .listSonarServers()
      .then((servers) => {
        setSonarServers(servers)
        if (servers.length > 0) setSonarServerId(servers[0].id)
      })
      .catch((err) => setError(`Failed to load Sonar servers: ${err.message}`))
    api
      .listGithubCredentials()
      .then((creds) => {
        setGithubCredentials(creds)
        if (creds.length > 0) setGithubCredentialId(creds[0].id)
      })
      .catch((err) => setError(`Failed to load GitHub credentials: ${err.message}`))
  }, [])

  async function handleSubmit(e) {
    e.preventDefault()
    if (!isNonEmpty(source)) {
      setError(sourceType === 'github' ? 'Repository is required.' : 'Path is required.')
      return
    }
    if (!sonarServerId) {
      setError('Select a Sonar server.')
      return
    }
    setSubmitting(true)
    setError(null)
    try {
      const run = await api.createRun({
        agent_type: agentType,
        source_type: sourceType,
        source: source.trim(),
        source_branch: sourceBranch.trim() || null,
        language,
        sonar_server_id: sonarServerId,
        github_credential_id: sourceType === 'github' ? githubCredentialId || null : null,
      })
      navigate(`/runs/${run.id}`)
    } catch (err) {
      setError(err.message)
      setSubmitting(false)
    }
  }

  const noSonarServers = sonarServers.length === 0
  const noGithubCredentials = sourceType === 'github' && githubCredentials.length === 0

  return (
    <div className="new-run-page form-page">
      <Link to="/runs" className="back-link">
        ← Back to Runs
      </Link>
      <h2>Start a new run</h2>
      {noSonarServers && (
        <p className="warning">
          No Sonar servers configured yet — add one on the <a href="/connections">Connections</a> page first.
        </p>
      )}
      <form className="new-run-form" onSubmit={handleSubmit}>
        <label>
          Agent
          <select value={agentType} onChange={(e) => setAgentType(e.target.value)}>
            {AGENT_TYPES.map((t) => (
              <option key={t.value} value={t.value}>
                {t.label}
              </option>
            ))}
          </select>
        </label>

        <label>
          Source
          <select value={sourceType} onChange={(e) => setSourceType(e.target.value)}>
            <option value="github">GitHub repo</option>
            <option value="local">Local path (on the running container)</option>
          </select>
        </label>

        <label>
          {sourceType === 'github' ? 'Repository (owner/repo)' : 'Path'}
          <input
            value={source}
            onChange={(e) => setSource(e.target.value)}
            placeholder={sourceType === 'github' ? 'owner/repo' : '/path/to/project'}
            required
          />
        </label>

        <label>
          Branch (optional — defaults to the repo's default branch)
          <input value={sourceBranch} onChange={(e) => setSourceBranch(e.target.value)} placeholder="e.g. develop" />
        </label>

        <label>
          Language
          <select value={language} onChange={(e) => setLanguage(e.target.value)}>
            {LANGUAGES.map((l) => (
              <option key={l.value} value={l.value}>
                {l.label}
              </option>
            ))}
          </select>
        </label>

        <label>
          Sonar server
          <select value={sonarServerId} onChange={(e) => setSonarServerId(e.target.value)} required>
            <option value="" disabled>
              Select a Sonar server…
            </option>
            {sonarServers.map((s) => (
              <option key={s.id} value={s.id}>
                {s.name} ({s.base_url})
              </option>
            ))}
          </select>
        </label>

        {sourceType === 'github' && (
          <label>
            GitHub credential
            <select value={githubCredentialId} onChange={(e) => setGithubCredentialId(e.target.value)}>
              <option value="">None (public repo, read-only)</option>
              {githubCredentials.map((c) => (
                <option key={c.id} value={c.id}>
                  {c.name} ({c.api_base_url})
                </option>
              ))}
            </select>
            {noGithubCredentials && (
              <span className="field-note">
                No GitHub credentials configured — fine for a public repo, but the run won't be able to push its
                fix branch without one.
              </span>
            )}
          </label>
        )}

        <button type="submit" disabled={submitting || noSonarServers || !sonarServerId}>
          {submitting ? 'Starting…' : 'Start run'}
        </button>
        {error && <p className="error">{error}</p>}
      </form>
    </div>
  )
}
