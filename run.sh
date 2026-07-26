#!/usr/bin/env bash
# chrome-in-a-box — real Google Chrome in an isolated container, used from your
# own browser tab. See README.md for the why and the trade-offs.
#
# Everything runs on a container engine (podman or docker). The web UI is bound to
# 127.0.0.1 only, so the browser is never exposed to the network.
set -euo pipefail

# renovate: datasource=docker depName=kasmweb/chrome
IMAGE="${CIB_IMAGE:-docker.io/kasmweb/chrome:1.19.0}"
NAME="${CIB_NAME:-chrome-in-a-box}"
VOLUME="${CIB_VOLUME:-chrome-in-a-box-profile}"
PORT="${CIB_PORT:-6901}"
# KasmVNC only ships modes up to 1920x1200; anything larger silently falls back.
RESOLUTION="${CIB_RESOLUTION:-1920x1200}"
# KasmVNC requires a password >= 6 chars even though we disable the login prompt.
PASSWORD="${CIB_PASSWORD:-chromeinabox}"
# How long to wait for the web UI before giving up. Emulated boots are slower.
WAIT_SECS="${CIB_WAIT_SECS:-120}"
URL="https://localhost:${PORT}/?resize=scale"

log() { printf '%s\n' "$*"; }
die() { printf 'error: %s\n' "$*" >&2; exit 1; }

# Prints the engine to use. Call it as eng="$(engine)" — never in command
# position, where the die() below would only exit the subshell.
engine() {
  if [ -n "${CIB_ENGINE:-}" ]; then
    command -v "$CIB_ENGINE" 2>/dev/null && return 0
    die "CIB_ENGINE=${CIB_ENGINE} is not on PATH"
  fi
  command -v podman 2>/dev/null && return 0
  command -v docker 2>/dev/null && return 0
  die "need podman or docker on PATH"
}

usage() {
  cat >&2 <<EOF
Usage: ./run.sh <command>

  up        start the container and wait until Chrome is running
  down      stop and remove the container (the browser profile is kept)
  open      open ${URL} in your browser
  status    show the container state
  logs      show the last ${CIB_LOG_TAIL:-200} log lines (-f follows instead)
  shell     open a shell inside the container
  engine    print the container engine that will be used
  reset     delete the browser profile volume (destructive; asks first)

Environment overrides:
CIB_PORT (${PORT}), CIB_RESOLUTION (${RESOLUTION}), CIB_WAIT_SECS (${WAIT_SECS}),
CIB_ENGINE, CIB_IMAGE, CIB_NAME, CIB_VOLUME, CIB_PASSWORD, CIB_LOG_TAIL,
CIB_FORCE=1 (recreate a running container instead of reusing it).
EOF
  exit 1
}

# Both of these are documented ways to make the container die at boot, so fail
# early with an explanation instead of after a long, confusing wait.
preflight() {
  [ "${#PASSWORD}" -ge 6 ] ||
    die "CIB_PASSWORD must be at least 6 characters; KasmVNC refuses to start with a shorter one"
  [[ "$RESOLUTION" =~ ^[0-9]+x[0-9]+$ ]] ||
    die "CIB_RESOLUTION must look like 1920x1200, got '${RESOLUTION}'"
  { [ "${RESOLUTION%x*}" -le 1920 ] && [ "${RESOLUTION#*x}" -le 1200 ]; } ||
    die "CIB_RESOLUTION ${RESOLUTION} exceeds the modes KasmVNC ships (max 1920x1200); larger values silently fall back to 1024x768"
}

# The web UI answers on HTTPS with a self-signed cert; -k is expected here.
ui_is_up() {
  curl -sk --connect-timeout 2 --max-time 5 -o /dev/null "https://localhost:${PORT}/"
}

wait_for_ui() {
  local eng="$1" deadline=$(( SECONDS + WAIT_SECS ))
  while [ "$SECONDS" -lt "$deadline" ]; do
    ui_is_up && return 0
    # A container that died at boot would otherwise be misreported as "slow".
    if [ "$("$eng" inspect -f '{{.State.Running}}' "$NAME" 2>/dev/null || true)" != "true" ]; then
      "$eng" logs --tail 50 "$NAME" >&2 2>/dev/null || true
      die "the container exited during boot (log above)"
    fi
    sleep 2
  done
  return 1
}

