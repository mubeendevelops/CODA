// Package db owns the pgx connection pool and re-exports the sqlc-generated
// query layer (internal/db/sqlc) against the schema in docs/architecture.md
// §5. Migrations live in go/migrations, applied via golang-migrate — see
// Makefile targets `migrate` and `migrate-test`.
package db
