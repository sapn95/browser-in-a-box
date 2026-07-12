#!/usr/bin/env bash
# chrome-in-a-box — self-hosted, isolated browser you use from your own browser tab.
#
# Two paths, because Chromium and Google Chrome have different constraints:
#   * up/forward  — Chromium (arm64-native, fast) via Helm on a *local* minikube profile.
#   * chrome      — Google Chrome (amd64-only, emulated on ARM) directly via podman/docker,
#                   because it needs Google's password sync and can't run as an arm64 pod.
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

# Google Chrome (amd64) path — runs directly under the container engine, emulated on
# Apple Silicon. Not a Kubernetes workload: k8s resolves images to the arm64 node, which
# the amd64-only Google Chrome image cannot satisfy.
CHROME_NAME="cib-chrome"
CHROME_IMAGE="ghcr.io/m1k1o/neko/google-chrome:latest"
CHROME_VOLUME="cib-chrome-profile"

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
  chrome          run Google Chrome (build-free; pulls the upstream image)
  chrome-down     stop & remove the Google Chrome container

  open            open http://localhost:${WEB_PORT} in your browser (works for either path)
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
  echo "Open http://localhost:${WEB_PORT}  (no login)"
  kubectl --context "$PROFILE" -n "$NS" port-forward "svc/$RELEASE" \
    "${WEB_PORT}:8080" "${TCPMUX_PORT}:8081"
}

cmd_chrome() {
  local eng; eng="$(engine)"
  echo "Starting Google Chrome (amd64, emulated) ..."
  "$eng" rm -f "$CHROME_NAME" >/dev/null 2>&1 || true
  "$eng" run -d --name "$CHROME_NAME" \
    --platform linux/amd64 \
    --shm-size=2g --cap-add=SYS_ADMIN --security-opt seccomp=unconfined \
    -p "127.0.0.1:${WEB_PORT}:8080" \
    -p "127.0.0.1:${TCPMUX_PORT}:${TCPMUX_PORT}" \
    -e "NEKO_WEBRTC_TCPMUX=${TCPMUX_PORT}" \
    -e NEKO_WEBRTC_NAT1TO1=127.0.0.1 \
    -e NEKO_WEBRTC_ICELITE=1 \
    -e NEKO_MEMBER_PROVIDER=noauth \
    -v "${CHROME_VOLUME}:/home/neko" \
    "$CHROME_IMAGE" >/dev/null
  echo "Up. Open http://localhost:${WEB_PORT}  (no login), then sign into Google."
  echo "amd64/emulated → slower than native. Stop with: ./run.sh chrome-down"
}

cmd_chrome_down() {
  local eng; eng="$(engine)"
  "$eng" rm -f "$CHROME_NAME" >/dev/null 2>&1 && echo "stopped." || echo "not running."
}

case "${1:-}" in
  up)          cmd_up ;;
  forward)     cmd_forward ;;
  chrome)      cmd_chrome ;;
  chrome-down) cmd_chrome_down ;;
  open)        open "http://localhost:${WEB_PORT}" 2>/dev/null || echo "Open http://localhost:${WEB_PORT}" ;;
  status)      kubectl --context "$PROFILE" -n "$NS" get pods,svc ;;
  logs)        kubectl --context "$PROFILE" -n "$NS" logs -f "deploy/$RELEASE" ;;
  down)        helm --kube-context "$PROFILE" uninstall "$RELEASE" -n "$NS" && echo "uninstalled (cluster kept; ./run.sh nuke to remove it)" ;;
  nuke)        minikube delete -p "$PROFILE" ;;
  *)           usage ;;
esac
