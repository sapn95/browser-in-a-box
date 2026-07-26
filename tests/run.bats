#!/usr/bin/env bats
# Tests for run.sh. Behavioural tests use a PATH shim for the container engine, so
# they exercise the real code paths without needing podman or docker.
#
# shellcheck disable=SC2016  # the static tests grep for literal ${...} in run.sh's source

setup() {
  RUN="${BATS_TEST_DIRNAME}/../run.sh"
  STUB_DIR="${BATS_TEST_TMPDIR}/bin"
  CALLS="${BATS_TEST_TMPDIR}/calls.log"
  mkdir -p "$STUB_DIR"
  cat > "$STUB_DIR/podman" <<EOF
#!/usr/bin/env bash
printf '%s\n' "\$*" >> "$CALLS"
exit 0
EOF
  chmod +x "$STUB_DIR/podman"
  PATH="$STUB_DIR:$PATH"
  # Keep the shim in charge even on machines that have docker installed.
  export CIB_ENGINE=podman
}

# --- dispatch -----------------------------------------------------------------

@test "unknown command prints usage and fails" {
  run "$RUN" frobnicate
  [ "$status" -ne 0 ]
  [[ "$output" == *"Usage:"* ]]
}

@test "usage documents every command the dispatcher handles" {
  run "$RUN" frobnicate
  for cmd in $(grep -oE '^  [a-z]+\)' "$RUN" | tr -d ' )'); do
    [[ "$output" =~ (^|$'\n')"  $cmd"[[:space:]] ]]
  done
}

@test "engine prints the engine that will be used" {
  run "$RUN" engine
  [ "$status" -eq 0 ]
  [[ "$output" == *"podman"* ]]
}

# --- behaviour ----------------------------------------------------------------

@test "down removes the container" {
  run "$RUN" down
  [ "$status" -eq 0 ]
  grep -q -- 'rm -f chrome-in-a-box' "$CALLS"
}

@test "reset declines cleanly when the answer is not y" {
  run "$RUN" reset <<< "n"
  [ "$status" -eq 0 ]
  [[ "$output" == *"Cancelled."* ]]
  run grep -q -- 'volume rm' "$CALLS"
  [ "$status" -ne 0 ]
}

@test "reset deletes the profile volume when confirmed" {
  run "$RUN" reset <<< "y"
  [ "$status" -eq 0 ]
  grep -q -- 'volume rm chrome-in-a-box-profile' "$CALLS"
}

@test "logs does not follow unless -f is given" {
  run "$RUN" logs
  [ "$status" -eq 0 ]
  grep -q -- 'logs --tail 200 chrome-in-a-box' "$CALLS"
}

@test "logs -f follows" {
  run "$RUN" logs -f
  [ "$status" -eq 0 ]
  grep -q -- 'logs -f chrome-in-a-box' "$CALLS"
}

@test "an unusable CIB_ENGINE fails loudly instead of silently falling back" {
  CIB_ENGINE=definitely-not-installed run "$RUN" engine
  [ "$status" -ne 0 ]
  [[ "$output" == *"not on PATH"* ]]
}

# --- preflight: the settings that are known to kill the container -------------

@test "a password shorter than 6 characters is rejected before starting" {
  CIB_PASSWORD=abc run "$RUN" up
  [ "$status" -ne 0 ]
  [[ "$output" == *"at least 6 characters"* ]]
}

@test "a resolution above the modes KasmVNC ships is rejected" {
  CIB_RESOLUTION=2560x1600 run "$RUN" up
  [ "$status" -ne 0 ]
  [[ "$output" == *"1920x1200"* ]]
}

@test "a malformed resolution is rejected" {
  CIB_RESOLUTION=huge run "$RUN" up
  [ "$status" -ne 0 ]
  [[ "$output" == *"must look like"* ]]
}

# --- static guarantees --------------------------------------------------------

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

@test "the container gets a bridge network" {
  # kasm's vnc_startup.sh waits forever for a veth; rootless podman's default
  # network namespace has none, so the desktop never comes up without this.
  grep -q -- '--network bridge' "$RUN"
}

@test "the image is pinned to a tag, not latest" {
  grep -qE 'IMAGE="\$\{CIB_IMAGE:-docker\.io/kasmweb/chrome:[0-9]' "$RUN"
  run grep -q 'kasmweb/chrome:latest' "$RUN"
  [ "$status" -ne 0 ]
}

@test "the default password meets the 6 character minimum" {
  pw=$(sed -n 's/^PASSWORD="\${CIB_PASSWORD:-\(.*\)}"$/\1/p' "$RUN")
  [ "${#pw}" -ge 6 ]
}

@test "the default resolution stays within the modes KasmVNC ships" {
  res=$(sed -n 's/^RESOLUTION="\${CIB_RESOLUTION:-\(.*\)}"$/\1/p' "$RUN")
  [ "$res" = "1920x1200" ]
}

@test "the JPEG quality stays in the range KasmVNC accepts (0-9)" {
  # DynamicQualityMax=10 makes Xvnc exit with a fatal error.
  run grep -qE 'DynamicQualityM(in|ax)=(1[0-9]|[0-9]{2,})' "$RUN"
  [ "$status" -ne 0 ]
}

@test "the stale-profile-lock and resolution workarounds are still in place" {
  # Both guard against a black desktop; the paths are image-specific.
  grep -q 'rm -f /home/kasm-user/.config/google-chrome/Singleton\*' "$RUN"
  grep -q 'xrandr -s "\$RES"' "$RUN"
}
