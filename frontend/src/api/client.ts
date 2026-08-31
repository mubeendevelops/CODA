import createClient from 'openapi-fetch'

import { refreshAccessToken } from '../auth/refresh'
import { getAccessToken } from '../auth/tokenStore'
import { API_BASE_URL } from './config'
import type { paths } from './schema'

export { API_BASE_URL }

// Paths that must never carry a (possibly stale) Authorization header and
// must never trigger a refresh-on-401 retry — refresh/login/logout ARE the
// auth flow, per openapi/coda-v1.yaml's `security: []` on each.
const UNAUTHENTICATED_PATHS = ['/auth/login', '/auth/refresh', '/auth/logout']

// A custom fetch (rather than openapi-fetch middleware) so a 401 can
// actually retry the original request once with a refreshed token —
// openapi-fetch's onResponse hook can inspect/transform a response but
// can't cleanly re-issue the request itself.
async function authenticatedFetch(input: RequestInfo | URL, init?: RequestInit): Promise<Response> {
  const url = typeof input === 'string' ? input : input.toString()
  const isUnauthenticatedPath = UNAUTHENTICATED_PATHS.some((p) => url.includes(p))

  const withAuth = (headers?: HeadersInit): HeadersInit => {
    if (isUnauthenticatedPath) return headers ?? {}
    const token = getAccessToken()
    if (!token) return headers ?? {}
    return { ...(headers as Record<string, string> | undefined), Authorization: `Bearer ${token}` }
  }

  const first = await fetch(input, { ...init, headers: withAuth(init?.headers) })
  if (first.status !== 401 || isUnauthenticatedPath) return first

  const refreshed = await refreshAccessToken()
  if (!refreshed) return first

  return fetch(input, { ...init, headers: withAuth(init?.headers) })
}

export const api = createClient<paths>({ baseUrl: API_BASE_URL, fetch: authenticatedFetch })