# The image auto-starts Chrome, but an interrupted run can leave a stale profile
# lock behind, which makes Chrome exit immediately and show a black desktop.
# Setting the resolution here too: the image ignores VNC_RESOLUTION.
ensure_desktop() {
  local eng="$1" out rc=0
  # shellcheck disable=SC2016  # must expand inside the container, not here
  out="$("$eng" exec -e "RES=${RESOLUTION}" "$NAME" bash -c '
    export DISPLAY=:1
    xrandr -s "$RES" >/dev/null || echo "could not set mode $RES (KasmVNC ships a fixed mode list)" >&2
    if ! pgrep chrome >/dev/null 2>&1; then
      rm -f /home/kasm-user/.config/google-chrome/Singleton*
      nohup /opt/google/chrome/google-chrome --no-sandbox --start-maximized \
        --user-data-dir=/home/kasm-user/.config/google-chrome \
        >/tmp/chrome.log 2>&1 &
    fi
  ' 2>&1)" || rc=$?
  { [ "$rc" -eq 0 ] && [ -z "$out" ]; } && return 0
  printf 'warning: desktop setup incomplete (rc=%s)\n%s\n' "$rc" "$out" >&2
  return 1
}

cmd_up() {
  preflight
  local eng; eng="$(engine)"
  if [ "$("$eng" inspect -f '{{.State.Running}}' "$NAME" 2>/dev/null || true)" = "true" ] &&
     [ "${CIB_FORCE:-0}" != "1" ] && ui_is_up; then
    ensure_desktop "$eng" || true   # still re-applies the mode and revives Chrome
    log "Already running. Open ${URL}"
    return 0
  fi
  "$eng" rm -f "$NAME" >/dev/null 2>&1 || true
  log "Starting Google Chrome (amd64 image; emulated on Apple Silicon) ..."
  # --network bridge is load-bearing: kasmweb's vnc_startup.sh waits forever for a
  # veth interface, and rootless podman's default netns (pasta/slirp4netns) has
  # none. No-op on docker and on rootful podman, where bridge is already default.
  "$eng" run -d --name "$NAME" \
    --platform linux/amd64 \
    --network bridge \
    --shm-size=2g --security-opt seccomp=unconfined \
    -p "127.0.0.1:${PORT}:6901" \
    -e "VNC_PW=${PASSWORD}" \
    -e "VNCOPTIONS=-DisableBasicAuth=1 -DynamicQualityMin=8 -DynamicQualityMax=9 -DLP_ClipDelay=0" \
    -v "${VOLUME}:/home/kasm-user" \
    "$IMAGE" >/dev/null
  log "Waiting for the desktop ..."
  wait_for_ui "$eng" ||
    die "the web UI did not come up within ${WAIT_SECS}s; check './run.sh logs'"
  for _ in 1 2 3; do ensure_desktop "$eng" && break; sleep 3; done
  log ""
  log "Ready. Open ${URL}"
  log "No login needed — accept the self-signed certificate once."
}

cmd_down() {
  local eng; eng="$(engine)"
  if "$eng" rm -f "$NAME" >/dev/null 2>&1; then
    log "Stopped. The browser profile is kept in volume '${VOLUME}'."
  else
    log "Not running."
  fi
}

cmd_reset() {
  local eng reply; eng="$(engine)"
  printf 'Delete the browser profile (volume %s)? All logins are lost. [y/N] ' "$VOLUME"
  read -r reply || reply=""
  case "$reply" in
    [yY]*) "$eng" rm -f "$NAME" >/dev/null 2>&1 || true
           if "$eng" volume rm "$VOLUME" >/dev/null 2>&1; then
             log "Profile deleted."
           else
             log "Nothing to delete (volume '${VOLUME}' is not present or still in use)."
           fi ;;
    *)     log "Cancelled." ;;
  esac
}

case "${1:-up}" in
  up)     cmd_up ;;
  down)   cmd_down ;;
  reset)  cmd_reset ;;
  engine) engine ;;
  open)   open "$URL" 2>/dev/null || log "Open ${URL}" ;;
  status) eng="$(engine)"; "$eng" ps -a --filter "name=${NAME}" \
            --format "table {{.Names}}\t{{.Status}}\t{{.Ports}}" ;;
  logs)   eng="$(engine)"
          if [ "${2:-}" = "-f" ]; then "$eng" logs -f "$NAME"
          else "$eng" logs --tail "${CIB_LOG_TAIL:-200}" "$NAME"; fi ;;
  shell)  eng="$(engine)"; "$eng" exec -it "$NAME" bash ;;
  *)      usage ;;
esac
