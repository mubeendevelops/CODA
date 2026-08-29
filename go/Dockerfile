# Shared Dockerfile for go-api and go-orchestrator. SERVICE selects the
# cmd/ package to build; both binaries are cheap enough that duplicating the
# build stage per-service isn't worth a second file.
# 1.25 to match go.mod (claude_context.md §4: bumped from 1.23 when
# testcontainers-go v0.44 was added for Phase 3's integration tests, which
# sets 1.25 as its own `go` directive floor). Leaving this at 1.23 makes
# `go mod download` fail under GOTOOLCHAIN=local.
FROM golang:1.25-alpine AS build
ARG SERVICE
WORKDIR /src

RUN apk add --no-cache git

COPY go.mod go.sum ./
RUN go mod download

COPY . .
RUN CGO_ENABLED=0 GOOS=linux go build -o /out/service ./cmd/${SERVICE}

FROM alpine:3.20
RUN apk add --no-cache curl ca-certificates
COPY --from=build /out/service /usr/local/bin/service
ENTRYPOINT ["/usr/local/bin/service"]
