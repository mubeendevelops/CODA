// Token persistence. localStorage is the only client-side storage go-api's
// JSON-body token response leaves available (no httpOnly cookie mechanism
// exists server-side) — see claude_context.md's Phase 8 decisions for why
// this is a deliberate, documented tradeoff, not an oversight.

const ACCESS_TOKEN_KEY = 'coda.access_token'
const REFRESH_TOKEN_KEY = 'coda.refresh_token'

export interface TokenPair {
  accessToken: string
  refreshToken: string
}

export function getAccessToken(): string | null {
  return localStorage.getItem(ACCESS_TOKEN_KEY)
}

export function getRefreshToken(): string | null {
  return localStorage.getItem(REFRESH_TOKEN_KEY)
}

export function setTokens(tokens: TokenPair): void {
  localStorage.setItem(ACCESS_TOKEN_KEY, tokens.accessToken)
  localStorage.setItem(REFRESH_TOKEN_KEY, tokens.refreshToken)
  window.dispatchEvent(new Event('coda:auth-changed'))
}

export function clearTokens(): void {
  localStorage.removeItem(ACCESS_TOKEN_KEY)
  localStorage.removeItem(REFRESH_TOKEN_KEY)
  window.dispatchEvent(new Event('coda:auth-changed'))
}

export function isAuthenticated(): boolean {
  return getAccessToken() !== null
}
