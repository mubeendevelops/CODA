import { Link } from 'react-router-dom'

import { useConsultations } from '../api/queries'
import { EmptyState, ErrorMessage, Loading } from '../components/QueryState'

const RESULT_READY_STATES = new Set(['awaiting_review', 'under_review', 'approved', 'exported'])

function actionFor(consultation: { id: string; state: string }) {
  if (consultation.state === 'consent_recorded') {
    return <Link to={`/consultations/${consultation.id}/upload`}>Upload audio</Link>
  }
  if (RESULT_READY_STATES.has(consultation.state)) {
    return <Link to={`/consultations/${consultation.id}/result`}>View result</Link>
  }
  // No action: intermediate pipeline states (asr_queued, nlp_running, ...)
  // have no way to recover a job id from the consultation alone once you've
  // left the page that created the job — see claude_context.md's Phase 8
  // decisions for why that's a real, documented API-surface gap, not an
  // oversight here.
  return <span style={{ color: '#666' }}>Processing — reopen from the page that started it</span>
}

export function ConsultationsListPage() {
  const { data, isLoading, isError, error } = useConsultations()

  return (
    <div>
      <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center' }}>
        <h1>Consultations</h1>
        <Link to="/consultations/new">
          <button type="button">New consultation</button>
        </Link>
      </div>

      {isLoading && <Loading label="Loading consultations…" />}
      {isError && (
        <ErrorMessage
          message={error instanceof Error ? error.message : 'Failed to load consultations'}
        />
      )}
      {!isLoading && !isError && data && data.length === 0 && (
        <EmptyState message="No consultations yet. Create one to get started." />
      )}
      {!isLoading && !isError && data && data.length > 0 && (
        <table style={{ width: '100%', borderCollapse: 'collapse', marginTop: '1rem' }}>
          <thead>
            <tr style={{ textAlign: 'left', borderBottom: '1px solid #ccc' }}>
              <th>ID</th>
              <th>Language</th>
              <th>State</th>
              <th></th>
            </tr>
          </thead>
          <tbody>
            {data.map((c) => (
              <tr key={c.id} style={{ borderBottom: '1px solid #eee' }}>
                <td>{c.id?.slice(0, 8)}</td>
                <td>{c.language}</td>
                <td>{c.state}</td>
                <td>{c.id && c.state && actionFor({ id: c.id, state: c.state })}</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </div>
  )
}
