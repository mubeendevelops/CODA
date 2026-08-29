package auth

import "context"

type contextKey int

const claimsContextKey contextKey = iota

// WithClaims returns a context carrying claims, for the Authenticate
// middleware to stash the verified identity of the current request.
func WithClaims(ctx context.Context, claims *Claims) context.Context {
	return context.WithValue(ctx, claimsContextKey, claims)
}

// ClaimsFromContext returns the authenticated request's claims, and false
// if the request never passed through Authenticate (or is anonymous).
func ClaimsFromContext(ctx context.Context) (*Claims, bool) {
	claims, ok := ctx.Value(claimsContextKey).(*Claims)
	return claims, ok
}
