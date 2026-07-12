# syntax=docker/dockerfile:1
#
# chrome-in-a-box — a thin layer over Neko (https://github.com/m1k1o/neko),
# a self-hosted virtual browser you drive straight from your own browser tab.
#
# BROWSER selects the Neko flavour:
#   chromium       -> arm64-native, fast, open source (default)
#   google-chrome  -> amd64 only; adds Google account sync
ARG BROWSER=chromium
FROM ghcr.io/m1k1o/neko/${BROWSER}:latest

LABEL org.opencontainers.image.title="chrome-in-a-box" \
      org.opencontainers.image.description="Self-hosted, isolated browser you use from your own browser (Neko-based)." \
      org.opencontainers.image.source="https://github.com/sapn95/chrome-in-a-box" \
      org.opencontainers.image.licenses="MIT"

# Neko's entrypoint (s6-overlay) starts as root and drops privileges itself.
USER root

# Recommended (user-overridable) defaults: keep the built-in password manager and
# autofill on, and restore the last session at start. These are *recommended*
# policies, not managed ones — nothing is locked and no "managed" banner appears.
COPY policies/recommended.json /etc/chromium/policies/recommended/chrome-in-a-box.json
COPY policies/recommended.json /etc/opt/chrome/policies/recommended/chrome-in-a-box.json

# Google Chrome is amd64-only, so on Apple Silicon it runs emulated. Under plain QEMU
# user-mode emulation, multi-process Chrome hits syscalls QEMU doesn't implement
# (ptrace/prctl) and crash-loops into a black screen; --single-process avoids the
# child processes. Under Rosetta (or native amd64) it's not needed, so run.sh passes
# SINGLE_PROCESS=false there to get faster multi-process Chrome. See the README.
ARG BROWSER
ARG SINGLE_PROCESS=true
RUN if [ "$BROWSER" = "google-chrome" ] && [ "$SINGLE_PROCESS" = "true" ]; then \
      grep -q -- '--single-process' /etc/neko/supervisord/google-chrome.conf || \
      sed -i '/--no-sandbox/a\  --single-process' /etc/neko/supervisord/google-chrome.conf; \
    fi
