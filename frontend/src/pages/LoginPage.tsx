import { useState, type FormEvent } from 'react'
import { useLocation, useNavigate } from 'react-router-dom'

import { useAuth } from '../auth/useAuth'
import { ErrorMessage } from '../components/QueryState'

export function LoginPage() {
  const { login } = useAuth()
  const navigate = useNavigate()
  const location = useLocation()
  const [email, setEmail] = useState('')
  const [password, setPassword] = useState('')
  const [error, setError] = useState<string | null>(null)
  const [submitting, setSubmitting] = useState(false)

  const from = (location.state as { from?: Location } | null)?.from?.pathname ?? '/'

  const handleSubmit = async (e: FormEvent) => {
    e.preventDefault()
    setError(null)
    setSubmitting(true)
    const result = await login(email, password)
    setSubmitting(false)
    if (result.ok === false) {
      setError(result.error)
      return
    }
    navigate(from, { replace: true })
  }

  return (
    <main style={{ maxWidth: 360, margin: '4rem auto', padding: '0 1rem' }}>
      <h1>CODA</h1>
      <form onSubmit={handleSubmit}>
        <div>
          <label htmlFor="email">Email</label>
          <br />
          <input
            id="email"
            type="email"
            required
            autoComplete="username"
            value={email}
            onChange={(e) => setEmail(e.target.value)}
          />
        </div>
        <div style={{ marginTop: '0.5rem' }}>
          <label htmlFor="password">Password</label>
          <br />
          <input
            id="password"
            type="password"
            required
            autoComplete="current-password"
            value={password}
            onChange={(e) => setPassword(e.target.value)}
          />
        </div>
        {error && (
          <div style={{ marginTop: '0.5rem' }}>
            <ErrorMessage message={error} />
          </div>
        )}
        <button type="submit" disabled={submitting} style={{ marginTop: '1rem' }}>
          {submitting ? 'Logging in…' : 'Log in'}
        </button>
      </form>
    </main>
  )
}
