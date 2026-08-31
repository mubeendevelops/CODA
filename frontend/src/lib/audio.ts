/** sha256 hex digest of a File's bytes — required by both the presign and
 * confirm requests (openapi/coda-v1.yaml's AudioPresignRequest/
 * AudioConfirmRequest). Computed client-side via Web Crypto so the server
 * never has to trust an unverifiable claim. */
export async function sha256Hex(file: File): Promise<string> {
  const buffer = await file.arrayBuffer()
  const digest = await crypto.subtle.digest('SHA-256', buffer)
  return Array.from(new Uint8Array(digest))
    .map((b) => b.toString(16).padStart(2, '0'))
    .join('')
}

/** Audio duration in seconds, read via a real decode of the file's
 * metadata (HTMLAudioElement), not guessed from file size/bitrate. */
export function audioDurationSeconds(file: File): Promise<number> {
  return new Promise((resolve, reject) => {
    const url = URL.createObjectURL(file)
    const audio = new Audio()
    audio.preload = 'metadata'
    audio.onloadedmetadata = () => {
      URL.revokeObjectURL(url)
      resolve(audio.duration)
    }
    audio.onerror = () => {
      URL.revokeObjectURL(url)
      reject(new Error('Could not read audio duration — the file may not be a valid WAV/MP3.'))
    }
    audio.src = url
  })
}

export type AllowedAudioContentType =
  'audio/wav' | 'audio/x-wav' | 'audio/wave' | 'audio/mpeg' | 'audio/mp3'

const ALLOWED_CONTENT_TYPES: readonly AllowedAudioContentType[] = [
  'audio/wav',
  'audio/x-wav',
  'audio/wave',
  'audio/mpeg',
  'audio/mp3',
]

const CONTENT_TYPE_BY_EXTENSION: Record<string, AllowedAudioContentType> = {
  wav: 'audio/wav',
  mp3: 'audio/mpeg',
}

/** The server only accepts a fixed set of content types (AudioPresignRequest's
 * enum); browsers don't always set File.type reliably for .wav, so fall back
 * to the extension. */
export function resolveContentType(file: File): AllowedAudioContentType | null {
  if ((ALLOWED_CONTENT_TYPES as readonly string[]).includes(file.type)) {
    return file.type as AllowedAudioContentType
  }
  const ext = file.name.split('.').pop()?.toLowerCase()
  return ext ? (CONTENT_TYPE_BY_EXTENSION[ext] ?? null) : null
}

/** PUTs a file to a presigned URL via XMLHttpRequest (not fetch — fetch
 * has no upload-progress event) and reports real progress. */
export function uploadWithProgress(
  url: string,
  file: File,
  onProgress: (percent: number) => void,
): Promise<void> {
  return new Promise((resolve, reject) => {
    const xhr = new XMLHttpRequest()
    xhr.open('PUT', url)
    xhr.upload.onprogress = (event) => {
      if (event.lengthComputable) {
        onProgress(Math.round((event.loaded / event.total) * 100))
      }
    }
    xhr.onload = () => {
      if (xhr.status >= 200 && xhr.status < 300) {
        resolve()
      } else {
        reject(new Error(`Upload failed (HTTP ${xhr.status})`))
      }
    }
    xhr.onerror = () => reject(new Error('Upload failed — network error'))
    xhr.send(file)
  })
}
