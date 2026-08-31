/** Extracts a readable message from an openapi-fetch error body, which is
 * always `{ error: string }` per openapi/coda-v1.yaml's Error schema — but
 * typed as `unknown` by the generated client until narrowed. */
export function apiErrorMessage(error: unknown, fallback: string): string {
  if (typeof error === 'object' && error !== null && 'error' in error) {
    const value = (error as { error: unknown }).error
    if (typeof value === 'string' && value.length > 0) return value
  }
  return fallback
}
