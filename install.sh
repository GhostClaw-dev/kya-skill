#!/bin/sh
# Install kya-agent binary from GitHub Releases.
# Usage: curl -fsSL https://raw.githubusercontent.com/GhostClaw-dev/kya-skill/main/install.sh | sh
set -e

REPO="GhostClaw-dev/kya-skill"
INSTALL_DIR="${INSTALL_DIR:-$HOME/.local/bin}"

OS="$(uname -s)"
ARCH="$(uname -m)"

case "${OS}" in
  Linux)   OS_NAME="linux" ;;
  Darwin)  OS_NAME="darwin" ;;
  MINGW*|MSYS*|CYGWIN*) OS_NAME="windows" ;;
  *)       echo "Error: unsupported OS: ${OS}" >&2; exit 1 ;;
esac

case "${ARCH}" in
  x86_64|amd64)   ARCH_NAME="x86_64" ;;
  aarch64|arm64)  ARCH_NAME="aarch64" ;;
  *)              echo "Error: unsupported architecture: ${ARCH}" >&2; exit 1 ;;
esac

# Linux x86_64 prefers musl (static) build — avoids glibc version skew.
if [ "${OS_NAME}" = "linux" ] && [ "${ARCH_NAME}" = "x86_64" ]; then
  ASSET="kya-agent-linux-x86_64-musl"
elif [ "${OS_NAME}" = "linux" ] && [ "${ARCH_NAME}" = "aarch64" ]; then
  ASSET="kya-agent-linux-aarch64-musl"
elif [ "${OS_NAME}" = "windows" ]; then
  ASSET="kya-agent-windows-${ARCH_NAME}.exe"
else
  ASSET="kya-agent-${OS_NAME}-${ARCH_NAME}"
fi

# Resolve latest release tag.
echo "Fetching latest release..."
LATEST=$(curl -fsSL -H "Accept: application/vnd.github+json" \
  "https://api.github.com/repos/${REPO}/releases/latest" \
  | grep '"tag_name"' | head -1 | sed 's/.*: "\(.*\)".*/\1/')

if [ -z "${LATEST}" ]; then
  echo "Error: could not find latest release. Check https://github.com/${REPO}/releases" >&2
  exit 1
fi

URL="https://github.com/${REPO}/releases/download/${LATEST}/${ASSET}"
echo "Downloading kya-agent ${LATEST} for ${OS_NAME}/${ARCH_NAME}..."
echo "  ${URL}"

TMPFILE=$(mktemp)
HTTP_CODE=$(curl -sSL -w "%{http_code}" -o "${TMPFILE}" "${URL}")
if [ "${HTTP_CODE}" != "200" ]; then
  rm -f "${TMPFILE}"
  echo "Error: download failed (HTTP ${HTTP_CODE})" >&2
  echo "Available at: https://github.com/${REPO}/releases/tag/${LATEST}" >&2
  exit 1
fi

chmod +x "${TMPFILE}"

mkdir -p "${INSTALL_DIR}"

TARGET="${INSTALL_DIR}/kya-agent"
if [ "${OS_NAME}" = "windows" ]; then
  TARGET="${TARGET}.exe"
fi

if [ -w "${INSTALL_DIR}" ]; then
  mv "${TMPFILE}" "${TARGET}"
else
  echo "Installing to ${INSTALL_DIR} (requires sudo)..."
  sudo mv "${TMPFILE}" "${TARGET}"
fi

# macOS: strip Gatekeeper quarantine.
if [ "${OS_NAME}" = "darwin" ]; then
  xattr -d com.apple.quarantine "${TARGET}" 2>/dev/null || true
fi

echo ""
echo "kya-agent ${LATEST} installed to ${TARGET}"

case ":${PATH}:" in
  *":${INSTALL_DIR}:"*) ;;
  *)
    echo ""
    echo "NOTE: ${INSTALL_DIR} is not on your PATH. Add it with:"
    echo "  export PATH=\"${INSTALL_DIR}:\$PATH\""
    ;;
esac

echo ""
echo "Verify: kya-agent --version"
echo "Next:   kya-agent preflight"
