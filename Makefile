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
        up down migrate migrate-test seed e2e clean

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
	cd python && uv run --project asr-service pytest -q asr-service/tests
	cd python && uv run --project nlp-service pytest -q nlp-service/tests

test-frontend:
	cd frontend && npm run test -- --run

## test-integration: run go-api's integration tests against a real,
## disposable Postgres container (testcontainers-go) — auth, RBAC, and
## org-scoping end to end over real HTTP. Excluded from `make test` (the
## `integration` build tag) since it needs Docker and takes longer; needs
## the `migrate` CLI on PATH the same way `make migrate-test` does.
test-integration:
	cd go && go test -tags=integration ./internal/http/...

## lint: lint every service.
lint: lint-go lint-python lint-frontend

lint-go:
	cd go && golangci-lint run ./...

lint-python:
	cd python && uv run --project asr-service ruff check asr-service/src asr-service/tests
	cd python && uv run --project nlp-service ruff check nlp-service/src nlp-service/tests
	cd python && uv run --project asr-service mypy asr-service/src
	cd python && uv run --project nlp-service mypy nlp-service/src

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

## e2e: end-to-end pipeline test against a running stack (Phase 1+ — nothing to test yet).
e2e:
	@echo "no e2e path yet — implemented in Phase 1 (vertical slice)"

## clean: remove build artifacts and stop the stack, keeping caches and volumes.
clean: down
	rm -rf go/bin frontend/dist
