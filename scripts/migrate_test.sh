#!/usr/bin/env bash
# Runs the full migration set up -> down -> up against a disposable Postgres
# container, to catch anything that isn't actually reversible before it
# reaches a real environment.
#
# Uses --tmpfs for the data directory rather than a named volume or bind
# mount: this repo's working tree lives on an NTFS volume (fuseblk), which
# cannot report real Unix file ownership, and Postgres refuses to start on
# such a volume (see claude_context.md §11, and docker-compose.yml's
# postgres service for the same issue handled differently there). tmpfs
# sidesteps it entirely and suits a throwaway container: nothing here needs
# to survive the container's life.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
CONTAINER=coda-migrate-test-pg
PORT=15987
DSN="postgres://test:test@localhost:${PORT}/test?sslmode=disable"

cleanup() {
  docker rm -f "$CONTAINER" >/dev/null 2>&1 || true
}
trap cleanup EXIT

echo "==> starting throwaway postgres on :${PORT}"
docker rm -f "$CONTAINER" >/dev/null 2>&1 || true
docker run -d --name "$CONTAINER" \
  --tmpfs /var/lib/postgresql/data \
  -e POSTGRES_USER=test -e POSTGRES_PASSWORD=test -e POSTGRES_DB=test \
  -p "${PORT}:5432" postgres:16 >/dev/null

echo "==> waiting for postgres to accept connections"
for _ in $(seq 1 30); do
  if docker exec "$CONTAINER" pg_isready -U test >/dev/null 2>&1; then
    break
  fi
  sleep 1
done
docker exec "$CONTAINER" pg_isready -U test

cd "$REPO_ROOT/go"

echo "==> migrate up (1st pass)"
migrate -path migrations -database "$DSN" up

echo "==> migrate down -all"
migrate -path migrations -database "$DSN" down -all

echo "==> verifying only schema_migrations remains"
remaining=$(docker exec "$CONTAINER" psql -U test -d test -tAc \
  "SELECT count(*) FROM information_schema.tables WHERE table_schema = 'public' AND table_name <> 'schema_migrations'")
if [ "$remaining" -ne 0 ]; then
  echo "FAIL: ${remaining} table(s) survived a full down migration" >&2
  docker exec "$CONTAINER" psql -U test -d test -c "\dt"
  exit 1
fi

echo "==> migrate up (2nd pass, proves full reversibility)"
migrate -path migrations -database "$DSN" up

echo "OK: migrations are clean and reversible (up -> down -> up)"
