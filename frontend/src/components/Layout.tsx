import type { ReactNode } from 'react'
import { Link, useNavigate } from 'react-router-dom'

import { useAuth } from '../auth/useAuth'

export function Layout({ children }: { children: ReactNode }) {
  const { logout } = useAuth()
  const navigate = useNavigate()

  const handleLogout = async () => {
    await logout()
    navigate('/login', { replace: true })
  }

  return (
    <div>
      <header
        style={{
          display: 'flex',
          justifyContent: 'space-between',
          alignItems: 'center',
          padding: '0.75rem 1rem',
          borderBottom: '1px solid #ccc',
        }}
      >
        <Link to="/" style={{ fontWeight: 'bold', textDecoration: 'none', color: 'inherit' }}>
          CODA
        </Link>
        <button type="button" onClick={handleLogout}>
          Log out
        </button>
      </header>
      <main style={{ padding: '1rem' }}>{children}</main>
    </div>
  )
}
