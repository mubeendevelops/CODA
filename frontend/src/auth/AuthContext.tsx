import { useCallback, useEffect, useState, type ReactNode } from 'react'

import { api } from '../api/client'
import { AuthContext, type LoginResult } from './context'
import { clearTokens, getRefreshToken, isAuthenticated, setTokens } from './tokenStore'

export function AuthProvider({ children }: { children: ReactNode }) {
  const [authed, setAuthed] = useState(isAuthenticated())

  useEffect(() => {
    const onChange = () => setAuthed(isAuthenticated())
    window.addEventListener('coda:auth-changed', onChange)
    return () => window.removeEventListener('coda:auth-changed', onChange)
  }, [])

  const login = useCallback(async (email: string, password: string): Promise<LoginResult> => {
    const { data, error } = await api.POST('/auth/login', {
      body: { email, password },
    })
    if (error || !data) {
      const message =
        typeof error === 'object' && error !== null && 'error' in error
          ? String((error as { error: unknown }).error)
          : 'Login failed'
      return { ok: false as const, error: message }
    }
    setTokens({ accessToken: data.access_token ?? '', refreshToken: data.refresh_token ?? '' })
    return { ok: true as const }
  }, [])

  const logout = useCallback(async () => {
    const refreshToken = getRefreshToken()
    if (refreshToken) {
      try {
        await api.POST('/auth/logout', { body: { refresh_token: refreshToken } })
      } catch {
        // Logout is best-effort client-side regardless — clear local state
        // even if the network call itself fails.
      }
    }
    clearTokens()
  }, [])

  return (
    <AuthContext.Provider value={{ isAuthenticated: authed, login, logout }}>
      {children}
    </AuthContext.Provider>
  )
}
