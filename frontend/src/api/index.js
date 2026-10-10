/**
 * Thin JSON API client for the Django/DRF backend.
 * Base URL comes from VITE_API_BASE_URL (see .env.example).
 * 409 responses (stock shortfall) are thrown as ApiError with `.data`
 * so views can render the Proceed/Cancel banner (spec 4.2 step 4).
 */

const configuredBaseUrl = import.meta.env.VITE_API_BASE_URL

/**
 * Vite inlines VITE_* values at build time and `frontend/.env` only holds the
 * localhost development default (git-ignored), so a production build knows the
 * backend address *only* when VITE_API_BASE_URL is set in the environment that
 * runs the build — GitHub Actions, see .github/workflows/deploy.yml. Without it
 * the bundle would quietly point at localhost and every screen would look
 * broken — so flag it loudly (console + the banner in App.vue) instead.
 */
export const apiBaseUrlMissing = import.meta.env.PROD && !configuredBaseUrl

if (apiBaseUrlMissing) {
  // eslint-disable-next-line no-console
  console.error(
    '[jsd] VITE_API_BASE_URL is not set for this production build, so the app ' +
      'is using http://localhost:8000. Set it to the Render backend URL ' +
      '(e.g. https://<your-app>.onrender.com) in the GitHub repo under ' +
      'Settings → Secrets and variables → Actions, then re-run the deploy ' +
      'workflow.'
  )
}

const BASE = (configuredBaseUrl || 'http://localhost:8000').replace(/\/+$/, '')

export class ApiError extends Error {
  constructor(status, data) {
    super(data?.detail || `HTTP ${status}`)
    this.status = status
    this.data = data
  }
}

async function request(path, { method = 'GET', body, params } = {}) {
  let url = `${BASE}/api${path}`
  if (params) {
    const qs = new URLSearchParams()
    Object.entries(params).forEach(([k, v]) => {
      if (v !== undefined && v !== null && v !== '') qs.append(k, v)
    })
    const s = qs.toString()
    if (s) url += `?${s}`
  }
  const opts = { method, headers: { Accept: 'application/json' } }
  if (body !== undefined) {
    opts.headers['Content-Type'] = 'application/json'
    opts.body = JSON.stringify(body)
  }
  let res
  try {
    res = await fetch(url, opts)
  } catch (e) {
    throw new ApiError(0, { detail: 'Network error — is the API reachable?' })
  }
  let data = null
  try {
    data = await res.json()
  } catch {
    data = null
  }
  if (!res.ok) throw new ApiError(res.status, data)
  return data
}

export const api = {
  get: (path, params) => request(path, { params }),
  post: (path, body) => request(path, { method: 'POST', body }),
  put: (path, body) => request(path, { method: 'PUT', body }),
  patch: (path, body) => request(path, { method: 'PATCH', body }),
  del: (path) => request(path, { method: 'DELETE' }),
}

/** DRF paginated list -> plain array (handles {results} payloads). */
export function listify(data) {
  if (Array.isArray(data)) return data
  return data?.results ?? []
}
