#!/usr/bin/env bash
# chrome-in-a-box — real Google Chrome in an isolated container, used from your
# own browser tab. See README.md for the why and the trade-offs.
#
# Everything runs on a container engine (podman or docker). The web UI is bound to
# 127.0.0.1 only, so the browser is never exposed to the network.
set -euo pipefail

# renovate: datasource=docker depName=kasmweb/chrome
IMAGE="${CIB_IMAGE:-docker.io/kasmweb/chrome:1.16.0}"
NAME="${CIB_NAME:-chrome-in-a-box}"
VOLUME="${CIB_VOLUME:-chrome-in-a-box-profile}"
PORT="${CIB_PORT:-6901}"
# KasmVNC only ships modes up to 1920x1200; anything larger silently falls back.
RESOLUTION="${CIB_RESOLUTION:-1920x1200}"
# KasmVNC requires a password >= 6 chars even though we disable the login prompt.
PASSWORD="${CIB_PASSWORD:-chromeinabox}"
URL="https://localhost:${PORT}/?resize=scale"

log() { printf '%s\n' "$*"; }
die() { printf 'error: %s\n' "$*" >&2; exit 1; }

engine() {
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
  logs      follow the container logs
  shell     open a shell inside the container
  reset     delete the browser profile volume (destructive; asks first)

Environment overrides: CIB_PORT (${PORT}), CIB_RESOLUTION (${RESOLUTION}),
CIB_IMAGE, CIB_NAME, CIB_VOLUME, CIB_PASSWORD.
EOF
  exit 1
}

# The web UI answers on HTTPS with a self-signed cert; -k is expected here.
ui_is_up() { curl -sk -o /dev/null "https://localhost:${PORT}/"; }

wait_for_ui() {
  local _
  for _ in $(seq 1 60); do
    ui_is_up && return 0
    sleep 2
  done
  return 1
}

# The image auto-starts Chrome, but an interrupted run can leave a stale profile
# lock behind, which makes Chrome exit immediately and show a black desktop.
# Setting the resolution here too: the image ignores VNC_RESOLUTION.
ensure_desktop() {
  local eng="$1"
  # shellcheck disable=SC2016  # must expand inside the container, not here
  "$eng" exec -e "RES=${RESOLUTION}" "$NAME" bash -c '
    export DISPLAY=:1
    xrandr -s "$RES" 2>/dev/null || true
    if [ "$(pgrep -c chrome)" -eq 0 ]; then
      rm -f /home/kasm-user/.config/google-chrome/Singleton* 2>/dev/null || true
      nohup /opt/google/chrome/google-chrome --no-sandbox --start-maximized \
        --user-data-dir=/home/kasm-user/.config/google-chrome \
        >/tmp/chrome.log 2>&1 &
    fi
  ' >/dev/null 2>&1 || true
}

cmd_up() {
  local eng; eng="$(engine)"
  "$eng" rm -f "$NAME" >/dev/null 2>&1 || true
  log "Starting Google Chrome (amd64 image; emulated on Apple Silicon) ..."
  "$eng" run -d --name "$NAME" \
    --platform linux/amd64 \
    --shm-size=2g --security-opt seccomp=unconfined \
    -p "127.0.0.1:${PORT}:6901" \
    -e "VNC_PW=${PASSWORD}" \
    -e "VNCOPTIONS=-DisableBasicAuth=1 -DynamicQualityMin=8 -DynamicQualityMax=9 -DLP_ClipDelay=0" \
    -v "${VOLUME}:/home/kasm-user" \
    "$IMAGE" >/dev/null
  log "Waiting for the desktop ..."
  wait_for_ui || die "the web UI did not come up; check './run.sh logs'"
  ensure_desktop "$eng"
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
  local eng; eng="$(engine)" reply
  printf 'Delete the browser profile (volume %s)? All logins are lost. [y/N] ' "$VOLUME"
  read -r reply
  case "$reply" in
    [yY]*) "$eng" rm -f "$NAME" >/dev/null 2>&1 || true
           "$eng" volume rm "$VOLUME" >/dev/null 2>&1 && log "Profile deleted." ;;
    *)     log "Cancelled." ;;
  esac
}

case "${1:-up}" in
  up)     cmd_up ;;
  down)   cmd_down ;;
  reset)  cmd_reset ;;
  open)   open "$URL" 2>/dev/null || log "Open ${URL}" ;;
  status) "$(engine)" ps -a --filter "name=${NAME}" \
            --format "table {{.Names}}\t{{.Status}}\t{{.Ports}}" ;;
  logs)   "$(engine)" logs -f "$NAME" ;;
  shell)  "$(engine)" exec -it "$NAME" bash ;;
  *)      usage ;;
esac
