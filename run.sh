#!/usr/bin/env bash
# chrome-in-a-box — self-hosted, isolated browser you use from your own browser tab.
#
# Two paths, because Chromium and Google Chrome have different constraints:
#   * up/forward  — Chromium (arm64-native, fast) via Helm on a *local* minikube profile.
#   * chrome      — Google Chrome (amd64-only, emulated on ARM) directly via podman/docker,
#                   because it needs Google's password sync and can't run as an arm64 pod.
#                   Emulated Chrome needs --single-process (baked into the image) or it
#                   crash-loops; see the README section "Google Chrome on Apple Silicon".
#
# The minikube profile is dedicated to this project and never touches any other
# kube-context you may have configured.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
RELEASE="chrome-in-a-box"
NS="chrome-in-a-box"
PROFILE="chrome-in-a-box"          # dedicated local cluster; also used as kube-context name
CHART="$HERE/charts/chrome-in-a-box"
DRIVER="${DRIVER:-podman}"
BROWSER="${BROWSER:-chromium}"     # or: google-chrome
WEB_PORT="${WEB_PORT:-8080}"
TCPMUX_PORT="${TCPMUX_PORT:-8081}"

# Google Chrome (amd64) path — built locally and run directly under the container
# engine, emulated on Apple Silicon. Not a Kubernetes workload: k8s resolves images
# to the arm64 node, which the amd64-only Google Chrome image cannot satisfy.
CHROME_NAME="cib-chrome"
CHROME_IMAGE_LOCAL="chrome-in-a-box:google-chrome-local"
CHROME_VOLUME="cib-chrome-profile"

# KasmVNC path — real Google Chrome served over a websocket VNC web client. Input rides
# the websocket, NOT a WebRTC data channel, so ad-blockers / WebRTC-blocking extensions
# can't silently break it. Chrome is amd64-only -> emulated on Apple Silicon (Rosetta).
# The KasmVNC password must be >= 6 characters.
KASM_NAME="cib-kasm-chrome"
KASM_IMAGE="docker.io/kasmweb/chrome:1.16.0"
KASM_VOLUME="cib-kasm-chrome-profile"
KASM_PORT="${KASM_PORT:-6901}"
KASM_PW="${KASM_PW:-nekobox}"

need() { command -v "$1" >/dev/null 2>&1 || { echo "error: '$1' not found on PATH" >&2; exit 1; }; }

engine() { command -v podman || command -v docker || { echo "error: need podman or docker on PATH" >&2; exit 1; }; }

ensure_browser_supported() {
  if [[ "$BROWSER" != "google-chrome" ]]; then
    return
  fi

  local node_arches
  node_arches="$(kubectl --context "$PROFILE" get nodes -o jsonpath='{range .items[*]}{.status.nodeInfo.architecture}{"\n"}{end}' 2>/dev/null | sort -u | paste -sd, - || true)"

  if [[ "$node_arches" != "amd64" ]]; then
    cat >&2 <<EOF
error: BROWSER=google-chrome uses an amd64-only image, but the '$PROFILE' minikube node architecture is '${node_arches:-unknown}'.

On Apple Silicon, use the emulated Google Chrome path instead:
  ./run.sh chrome

Or use the fast native Chromium image:
  ./run.sh up
EOF
    exit 1
  fi
}

