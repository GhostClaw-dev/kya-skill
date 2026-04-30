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

# Intel Mac (x86_64-apple-darwin) is not in the prebuilt matrix —
# macos-13 GitHub runners are too slow / queued for reliable releases,
# and the awp / kya user base is overwhelmingly Linux-in-Docker plus
# Apple Silicon Macs. Surface a clean message instead of 404'ing on the
# release artifact lookup.
if [ "${OS_NAME}" = "darwin" ] && [ "${ARCH_NAME}" = "x86_64" ]; then
  echo "Error: prebuilt kya-agent for Intel Mac (x86_64) is not published." >&2
  echo "" >&2
  echo "Build it locally with Rust:" >&2
  echo "  cargo install --git https://github.com/${REPO}" >&2
  echo "Or run from inside Docker (Linux x86_64 image works on Intel Mac too)." >&2
  exit 1
fi

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

# Download helper. Tries curl → wget → python3 → node so the script
# works on minimal sandboxes that ship only one of these (Hermes
# Telegram / Discord runners often lack curl). All four paths follow
# HTTPS 302 redirects (GitHub release artifacts always redirect).
download() {
  url="$1"
  out="$2"
  if   command -v curl    >/dev/null 2>&1; then
    curl -fsSL -o "${out}" "${url}"
  elif command -v wget    >/dev/null 2>&1; then
    wget -qO "${out}" "${url}"
  elif command -v python3 >/dev/null 2>&1; then
    python3 -c "import sys, urllib.request; urllib.request.urlretrieve(sys.argv[1], sys.argv[2])" "${url}" "${out}"
  elif command -v node    >/dev/null 2>&1; then
    # Node fallback. Uses https (not http) and follows up to 5 redirects.
    node -e "
      const https = require('https');
      const fs = require('fs');
      const url = require('url');
      const get = (u, redirects = 5) => {
        https.get(u, (res) => {
          if (res.statusCode >= 300 && res.statusCode < 400 && res.headers.location) {
            if (redirects <= 0) { console.error('too many redirects'); process.exit(1); }
            res.resume();
            get(url.resolve(u, res.headers.location), redirects - 1);
            return;
          }
          if (res.statusCode !== 200) { console.error('HTTP ' + res.statusCode); process.exit(1); }
          const f = fs.createWriteStream(process.argv[2]);
          res.pipe(f);
          f.on('finish', () => f.close());
        }).on('error', (e) => { console.error(e.message); process.exit(1); });
      };
      get(process.argv[1]);
    " "${url}" "${out}"
  else
    echo "Error: need curl, wget, python3, or node to fetch kya-agent" >&2
    return 1
  fi
}

# Resolve latest release tag (GitHub API returns JSON; grep out tag_name).
echo "Fetching latest release..."
META_TMP=$(mktemp)
if ! download "https://api.github.com/repos/${REPO}/releases/latest" "${META_TMP}"; then
  rm -f "${META_TMP}"
  echo "Error: could not reach GitHub API" >&2
  exit 1
fi
LATEST=$(grep '"tag_name"' "${META_TMP}" | head -1 | sed 's/.*: "\(.*\)".*/\1/')
rm -f "${META_TMP}"

if [ -z "${LATEST}" ]; then
  echo "Error: could not find latest release. Check https://github.com/${REPO}/releases" >&2
  exit 1
fi

URL="https://github.com/${REPO}/releases/download/${LATEST}/${ASSET}"
echo "Downloading kya-agent ${LATEST} for ${OS_NAME}/${ARCH_NAME}..."
echo "  ${URL}"

TMPFILE=$(mktemp)
if ! download "${URL}" "${TMPFILE}"; then
  rm -f "${TMPFILE}"
  echo "Error: download failed" >&2
  echo "Available at: https://github.com/${REPO}/releases/tag/${LATEST}" >&2
  exit 1
fi
# Sanity check: zero-byte download is the symptom we're guarding against.
if [ ! -s "${TMPFILE}" ]; then
  rm -f "${TMPFILE}"
  echo "Error: downloaded file is empty (likely a redirect that wasn't followed)" >&2
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
