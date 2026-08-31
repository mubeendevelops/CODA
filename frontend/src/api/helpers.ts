import { apiErrorMessage } from '../lib/apiError'

/** openapi-fetch returns `{ data, error }` rather than throwing — this
 * adapts that into throw-on-error so TanStack Query's built-in error state
 * (isError/error) works the normal way in every page, instead of every
 * call site re-checking `error` by hand. */
export function unwrap<T>(result: { data?: T; error?: unknown }, fallbackMessage: string): T {
  if (result.error !== undefined || result.data === undefined) {
    throw new Error(apiErrorMessage(result.error, fallbackMessage))
  }
  return result.data
}