usage() {
  cat >&2 <<EOF
Usage: ./run.sh <command>

Chromium via Helm on a local minikube cluster (arm64-native, fast; no Google sync):
  up              start the local minikube cluster & 'helm upgrade --install' the chart
  forward         port-forward the web UI + WebRTC to localhost (foreground, Ctrl-C to stop)
  status          show pods & service
  logs            follow the browser pod logs
  down            'helm uninstall' the release (keeps the cluster)
  nuke            delete the whole local minikube profile

Google Chrome via podman/docker (amd64, emulated; adds Google account sync + Password Manager):
  chrome          build & run Google Chrome (fail-fast if it crash-loops under emulation)
  chrome-down     stop & remove the Google Chrome container

Real Google Chrome over KasmVNC (amd64/emulated; websocket input — ad-blocker-proof; recommended):
  kasm            run real Google Chrome over KasmVNC (https://localhost:${KASM_PORT}, kasm_user)
  kasm-down       stop & remove the KasmVNC Chrome container

  open            open http://localhost:${WEB_PORT} in your browser (Neko paths)
EOF
  exit 1
}

cmd_up() {
  need minikube; need helm; need kubectl
  if ! minikube status -p "$PROFILE" >/dev/null 2>&1; then
    echo "Starting local minikube profile '$PROFILE' (driver: $DRIVER) ..."
    minikube start -p "$PROFILE" --driver="$DRIVER"
  fi
  ensure_browser_supported
  echo "helm upgrade --install ($BROWSER) ..."
  helm --kube-context "$PROFILE" upgrade --install "$RELEASE" "$CHART" \
    --namespace "$NS" --create-namespace \
    --set image.tag="$BROWSER"
  kubectl --context "$PROFILE" -n "$NS" rollout status "deploy/$RELEASE" --timeout=240s
  echo "Deployed. Next:  ./run.sh forward   (then ./run.sh open)"
}

cmd_forward() {
  need kubectl
  echo "Forwarding ${WEB_PORT} + ${TCPMUX_PORT} … Ctrl-C to stop."
  echo "Open http://localhost:${WEB_PORT}/?usr=chrome&pwd=neko  (auto-login)"
  kubectl --context "$PROFILE" -n "$NS" port-forward "svc/$RELEASE" \
    "${WEB_PORT}:8080" "${TCPMUX_PORT}:8081"
}

# Fail-fast: confirm Chrome stays up instead of crash-looping into a black screen.
# Same supervisor PID after a pause == stable; changed/empty == flapping.
chrome_healthcheck() {
  local eng="$1" pid1 pid2
  echo "Health-check: confirming Google Chrome stays up (not crash-looping) ..."
  sleep 8
  pid1="$("$eng" exec "$CHROME_NAME" supervisorctl status google-chrome 2>/dev/null | grep -oE 'pid [0-9]+' || true)"
  sleep 10
  pid2="$("$eng" exec "$CHROME_NAME" supervisorctl status google-chrome 2>/dev/null | grep -oE 'pid [0-9]+' || true)"
  if [ -z "$pid1" ] || [ "$pid1" != "$pid2" ]; then
    cat >&2 <<EOF

error: Google Chrome is crash-looping under emulation (pid '${pid1:-none}' -> '${pid2:-none}').
On Apple Silicon, QEMU user-mode emulation lacks syscalls Chrome needs. Options:
  * Enable Rosetta for a stable, faster amd64 runtime — see the README section
    "Google Chrome on Apple Silicon" (requires recreating the podman machine), or
  * Use the fast native Chromium instead:  ./run.sh up
EOF
    "$eng" logs --tail 5 "$CHROME_NAME" >&2 2>/dev/null || true
    exit 1
  fi
  echo "Healthy (${pid2})."
}

cmd_chrome() {
  local eng; eng="$(engine)"
  # Rosetta runs syscalls natively, so multi-process Chrome is stable and faster.
  # Without it, fall back to --single-process to avoid the QEMU crash-loop.
  local single_process="true"
  if command -v podman >/dev/null 2>&1 && \
     podman machine inspect podman-machine-default --format '{{.Rosetta}}' 2>/dev/null | grep -qi true; then
    single_process="false"
    echo "Rosetta detected — building multi-process Google Chrome (faster)."
  else
    echo "No Rosetta — building with --single-process (QEMU emulation workaround)."
  fi
  echo "Building Google Chrome image (amd64) ..."
  "$eng" build --platform linux/amd64 \
    --build-arg BROWSER=google-chrome --build-arg "SINGLE_PROCESS=${single_process}" \
    -t "$CHROME_IMAGE_LOCAL" "$HERE"
  "$eng" rm -f "$CHROME_NAME" >/dev/null 2>&1 || true
  # Clear any stale Chrome profile lock from a previous hard stop, otherwise Chrome
  # refuses to start ("profile appears to be in use by another process") -> black screen.
  "$eng" run --rm -v "${CHROME_VOLUME}:/home/neko" docker.io/library/alpine \
    sh -c 'rm -f /home/neko/.config/google-chrome/Singleton* /home/neko/.config/chromium/Singleton* 2>/dev/null || true' >/dev/null 2>&1 || true
  echo "Starting Google Chrome (amd64, emulated) ..."
  "$eng" run -d --name "$CHROME_NAME" \
    --platform linux/amd64 \
    --shm-size=2g --cap-add=SYS_ADMIN --security-opt seccomp=unconfined \
    -p "127.0.0.1:${WEB_PORT}:8080" \
    -p "127.0.0.1:${TCPMUX_PORT}:${TCPMUX_PORT}" \
    -e "NEKO_WEBRTC_TCPMUX=${TCPMUX_PORT}" \
    -e NEKO_WEBRTC_NAT1TO1=127.0.0.1 \
    -e NEKO_WEBRTC_ICELITE=1 \
    -e NEKO_MEMBER_PROVIDER=noauth \
    -e NEKO_SESSION_IMPLICIT_HOSTING=true \
    -v "${CHROME_VOLUME}:/home/neko" \
    "$CHROME_IMAGE_LOCAL" >/dev/null
  chrome_healthcheck "$eng"
  echo "Up. Open http://localhost:${WEB_PORT}/?usr=chrome&pwd=neko  (auto-login), then sign into Google."
}

cmd_chrome_down() {
  local eng; eng="$(engine)"
  "$eng" rm -f "$CHROME_NAME" >/dev/null 2>&1 && echo "stopped." || echo "not running."
}

cmd_kasm() {
  local eng; eng="$(engine)"
  echo "Starting real Google Chrome over KasmVNC (amd64/emulated; websocket input) ..."
  "$eng" rm -f "$KASM_NAME" >/dev/null 2>&1 || true
  "$eng" run -d --name "$KASM_NAME" \
    --platform linux/amd64 \
    --shm-size=2g --security-opt seccomp=unconfined \
    -p "127.0.0.1:${KASM_PORT}:6901" \
    -e "VNC_PW=${KASM_PW}" \
    -e "VNC_RESOLUTION=${KASM_RES:-1920x1080}" \
    -v "${KASM_VOLUME}:/home/kasm-user" \
    "$KASM_IMAGE" >/dev/null
  # Kasm's auto-launch can leave a black screen after an emulated first-launch crash
  # (which also drops a stale profile lock). Wait for the desktop, then make sure
  # Chrome is actually running.
  echo "Waiting for the desktop, then ensuring Chrome is up ..."
  sleep 12
  # shellcheck disable=SC2016  # $(pgrep) and $DISPLAY must expand inside the container, not here
  "$eng" exec "$KASM_NAME" bash -c '
    export DISPLAY=:1
    if [ "$(pgrep -c chrome)" -eq 0 ]; then
      rm -f /home/kasm-user/.config/google-chrome/Singleton* 2>/dev/null
      nohup /opt/google/chrome/google-chrome --no-sandbox --start-maximized \
        --user-data-dir=/home/kasm-user/.config/google-chrome https://www.google.com \
        >/tmp/chrome.log 2>&1 &
    fi
  ' >/dev/null 2>&1 || true
  echo "Up. Open https://localhost:${KASM_PORT}/?resize=remote  (accept the self-signed cert),"
  echo "log in as kasm_user / ${KASM_PW}, then sign into Google in Chrome."
  echo "(?resize=remote = native resolution auto-fitted to your browser window.)"
}

cmd_kasm_down() {
  local eng; eng="$(engine)"
  "$eng" rm -f "$KASM_NAME" >/dev/null 2>&1 && echo "stopped." || echo "not running."
}

case "${1:-}" in
  up)          cmd_up ;;
  forward)     cmd_forward ;;
  chrome)      cmd_chrome ;;
  chrome-down) cmd_chrome_down ;;
  kasm)        cmd_kasm ;;
  kasm-down)   cmd_kasm_down ;;
  open)        open "http://localhost:${WEB_PORT}/?usr=chrome&pwd=neko" 2>/dev/null || echo "Open http://localhost:${WEB_PORT}/?usr=chrome&pwd=neko" ;;
  status)      kubectl --context "$PROFILE" -n "$NS" get pods,svc ;;
  logs)        kubectl --context "$PROFILE" -n "$NS" logs -f "deploy/$RELEASE" ;;
  down)        helm --kube-context "$PROFILE" uninstall "$RELEASE" -n "$NS" && echo "uninstalled (cluster kept; ./run.sh nuke to remove it)" ;;
  nuke)        minikube delete -p "$PROFILE" ;;
  *)           usage ;;
esac
