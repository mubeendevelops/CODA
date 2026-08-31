import { useEffect, useState } from 'react'

import { connectJobEvents } from '../lib/sse'
import { API_BASE_URL } from './config'
import type { components } from './schema'

type Job = components['schemas']['Job']

const TERMINAL_STATES = new Set(['approved', 'exported', 'failed', 'dead_lettered', 'cancelled'])

export function isJobTerminal(state: string | undefined): boolean {
  return state !== undefined && TERMINAL_STATES.has(state)
}

interface JobEventsState {
  job: Job | null
  connectionError: string | null
}

/** Subscribes to GET /jobs/{id}/events for the lifetime of the component
 * (or until the job reaches a terminal state) and returns the latest
 * snapshot. See lib/sse.ts for why this isn't the native EventSource. */
export function useJobEvents(jobId: string | undefined): JobEventsState {
  const [state, setState] = useState<JobEventsState>({ job: null, connectionError: null })

  useEffect(() => {
    if (!jobId) return

    const handle = connectJobEvents(
      `${API_BASE_URL}/jobs/${jobId}/events`,
      (frame) => {
        if (frame.event !== 'stage') return
        try {
          const job = JSON.parse(frame.data) as Job
          setState({ job, connectionError: null })
        } catch {
          // A malformed frame is a server-side bug, not something the user
          // can act on — drop it rather than surface noise, the next frame
          // (or the heartbeat-driven reconnect) will recover.
        }
      },
      (message) => setState((prev) => ({ ...prev, connectionError: message })),
    )

    return () => handle.close()
  }, [jobId])

  return state
}
