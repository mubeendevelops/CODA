import type { components } from '../api/schema'
import { formatMs, formatSpeaker } from '../lib/format'

type Turn = components['schemas']['Turn']

export function TranscriptPanel({ turns }: { turns: Turn[] }) {
  if (turns.length === 0) {
    return <p>No transcript turns.</p>
  }

  return (
    <div style={{ maxHeight: 480, overflowY: 'auto', border: '1px solid #ccc', padding: '0.5rem' }}>
      {turns.map((turn) => (
        <div key={turn.turn_index} style={{ marginBottom: '0.75rem' }}>
          <div style={{ fontSize: '0.85em', color: '#555' }}>
            <strong>{formatSpeaker(turn.speaker_label)}</strong> · {formatMs(turn.start_ms)}–
            {formatMs(turn.end_ms)}
          </div>
          <div>{turn.text_redacted || turn.text || <em>(no text)</em>}</div>
        </div>
      ))}
    </div>
  )
}
