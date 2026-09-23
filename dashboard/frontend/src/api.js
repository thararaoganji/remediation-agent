const BASE = '/api'

// A 401 anywhere (session missing/expired/revoked) sends the user back to
// login instead of surfacing a raw "Not authenticated" error in whatever
// page happened to be open -- except from the login/me endpoints
// themselves, where a 401 is an expected, handled response (wrong
// password, or the initial "am I logged in?" check on app load), not a
// stale-session situation to redirect out of.
function isAuthCheckPath(path) {
  return path === '/auth/login' || path === '/auth/me'
}

async function request(path, options = {}) {
  const res = await fetch(`${BASE}${path}`, {
    headers: { 'Content-Type': 'application/json', ...(options.headers || {}) },
    ...options,
  })
  if (res.status === 401 && !isAuthCheckPath(path)) {
    window.location.href = '/login'
    return new Promise(() => {}) // navigation is in flight; don't resolve into stale-session code paths
  }
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
  login: (email, password) => request('/auth/login', { method: 'POST', body: JSON.stringify({ email, password }) }),
  logout: () => request('/auth/logout', { method: 'POST' }),
  getCurrentUser: () => request('/auth/me'),

  listUsers: () => request('/users'),
  createUser: (data) => request('/users', { method: 'POST', body: JSON.stringify(data) }),
  deleteUser: (email) => request(`/users/${encodeURIComponent(email)}`, { method: 'DELETE' }),

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
