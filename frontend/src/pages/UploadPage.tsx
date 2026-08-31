import { useState } from 'react'
import { useNavigate, useParams } from 'react-router-dom'

import { api } from '../api/client'
import { unwrap } from '../api/helpers'
import { ErrorMessage } from '../components/QueryState'
import {
  audioDurationSeconds,
  resolveContentType,
  sha256Hex,
  uploadWithProgress,
} from '../lib/audio'

type Stage =
  'idle' | 'hashing' | 'presigning' | 'uploading' | 'confirming' | 'starting-job' | 'done' | 'error'

const STAGE_LABEL: Record<Stage, string> = {
  idle: '',
  hashing: 'Hashing file…',
  presigning: 'Requesting upload URL…',
  uploading: 'Uploading…',
  confirming: 'Confirming upload…',
  'starting-job': 'Starting processing…',
  done: 'Done',
  error: 'Error',
}

export function UploadPage() {
  const { consultationId } = useParams<{ consultationId: string }>()
  const navigate = useNavigate()
  const [stage, setStage] = useState<Stage>('idle')
  const [progress, setProgress] = useState(0)
  const [error, setError] = useState<string | null>(null)

  const handleFileChange = async (file: File | null) => {
    if (!file || !consultationId) return
    setError(null)

    const contentType = resolveContentType(file)
    if (!contentType) {
      setStage('error')
      setError('Unsupported file type. Only WAV and MP3 are accepted.')
      return
    }

    try {
      setStage('hashing')
      const [sha256, durationSec] = await Promise.all([sha256Hex(file), audioDurationSeconds(file)])

      setStage('presigning')
      const presign = unwrap(
        await api.POST('/consultations/{id}/audio/presign', {
          params: { path: { id: consultationId } },
          body: { content_type: contentType, size_bytes: file.size, sha256 },
        }),
        'Failed to get an upload URL',
      )

      setStage('uploading')
      setProgress(0)
      await uploadWithProgress(presign.upload_url!, file, setProgress)

      setStage('confirming')
      unwrap(
        await api.POST('/consultations/{id}/audio/confirm', {
          params: { path: { id: consultationId } },
          body: { object_key: presign.object_key!, sha256, duration_sec: durationSec },
        }),
        'Failed to confirm the upload',
      )

      setStage('starting-job')
      const job = unwrap(
        await api.POST('/consultations/{id}/jobs', {
          params: { path: { id: consultationId } },
          body: { arm: 'baseline' },
        }),
        'Failed to start processing',
      )

      setStage('done')
      navigate(`/consultations/${consultationId}/jobs/${job.id}`)
    } catch (err) {
      setStage('error')
      setError(err instanceof Error ? err.message : 'Upload failed')
    }
  }

  if (!consultationId) return <ErrorMessage message="Missing consultation id" />

  return (
    <div style={{ maxWidth: 480 }}>
      <h1>Upload audio</h1>
      <p>Accepted formats: WAV, MP3.</p>

      <input
        type="file"
        accept=".wav,.mp3,audio/wav,audio/mpeg"
        disabled={stage !== 'idle' && stage !== 'error' && stage !== 'done'}
        onChange={(e) => handleFileChange(e.target.files?.[0] ?? null)}
      />

      {stage !== 'idle' && (
        <div style={{ marginTop: '1rem' }}>
          <p role="status" aria-live="polite">
            {STAGE_LABEL[stage]}
            {stage === 'uploading' && ` ${progress}%`}
          </p>
          {stage === 'uploading' && (
            <progress value={progress} max={100} style={{ width: '100%' }} />
          )}
        </div>
      )}

      {error && (
        <div style={{ marginTop: '0.5rem' }}>
          <ErrorMessage message={error} />
        </div>
      )}
    </div>
  )
}
