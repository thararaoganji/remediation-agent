import { useEffect, useState } from 'react'
import { Link } from 'react-router-dom'
import { api } from '../api.js'
import { useAuth } from '../AuthContext.jsx'
import ConfirmDialog from '../components/ConfirmDialog.jsx'
import {
  AnthropicVendorIcon,
  CheckIcon,
  EditIcon,
  GitBranchIcon,
  GoogleVendorIcon,
  OpenAiVendorIcon,
  PlusIcon,
  SonarIcon,
  SparkleIcon,
  TrashIcon,
} from '../components/icons.jsx'
import Pagination from '../components/Pagination.jsx'
import { usePagination } from '../usePagination.js'
import { usePageTitle } from '../usePageTitle.js'

const PAGE_SIZE = 10

const VENDOR_LABELS = { google: 'Google', openai: 'OpenAI', anthropic: 'Anthropic' }
const VENDOR_ICONS = { google: GoogleVendorIcon, openai: OpenAiVendorIcon, anthropic: AnthropicVendorIcon }

function SonarServerRow({ server, showOwner, onChanged }) {
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
          <Link
            to={`/connections/sonar-servers/${server.id}/edit`}
            className="icon-action"
            aria-label="Edit"
            title="Edit"
          >
            <EditIcon />
          </Link>
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

function GithubCredentialRow({ credential, showOwner, onChanged }) {
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
          <Link
            to={`/connections/github-credentials/${credential.id}/edit`}
            className="icon-action"
            aria-label="Edit"
            title="Edit"
          >
            <EditIcon />
          </Link>
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

function LlmConfigRow({ config, onChanged }) {
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
          <Link to={`/connections/llm-configs/${config.id}/edit`} className="icon-action" aria-label="Edit" title="Edit">
            <EditIcon />
          </Link>
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

export default function ConnectionsPage() {
  usePageTitle('Connections')
  const { user } = useAuth()
  const isAdmin = user?.role === 'admin'
  const [sonarServers, setSonarServers] = useState([])
  const [sonarServersError, setSonarServersError] = useState(null)
  const [githubCredentials, setGithubCredentials] = useState([])
  const [githubCredentialsError, setGithubCredentialsError] = useState(null)
  const [llmConfigs, setLlmConfigs] = useState([])
  const [llmConfigsError, setLlmConfigsError] = useState(null)
  const sonarPagination = usePagination(sonarServers, PAGE_SIZE)
  const githubPagination = usePagination(githubCredentials, PAGE_SIZE)
  const llmPagination = usePagination(llmConfigs, PAGE_SIZE)

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

  async function refreshLlmConfigs() {
    try {
      setLlmConfigs(await api.listLlmConfigs())
      setLlmConfigsError(null)
    } catch (err) {
      setLlmConfigsError(err.message)
    }
  }

  useEffect(() => {
    refreshSonarServers()
    refreshGithubCredentials()
    if (isAdmin) refreshLlmConfigs()
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [isAdmin])

  return (
    <div className="connections-page">
      {isAdmin && (
        <section className="connections-section">
          <div className="page-head-row">
            <h2>
              <SparkleIcon /> LLM API Keys
            </h2>
            <Link
              to="/connections/llm-configs/new"
              className="button-link icon-button-link"
              aria-label="Add LLM API Key"
              title="Add LLM API Key"
            >
              <PlusIcon size={18} />
            </Link>
          </div>
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
                <LlmConfigRow key={c.id} config={c} onChanged={refreshLlmConfigs} />
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
      )}

      <div className="connections-grid">
        <section className="connections-section">
          <div className="page-head-row">
            <h2>
              <SonarIcon /> SonarQube Servers
            </h2>
            <Link
              to="/connections/sonar-servers/new"
              className="button-link icon-button-link"
              aria-label="Add Sonar Server"
              title="Add Sonar Server"
            >
              <PlusIcon size={18} />
            </Link>
          </div>
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
                <SonarServerRow key={s.id} server={s} showOwner={isAdmin} onChanged={refreshSonarServers} />
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

        <section className="connections-section">
          <div className="page-head-row">
            <h2>
              <GitBranchIcon /> GitHub Credentials
            </h2>
            <Link
              to="/connections/github-credentials/new"
              className="button-link icon-button-link"
              aria-label="Add GitHub Credential"
              title="Add GitHub Credential"
            >
              <PlusIcon size={18} />
            </Link>
          </div>
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
                <GithubCredentialRow key={c.id} credential={c} showOwner={isAdmin} onChanged={refreshGithubCredentials} />
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
    </div>
  )
}
