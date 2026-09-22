const BASE = '/api'

async function request(path, options = {}) {
  const res = await fetch(`${BASE}${path}`, {
    headers: { 'Content-Type': 'application/json', ...(options.headers || {}) },
    ...options,
  })
  if (!res.ok) {
    let detail = res.statusText
    try {
      const body = await res.json()
      detail = body.detail || JSON.stringify(body)
    } catch {
      // response body wasn't JSON -- keep statusText
    }
    throw new Error(detail)
  }
  if (res.status === 204) return null
  return res.json()
}

export const api = {
  listSonarServers: () => request('/sonar-servers'),
  createSonarServer: (data) => request('/sonar-servers', { method: 'POST', body: JSON.stringify(data) }),
  updateSonarServer: (id, data) => request(`/sonar-servers/${id}`, { method: 'PUT', body: JSON.stringify(data) }),
  deleteSonarServer: (id) => request(`/sonar-servers/${id}`, { method: 'DELETE' }),

  listGithubCredentials: () => request('/github-credentials'),
  createGithubCredential: (data) => request('/github-credentials', { method: 'POST', body: JSON.stringify(data) }),
  updateGithubCredential: (id, data) => request(`/github-credentials/${id}`, { method: 'PUT', body: JSON.stringify(data) }),
  deleteGithubCredential: (id) => request(`/github-credentials/${id}`, { method: 'DELETE' }),

  getGoogleApiKeyStatus: () => request('/google-api-key'),
  setGoogleApiKey: (value) => request('/google-api-key', { method: 'POST', body: JSON.stringify({ value }) }),

  listRuns: () => request('/runs'),
  getRun: (id) => request(`/runs/${id}`),
  createRun: (data) => request('/runs', { method: 'POST', body: JSON.stringify(data) }),
}
