#!/usr/bin/env bash
# Generates Python message classes for proto/coda/v1/*.proto into
# python/coda_proto/src/coda_proto/coda/v1/. Uses grpc_tools.protoc (bundles
# its own protoc) rather than buf's built-in Python plugin — see the comment
# in proto/buf.gen.yaml for why.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PROTO_DIR="$REPO_ROOT/proto"
OUT_DIR="$REPO_ROOT/python/coda_proto/src"

cd "$REPO_ROOT/python"

rm -rf "$OUT_DIR/coda/v1"
mkdir -p "$OUT_DIR/coda/v1"

uv run --project coda_proto python -m grpc_tools.protoc \
  -I "$PROTO_DIR" \
  --python_out="$OUT_DIR" \
  --pyi_out="$OUT_DIR" \
  "$PROTO_DIR"/coda/v1/*.proto

touch "$OUT_DIR/coda/v1/__init__.py"

echo "generated: $(find "$OUT_DIR/coda/v1" -name '*_pb2.py' | wc -l) _pb2.py files"
