// Shared loading/empty/error primitives — deliberately unstyled beyond
// what's needed to be legible (this is a vertical slice, not a styled
// product — see plan.md Phase 8).

export function Loading({ label = 'Loading…' }: { label?: string }) {
  return (
    <p role="status" aria-live="polite">
      {label}
    </p>
  )
}

export function ErrorMessage({ message }: { message: string }) {
  return (
    <p role="alert" style={{ color: '#b00020' }}>
      {message}
    </p>
  )
}

export function EmptyState({ message }: { message: string }) {
  return <p>{message}</p>
}
