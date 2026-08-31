import { useParams } from 'react-router-dom'

import { useConsultationResult } from '../api/queries'
import { useTranscriptArtifact } from '../api/useTranscriptArtifact'
import { ClinicalNoteView } from '../components/ClinicalNoteView'
import { ErrorMessage, Loading } from '../components/QueryState'
import { TranscriptPanel } from '../components/TranscriptPanel'

export function ResultPage() {
  const { consultationId } = useParams<{ consultationId: string }>()
  const result = useConsultationResult(consultationId)
  const transcript = useTranscriptArtifact(consultationId, result.isSuccess)

  if (!consultationId) return <ErrorMessage message="Missing consultation id" />

  if (result.isLoading) return <Loading label="Loading result…" />
  if (result.isError) {
    return (
      <ErrorMessage
        message={result.error instanceof Error ? result.error.message : 'Result is not ready yet'}
      />
    )
  }
  if (!result.data) return null

  const { clinical_note, summary } = result.data

  return (
    <div
      style={{
        display: 'grid',
        gridTemplateColumns: '1fr 1fr',
        gap: '1.5rem',
        alignItems: 'start',
      }}
    >
      <section>
        <h2>Transcript</h2>
        {transcript.isLoading && <Loading label="Loading transcript…" />}
        {transcript.isError && (
          <ErrorMessage
            message={
              transcript.error instanceof Error
                ? transcript.error.message
                : 'Failed to load transcript'
            }
          />
        )}
        {transcript.data && <TranscriptPanel turns={transcript.data.turns ?? []} />}
      </section>

      <section>
        <h2>Clinical note</h2>
        {clinical_note ? (
          <ClinicalNoteView note={clinical_note} />
        ) : (
          <p>No clinical note available.</p>
        )}

        <h2 style={{ marginTop: '1.5rem' }}>Summary</h2>
        {summary?.text ? <p>{summary.text}</p> : <p>No summary available.</p>}
      </section>
    </div>
  )
}
