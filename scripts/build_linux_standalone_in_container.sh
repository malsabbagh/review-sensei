#!/usr/bin/env bash
# Build on the oldest supported glibc and smoke-test in a clean consumer image.
set -euo pipefail

if [[ $# -ne 1 ]]; then
  echo "usage: $0 linux-arm64-gnu|linux-x64-gnu" >&2
  exit 2
fi

target=$1
case "$target" in
  linux-arm64-gnu) platform=linux/arm64 ;;
  linux-x64-gnu) platform=linux/amd64 ;;
  *) echo "unsupported Linux standalone target: $target" >&2; exit 2 ;;
esac

root=$(git rev-parse --show-toplevel)
build_image=python:3.11-bookworm@sha256:b99029c95d3d37fb1e4e76d287f7984373dca77c665885986e31b2c95260c13c
consumer_image=debian:12-slim@sha256:3783cc01769c7b2b1b83a5c5ad96c815348e28ed7da68e2e3687004faa906251

docker run --rm --platform "$platform" \
  --user "$(id -u):$(id -g)" \
  --volume "$root:/workspace" --workdir /workspace \
  --env "TARGET=$target" --env HOME=/tmp --env PYTHONPATH=src \
  "$build_image" bash -euo pipefail -c '
    test "$(getconf GNU_LIBC_VERSION)" = "glibc 2.36"
    python -m venv /tmp/review-sensei-release-venv
    python_bin=/tmp/review-sensei-release-venv/bin/python
    "$python_bin" -m pip install --disable-pip-version-check .
    "$python_bin" -m pip install --disable-pip-version-check -r packaging/standalone/requirements.txt
    "$python_bin" scripts/build_standalone.py \
      --target "$TARGET" --output "dist/standalone/$TARGET"
    "$python_bin" scripts/standalone_smoke.py \
      --executable "dist/standalone/$TARGET/review-sensei/review-sensei" \
      --repository /workspace \
      --diff evaluation/v1/diffs/off-by-one.patch \
      --fixture-response evaluation/v1/responses/off-by-one.json \
      --base-ref "$(git rev-parse HEAD^)" \
      --head-ref "$(git rev-parse HEAD)"
  '

# No Python, Node, model credential, or network is available to this consumer.
docker run --rm --platform "$platform" --network none \
  --user "$(id -u):$(id -g)" \
  --volume "$root:/workspace:ro" --workdir /workspace \
  --env "TARGET=$target" --env HOME=/tmp \
  "$consumer_image" sh -eu -c '
    test "$(getconf GNU_LIBC_VERSION)" = "glibc 2.36"
    executable="dist/standalone/$TARGET/review-sensei/review-sensei"
    "$executable" --version
    "$executable" plan \
      --diff evaluation/v1/diffs/off-by-one.patch \
      --provider ollama --json >/dev/null
    # The local review contract must hold with no host identity, no Python,
    # and no network: one required fix exits 1, and the same completed run
    # under the operational contract exits 0.
    set +e
    "$executable" --provider fixture \
      --fixture-response tests/fixtures/standalone-smoke/blocking-review.json \
      --diff tests/fixtures/standalone-smoke/blocking-review.patch \
      --no-learning-proposals --format markdown >/tmp/review.md
    review_status=$?
    "$executable" --provider fixture \
      --fixture-response tests/fixtures/standalone-smoke/blocking-review.json \
      --diff tests/fixtures/standalone-smoke/blocking-review.patch \
      --no-learning-proposals --exit-semantics operational >/dev/null
    operational_status=$?
    # One explicit persistent-local session must also start with no ledger or
    # artifact path supplied, storing its state under the platform default.
    "$executable" --provider fixture \
      --fixture-response tests/fixtures/standalone-smoke/blocking-review.json \
      --diff tests/fixtures/standalone-smoke/blocking-review.patch \
      --no-learning-proposals --local-session >/dev/null
    session_status=$?
    set -e
    test "$review_status" -eq 1 || { echo "local review exit $review_status" >&2; exit 1; }
    test "$operational_status" -eq 0 || { echo "operational exit $operational_status" >&2; exit 1; }
    test "$session_status" -eq 1 || { echo "local session exit $session_status" >&2; exit 1; }
    test -s /tmp/review.md
    find "$HOME" -name "*.json" -type f | grep -q . || \
      { echo "local session state was not stored" >&2; exit 1; }
  '
