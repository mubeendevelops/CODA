export function formatMs(ms: number | undefined): string {
  if (ms == null) return ''
  const totalSeconds = Math.floor(ms / 1000)
  const minutes = Math.floor(totalSeconds / 60)
  const seconds = totalSeconds % 60
  return `${minutes}:${seconds.toString().padStart(2, '0')}`
}

const SPEAKER_LABELS: Record<string, string> = {
  SPEAKER_ROLE_DOCTOR: 'Doctor',
  SPEAKER_ROLE_PATIENT: 'Patient',
  SPEAKER_ROLE_UNKNOWN: 'Unknown',
  SPEAKER_ROLE_UNSPECIFIED: 'Unspecified',
}

export function formatSpeaker(label: string | undefined): string {
  if (!label) return 'Unspecified'
  return SPEAKER_LABELS[label] ?? label
}
