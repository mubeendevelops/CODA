import { createContext } from 'react'

export type LoginResult = { ok: true } | { ok: false; error: string }

export interface AuthContextValue {
  isAuthenticated: boolean
  login: (email: string, password: string) => Promise<LoginResult>
  logout: () => Promise<void>
}

export const AuthContext = createContext<AuthContextValue | null>(null)
