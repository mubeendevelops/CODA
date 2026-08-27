# Shared Dockerfile for go-api and go-orchestrator. SERVICE selects the
# cmd/ package to build; both binaries are cheap enough that duplicating the
# build stage per-service isn't worth a second file.
FROM golang:1.23-alpine AS build
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
