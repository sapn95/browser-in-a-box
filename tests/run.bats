#!/usr/bin/env bats
# Tests for run.sh that need no container engine.

setup() {
  RUN="${BATS_TEST_DIRNAME}/../run.sh"
}

@test "unknown command prints usage and fails" {
  run "$RUN" frobnicate
  [ "$status" -ne 0 ]
  [[ "$output" == *"Usage:"* ]]
}

@test "usage documents every command" {
  run "$RUN" frobnicate
  for cmd in up down open status logs shell reset; do
    [[ "$output" == *"$cmd"* ]]
  done
}

@test "run.sh is executable and has a bash shebang" {
  [ -x "$RUN" ]
  head -1 "$RUN" | grep -q '^#!/usr/bin/env bash$'
}

@test "run.sh enables errexit, nounset and pipefail" {
  grep -qE '^set -euo pipefail$' "$RUN"
}

@test "the web UI is bound to localhost only" {
  # A bare -p <port>:<port> would expose the browser to the whole network.
  grep -q '127.0.0.1:${PORT}:6901' "$RUN"
  run grep -qE '^[[:space:]]*-p "\$\{PORT\}' "$RUN"
  [ "$status" -ne 0 ]
}

@test "the image is pinned to a tag, not latest" {
  grep -qE 'IMAGE="\$\{CIB_IMAGE:-docker\.io/kasmweb/chrome:[0-9]' "$RUN"
  run grep -q 'kasmweb/chrome:latest' "$RUN"
  [ "$status" -ne 0 ]
}

@test "the KasmVNC password meets the 6 character minimum" {
  pw=$(sed -n 's/^PASSWORD="\${CIB_PASSWORD:-\(.*\)}"$/\1/p' "$RUN")
  [ "${#pw}" -ge 6 ]
}

@test "the resolution stays within the modes KasmVNC ships" {
  # Larger values silently fall back to 1024x768.
  res=$(sed -n 's/^RESOLUTION="\${CIB_RESOLUTION:-\(.*\)}"$/\1/p' "$RUN")
  [ "$res" = "1920x1200" ]
}

@test "the JPEG quality stays in the range KasmVNC accepts (0-9)" {
  # DynamicQualityMax=10 makes Xvnc exit with a fatal error.
  run grep -qE 'DynamicQualityM(in|ax)=(1[0-9]|[0-9]{2,})' "$RUN"
  [ "$status" -ne 0 ]
}
