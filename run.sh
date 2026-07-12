#!/usr/bin/env bash
# chrome-in-a-box — deploy a self-hosted, isolated browser to a *local* minikube
# profile via Helm, then use it from your own browser tab. See README.md.
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

need() { command -v "$1" >/dev/null 2>&1 || { echo "error: '$1' not found on PATH" >&2; exit 1; }; }

usage() {
  cat >&2 <<EOF
Usage: ./run.sh <command>

  up              start the local minikube cluster & 'helm upgrade --install' the chart
                  (BROWSER=google-chrome ./run.sh up  for the Google Chrome flavour)
  forward         port-forward the web UI + WebRTC to localhost (foreground, Ctrl-C to stop)
  open            open http://localhost:${WEB_PORT} in your browser
  status          show pods & service
  logs            follow the browser pod logs
  down            'helm uninstall' the release (keeps the cluster)
  nuke            delete the whole local minikube profile
EOF
  exit 1
}

cmd_up() {
  need minikube; need helm; need kubectl
  if ! minikube status -p "$PROFILE" >/dev/null 2>&1; then
    echo "Starting local minikube profile '$PROFILE' (driver: $DRIVER) ..."
    minikube start -p "$PROFILE" --driver="$DRIVER"
  fi
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
  echo "Open http://localhost:${WEB_PORT}  (login: neko / neko)"
  kubectl --context "$PROFILE" -n "$NS" port-forward "svc/$RELEASE" \
    "${WEB_PORT}:8080" "${TCPMUX_PORT}:8081"
}

case "${1:-}" in
  up)      cmd_up ;;
  forward) cmd_forward ;;
  open)    open "http://localhost:${WEB_PORT}" 2>/dev/null || echo "Open http://localhost:${WEB_PORT}" ;;
  status)  kubectl --context "$PROFILE" -n "$NS" get pods,svc ;;
  logs)    kubectl --context "$PROFILE" -n "$NS" logs -f "deploy/$RELEASE" ;;
  down)    helm --kube-context "$PROFILE" uninstall "$RELEASE" -n "$NS" && echo "uninstalled (cluster kept; ./run.sh nuke to remove it)" ;;
  nuke)    minikube delete -p "$PROFILE" ;;
  *)       usage ;;
esac
