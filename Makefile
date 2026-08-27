SHELL := /usr/bin/env bash
export PATH := $(HOME)/.local/bin:$(HOME)/go/bin:$(PATH)

.PHONY: proto generate build build-go build-python build-frontend \
        test test-go test-python test-frontend \
        lint lint-go lint-python lint-frontend \
        up down migrate seed e2e clean

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

## migrate: apply Postgres migrations (Phase 3 — no migrations exist yet).
migrate:
	@echo "no migrations yet — implemented in Phase 3 (docs/architecture.md §5)"

## seed: load dev seed data (Phase 3+ — no seed data exists yet).
seed:
	@echo "no seed data yet — implemented alongside Phase 3/4"

## e2e: end-to-end pipeline test against a running stack (Phase 1+ — nothing to test yet).
e2e:
	@echo "no e2e path yet — implemented in Phase 1 (vertical slice)"

## clean: remove build artifacts and stop the stack, keeping caches and volumes.
clean: down
	rm -rf go/bin frontend/dist
