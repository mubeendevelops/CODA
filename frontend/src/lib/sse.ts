// A hand-rolled SSE reader over fetch, not the native EventSource API.
// EventSource cannot send custom headers, and GET /jobs/{id}/events requires
// a bearer token (auth.Authenticate applies to the whole /v1 group, this
// route included) — so the browser's built-in SSE client is unusable here
// without a query-string-token workaround on the server side, which
// openapi/coda-v1.yaml's actual route doesn't support. fetch + a manual
// frame parser needs no server change.

import { refreshAccessToken } from '../auth/refresh'
import { getAccessToken } from '../auth/tokenStore'

export interface SseFrame {
  event: string | null
  data: string
}

/** Parses one `\n\n`-delimited SSE frame (already split off the stream) into
 * its event name (default "message" per the SSE spec) and data payload.
 * A frame that is only a comment (docs/architecture.md §2.4's `: heartbeat`
 * lines) has no `data:` line and is filtered out by the caller. */
function parseFrame(raw: string): SseFrame | null {
  let event: string | null = null
  const dataLines: string[] = []
  for (const line of raw.split('\n')) {
    if (line.startsWith(':')) continue // comment (heartbeat)
    if (line.startsWith('event:')) event = line.slice('event:'.length).trim()
    else if (line.startsWith('data:')) dataLines.push(line.slice('data:'.length).trim())
  }
  if (dataLines.length === 0) return null
  return { event, data: dataLines.join('\n') }
}

export interface JobEventsHandle {
  close: () => void
}

/** Connects to GET /jobs/{id}/events and calls `onFrame` for every real
 * (non-heartbeat) frame until the stream ends, the job reaches a terminal
 * state (the caller decides that from the parsed data), or `close()` is
 * called. Reconnects with backoff on a dropped connection, up to a bounded
 * number of attempts, so a transient network blip doesn't permanently stop
 * progress updates. A 401 specifically triggers a token refresh before
 * retrying (not just backoff) — sseMaxDuration is 30 minutes
 * (docs/architecture.md §2.4) but the access token TTL is 15, so a
 * long-running stage would otherwise always fail once, every time. */
export function connectJobEvents(
  url: string,
  onFrame: (frame: SseFrame) => void,
  onError: (message: string) => void,
): JobEventsHandle {
  const controller = new AbortController()
  let closed = false
  let attempt = 0
  const maxAttempts = 5

  async function run() {
    while (!closed && attempt < maxAttempts) {
      let cleanEnd = false
      try {
        const token = getAccessToken()
        const res = await fetch(url, {
          headers: token ? { Authorization: `Bearer ${token}` } : {},
          signal: controller.signal,
        })
        if (res.status === 401) {
          const refreshed = await refreshAccessToken()
          if (!refreshed) throw new Error('Session expired')
          attempt += 1
          continue // retry immediately with the refreshed token, no backoff needed
        }
        if (!res.ok || !res.body) {
          throw new Error(`SSE connection failed (HTTP ${res.status})`)
        }
        attempt = 0 // a successful connection resets the backoff counter

        const reader = res.body.getReader()
        const decoder = new TextDecoder()
        let buffer = ''

        for (;;) {
          const { done, value } = await reader.read()
          if (done) break
          buffer += decoder.decode(value, { stream: true })
          let sepIndex: number
          while ((sepIndex = buffer.indexOf('\n\n')) !== -1) {
            const raw = buffer.slice(0, sepIndex)
            buffer = buffer.slice(sepIndex + 2)
            const frame = parseFrame(raw)
            if (frame) onFrame(frame)
          }
        }
        cleanEnd = true
      } catch (err) {
        if (closed || controller.signal.aborted) return
        attempt += 1
        if (attempt >= maxAttempts) {
          onError(err instanceof Error ? err.message : 'Lost connection to job status stream')
          return
        }
        await new Promise((resolve) => setTimeout(resolve, Math.min(1000 * 2 ** attempt, 8000)))
        continue
      }

      if (cleanEnd) {
        if (closed || controller.signal.aborted) return
        // A clean stream end is NOT necessarily "the job finished" — the
        // server may have closed for an unrelated reason (found live: a
        // request-scoped timeout shorter than a real job's runtime cleanly
        // closed the stream mid-job, and the client used to treat that as
        // "done", freezing the UI forever on the last frame received). The
        // one reliable "really done" signal is the caller observing a
        // terminal job state in a received frame and calling close() itself
        // (useJobEvents' cleanup does this on unmount after navigating away)
        // — so `closed` above is the actual stop condition, not this branch.
        // Reconnect exactly like a dropped connection, with the same
        // backoff/attempt budget.
        attempt += 1
        if (attempt >= maxAttempts) {
          onError('Lost connection to job status stream')
          return
        }
        await new Promise((resolve) => setTimeout(resolve, Math.min(1000 * 2 ** attempt, 8000)))
      }
    }
  }

  void run()

  return {
    close: () => {
      closed = true
      controller.abort()
    },
  }
}
