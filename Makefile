SHELL := /usr/bin/env bash
export PATH := $(HOME)/.local/bin:$(HOME)/go/bin:$(PATH)

# .env is git-ignored (see .env.example) — load it if present so
# migrate/seed can build a DSN without every developer exporting vars by
# hand. Silently absent on a fresh clone before `cp .env.example .env`.
ifneq (,$(wildcard .env))
include .env
export
endif

.PHONY: proto generate build build-go build-python build-frontend \
        test test-go test-python test-frontend test-integration \
        lint lint-go lint-python lint-frontend \
        up down migrate migrate-test seed e2e clean openapi-lint \
        data eval-registry eval-splits eval-asr-der

## proto: regenerate Go + Python types from proto/coda/v1/*.proto (commits generated code).
proto:
	cd proto && buf lint
	cd proto && buf generate
	./scripts/gen_proto_python.sh

## generate: alias for proto (plan.md Phase 0 acceptance criterion #2 names it this).
generate: proto

## build: build every service.
build: build-go build-python build-frontend

build-go:
	cd go && go build ./...

build-python:
	cd python && uv sync --all-packages

build-frontend:
	cd frontend && npm run build

## test: run every service's test suite.
test: test-go test-python test-frontend

test-go:
	cd go && go test ./...

test-python:
	cd python && uv run --project shared pytest -q shared/tests
	cd python && uv run --project asr-service pytest -q asr-service/tests
	cd python && uv run --project nlp-service pytest -q nlp-service/tests
	cd python && uv run --project eval pytest -q eval/tests

test-frontend:
	cd frontend && npm run test -- --run

## test-integration: run the integration suites against real, disposable
## containers (testcontainers-go) — go-api over real HTTP against Postgres +
## MinIO, and the queue/orchestrator against a real Redis + Postgres
## (docs/architecture.md §2 and §4: at-least-once delivery, XAUTOCLAIM
## recovery and transactional transitions are properties of Redis and
## Postgres, so a mock would only assert what the author assumed).
## Excluded from `make test` (the `integration` build tag) since it needs
## Docker and takes longer; needs the `migrate` CLI on PATH the same way
## `make migrate-test` does.
test-integration:
	cd go && go test -tags=integration -timeout 25m ./internal/http/... ./internal/queue/... ./internal/pipeline/...

## lint: lint every service.
lint: lint-go lint-python lint-frontend

lint-go:
	cd go && golangci-lint run ./...

lint-python:
	cd python && uv run --project shared ruff check shared/src shared/tests
	cd python && uv run --project asr-service ruff check asr-service/src asr-service/tests
	cd python && uv run --project nlp-service ruff check nlp-service/src nlp-service/tests
	cd python && uv run --project eval ruff check eval/src eval/tests
	cd python && uv run --project shared mypy shared/src
	cd python && uv run --project asr-service mypy asr-service/src
	cd python && uv run --project nlp-service mypy nlp-service/src
	cd python && uv run --project eval mypy eval/src

lint-frontend:
	cd frontend && npm run lint
	cd frontend && npm run format

## up: bring the full dev stack up, waiting for every healthcheck.
up:
	docker compose up --build -d --wait

## down: tear the dev stack down (keeps volumes).
down:
	docker compose down

## migrate: apply Postgres migrations to the running dev-compose database.
migrate:
	migrate -path go/migrations \
		-database "postgres://$(POSTGRES_USER):$(POSTGRES_PASSWORD)@localhost:$(POSTGRES_HOST_PORT)/$(POSTGRES_DB)?sslmode=disable" \
		up

## migrate-test: prove migrations are reversible (up -> down -> up) against
## a disposable Postgres container — does not touch the dev database.
migrate-test:
	./scripts/migrate_test.sh

## seed: load dev seed data (one org, a doctor + admin user, two consultations).
## Overrides POSTGRES_HOST/PORT from .env (the in-Docker-network values) to
## the host-side localhost:$(POSTGRES_HOST_PORT) mapping, since this runs
## outside Docker.
seed:
	cd go && POSTGRES_HOST=localhost POSTGRES_PORT=$(POSTGRES_HOST_PORT) go run ./cmd/seed

## e2e: walking-skeleton smoke test against a running `docker compose up` stack.
## Proves the plumbing (upload -> job -> echo workers -> awaiting_review)
## independently of model behaviour — no real ASR/NLP runs. Real-model
## acceptance is Phase 1's job once the echo handlers are replaced. Runs
## inside the compose network (docker compose run) rather than on the host:
## presigned MinIO URLs are signed against the container hostname, and a
## SigV4 signature can't be rewritten to a host-reachable one after signing.
e2e:
	docker compose run --rm e2e

## data: alias for eval-registry — plan.md Phase 2 AC 1 names it `make data`.
## Note this only (re)builds the registry/manifests from whatever is already
## under data/raw/ — it does not itself fetch datasets (git clone / git lfs
## pull are still manual steps; see docs/eval/ for exactly what was fetched
## and how). "Fetches and verifies every public dataset" is not yet met.
data: eval-registry

## eval-registry: (re)build the item manifest + data/registry.yaml from
## whatever datasets are present under data/raw/ (plan.md Phase 2 AC 1).
eval-registry:
	cd python && uv run --project eval coda-eval registry

## eval-splits: assign + validate session-disjoint train/dev/test splits for
## one dataset. Usage: make eval-splits DATASET=primock57
eval-splits:
	cd python && uv run --project eval coda-eval splits --dataset $(DATASET)

## eval-asr-der: run the ASR/DER measurement against PriMock57 audio actually
## present on disk (runs outside Docker, against the host-mapped Postgres
## port like `make seed`). Needs `make eval-registry` and
## `make eval-splits DATASET=primock57` to have been run first.
eval-asr-der:
	cd python && POSTGRES_HOST=localhost uv run --project eval coda-eval run-primock57-asr

## openapi-lint: sanity-check openapi/coda-v1.yaml parses as YAML with the
## expected top-level shape. Not a full OpenAPI schema validator — the spec
## is hand-authored against go/internal/http/router.go (see its header
## comment), not generated, so there is no codegen step to fail loudly if
## the two drift; this at least catches YAML syntax errors.
openapi-lint:
	python3 -c "import yaml; d = yaml.safe_load(open('openapi/coda-v1.yaml')); assert 'paths' in d and len(d['paths']) > 0; print(f\"OK: {len(d['paths'])} paths\")"

## clean: remove build artifacts and stop the stack, keeping caches and volumes.
clean: down
	rm -rf go/bin frontend/dist
