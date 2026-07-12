#!/usr/bin/env bats
# Smoke tests for run.sh argument handling (no cluster required).

setup() {
  RUN="${BATS_TEST_DIRNAME}/../run.sh"
}

@test "no args prints usage and fails" {
  run "$RUN"
  [ "$status" -ne 0 ]
  [[ "$output" == *"Usage:"* ]]
}

@test "unknown command prints usage and fails" {
  run "$RUN" frobnicate
  [ "$status" -ne 0 ]
  [[ "$output" == *"Usage:"* ]]
}

@test "usage documents the core commands" {
  run "$RUN"
  [[ "$output" == *"up"* ]]
  [[ "$output" == *"forward"* ]]
  [[ "$output" == *"down"* ]]
  [[ "$output" == *"nuke"* ]]
}

@test "run.sh enables errexit and pipefail" {
  head -10 "$RUN" | grep -qE 'set -euo pipefail|set -o pipefail'
}
