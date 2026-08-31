import { useEffect } from 'react'
import { useNavigate, useParams } from 'react-router-dom'

import { isJobTerminal, useJobEvents } from '../api/useJobEvents'
import { ErrorMessage, Loading } from '../components/QueryState'

const RESULT_READY_STATES = new Set(['awaiting_review', 'under_review', 'approved', 'exported'])

const STAGE_ORDER = ['asr', 'redact', 'nlp', 'export']

export function JobStatusPage() {
  const { consultationId, jobId } = useParams<{ consultationId: string; jobId: string }>()
  const navigate = useNavigate()
  const { job, connectionError } = useJobEvents(jobId)

  useEffect(() => {
    if (job?.state && RESULT_READY_STATES.has(job.state) && consultationId) {
      navigate(`/consultations/${consultationId}/result`, { replace: true })
    }
  }, [job?.state, consultationId, navigate])

  if (!jobId || !consultationId) return <ErrorMessage message="Missing job or consultation id" />

  return (
    <div style={{ maxWidth: 640 }}>
      <h1>Processing</h1>

      {connectionError && <ErrorMessage message={connectionError} />}
      {!job && !connectionError && <Loading label="Connecting to job status stream…" />}

      {job && (
        <div>
          <p>
            State: <strong>{job.state}</strong>
            {job.state && isJobTerminal(job.state) && !RESULT_READY_STATES.has(job.state) && (
              <span style={{ color: '#b00020' }}> — did not complete successfully</span>
            )}
          </p>
          {job.error != null && (
            <ErrorMessage
              message={
                typeof job.error === 'object' ? JSON.stringify(job.error) : String(job.error)
              }
            />
          )}

          <table style={{ width: '100%', borderCollapse: 'collapse', marginTop: '1rem' }}>
            <thead>
              <tr style={{ textAlign: 'left', borderBottom: '1px solid #ccc' }}>
                <th>Stage</th>
                <th>Status</th>
                <th>Progress</th>
                <th>Step</th>
              </tr>
            </thead>
            <tbody>
              {STAGE_ORDER.map((stageName) => {
                const stage = job.stages?.find((s) => s.stage === stageName)
                return (
                  <tr key={stageName} style={{ borderBottom: '1px solid #eee' }}>
                    <td>{stageName}</td>
                    <td>{stage?.status ?? 'pending'}</td>
                    <td>
                      {stage?.percent_complete != null
                        ? `${Math.round(stage.percent_complete)}%`
                        : ''}
                    </td>
                    <td>{stage?.step ?? ''}</td>
                  </tr>
                )
              })}
            </tbody>
          </table>

          <p style={{ marginTop: '1rem' }}>Overall: {job.progress_percent ?? 0}%</p>
        </div>
      )}
    </div>
  )
}
