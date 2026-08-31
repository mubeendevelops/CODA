import { Navigate, Route, BrowserRouter as Router, Routes } from 'react-router-dom'

import './App.css'
import { AuthProvider } from './auth/AuthContext'
import { useAuth } from './auth/useAuth'
import { Layout } from './components/Layout'
import { ProtectedRoute } from './components/ProtectedRoute'
import { ConsultationsListPage } from './pages/ConsultationsListPage'
import { JobStatusPage } from './pages/JobStatusPage'
import { LoginPage } from './pages/LoginPage'
import { NewConsultationPage } from './pages/NewConsultationPage'
import { ResultPage } from './pages/ResultPage'
import { UploadPage } from './pages/UploadPage'

function AppRoutes() {
  const { isAuthenticated } = useAuth()

  return (
    <Routes>
      <Route
        path="/login"
        element={isAuthenticated ? <Navigate to="/" replace /> : <LoginPage />}
      />
      <Route
        path="/"
        element={
          <ProtectedRoute>
            <Layout>
              <ConsultationsListPage />
            </Layout>
          </ProtectedRoute>
        }
      />
      <Route
        path="/consultations/new"
        element={
          <ProtectedRoute>
            <Layout>
              <NewConsultationPage />
            </Layout>
          </ProtectedRoute>
        }
      />
      <Route
        path="/consultations/:consultationId/upload"
        element={
          <ProtectedRoute>
            <Layout>
              <UploadPage />
            </Layout>
          </ProtectedRoute>
        }
      />
      <Route
        path="/consultations/:consultationId/jobs/:jobId"
        element={
          <ProtectedRoute>
            <Layout>
              <JobStatusPage />
            </Layout>
          </ProtectedRoute>
        }
      />
      <Route
        path="/consultations/:consultationId/result"
        element={
          <ProtectedRoute>
            <Layout>
              <ResultPage />
            </Layout>
          </ProtectedRoute>
        }
      />
      <Route path="*" element={<Navigate to="/" replace />} />
    </Routes>
  )
}

function App() {
  return (
    <Router>
      <AuthProvider>
        <AppRoutes />
      </AuthProvider>
    </Router>
  )
}

export default App
