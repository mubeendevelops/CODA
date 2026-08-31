import { API_BASE_URL } from '../api/config'
import { clearTokens, getRefreshToken, setTokens } from './tokenStore'

// Shared by api/client.ts (REST 401 retry) and lib/sse.ts (the SSE job-
// events stream can run for up to 30 minutes — docs/architecture.md §2.4's
// sseMaxDuration — far longer than the 15-minute access token TTL, so it
// needs the same refresh-on-401 the REST client gets). One in-flight
// promise either way, so two callers racing a refresh don't both rotate
// the refresh token and step on each other.
let refreshInFlight: Promise<boolean> | null = null

export async function refreshAccessToken(): Promise<boolean> {
  const refreshToken = getRefreshToken()
  if (!refreshToken) return false

  if (!refreshInFlight) {
    refreshInFlight = (async () => {
      try {
        const res = await fetch(`${API_BASE_URL}/auth/refresh`, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ refresh_token: refreshToken }),
        })
        if (!res.ok) {
          clearTokens()
          return false
        }
        const body = (await res.json()) as { access_token: string; refresh_token: string }
        setTokens({ accessToken: body.access_token, refreshToken: body.refresh_token })
        return true
      } catch {
        clearTokens()
        return false
      } finally {
        refreshInFlight = null
      }
    })()
  }
  return refreshInFlight
}
