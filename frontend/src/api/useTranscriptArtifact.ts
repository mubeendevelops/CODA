import { useQuery } from '@tanstack/react-query'

import { useTranscriptPresign } from './queries'
import type { components } from './schema'

type TranscriptArtifact = components['schemas']['TranscriptArtifact']

/** Fetches the transcript artifact's actual JSON content directly from
 * MinIO via the presigned URL — go-api never returns turn content itself
 * (docs/architecture.md §1.2, claude_context.md's Phase 8 decisions on why
 * this is a presigned-GET, not a server-side proxy). This is a plain
 * fetch, not the api client: the presigned URL's own query-string
 * signature is the only auth it needs or accepts. */
export function useTranscriptArtifact(consultationId: string | undefined, enabled: boolean) {
  const presign = useTranscriptPresign(consultationId, enabled)

  return useQuery({
    queryKey: ['consultations', consultationId, 'transcript-artifact', presign.data?.download_url],
    enabled: enabled && presign.data?.download_url !== undefined,
    retry: false,
    queryFn: async (): Promise<TranscriptArtifact> => {
      const res = await fetch(presign.data!.download_url!)
      if (!res.ok) {
        throw new Error(`Failed to fetch transcript (HTTP ${res.status})`)
      }
      return (await res.json()) as TranscriptArtifact
    },
  })
}
