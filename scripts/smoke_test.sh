#!/bin/sh
# Thin wrapper around `kya-agent smoke-test`.
# CI-safe: never signs, never POSTs to KYA — just exercises the
# typed-data builder and reachability checks.
set -e

INSTALL_DIR="${INSTALL_DIR:-$HOME/.local/bin}"
export PATH="$INSTALL_DIR:$PATH"

if ! command -v kya-agent >/dev/null 2>&1; then
  TMP="$(mktemp)"
  URL="https://raw.githubusercontent.com/GhostClaw-dev/kya-skill/main/install.sh"
  if   command -v curl    >/dev/null 2>&1; then curl -fsSL -o "$TMP" "$URL"
  elif command -v wget    >/dev/null 2>&1; then wget -qO  "$TMP" "$URL"
  else
    echo "need curl or wget to fetch kya-agent" >&2
    exit 1
  fi
  INSTALL_DIR="$INSTALL_DIR" sh "$TMP"
  rm -f "$TMP"
fi

exec kya-agent smoke-test "$@"
