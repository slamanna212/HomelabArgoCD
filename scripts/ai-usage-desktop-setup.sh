#!/usr/bin/env bash
# ai-usage-desktop-setup.sh
# One-shot setup for the AI allowance/usage collector on Sam's desktop.
#
# Installs:
#   - CodexBar CLI (codexbar) -> ~/.local/bin/codexbar
#   - config               -> ~/.config/codexbar/config.json (prompts for keys)
#   - exporter             -> ~/.config/ai-usage-exporter/exporter.py
#   - service              -> launchd agent (macOS) or systemd user unit (Linux)
#
# The collector runs where Claude Code / Codex are actually used, so the
# OAuth credentials in ~/.claude/.credentials.json and ~/.codex/auth.json are
# refreshed by normal CLI usage — no manual token rotation, ever.
#
# Usage: bash scripts/ai-usage-desktop-setup.sh
set -euo pipefail

VERSION="v0.49.3"
EXPORTER_SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/ai-usage-exporter.py"

# --- Detect OS + arch -> release asset -------------------------------------
OS="$(uname -s)"
ARCH="$(uname -m)"
case "${OS}:${ARCH}" in
  Darwin:arm64)  ASSET="CodexBarCLI-${VERSION}-macos-arm64.tar.gz" ;;
  Darwin:x86_64) ASSET="CodexBarCLI-${VERSION}-macos-x86_64.tar.gz" ;;
  Linux:x86_64)  ASSET="CodexBarCLI-${VERSION}-linux-musl-x86_64.tar.gz" ;;
  Linux:aarch64) ASSET="CodexBarCLI-${VERSION}-linux-musl-aarch64.tar.gz" ;;
  *) echo "Unsupported platform: ${OS}/${ARCH}" >&2; exit 1 ;;
esac

BIN_DIR="${HOME}/.local/bin"
BIN="${BIN_DIR}/codexbar"
CFG_DIR="${HOME}/.config/codexbar"
CFG="${CFG_DIR}/config.json"
EXP_DIR="${HOME}/.config/ai-usage-exporter"
EXP="${EXP_DIR}/exporter.py"
BASE_URL="https://github.com/steipete/CodexBar/releases/download/${VERSION}"

echo "==> Installing codexbar ${VERSION} (${OS}/${ARCH})"
mkdir -p "${BIN_DIR}" "${CFG_DIR}" "${EXP_DIR}"
TMP="$(mktemp -d)"
trap 'rm -rf "${TMP}"' EXIT

if command -v curl >/dev/null; then
  curl -fsSL -o "${TMP}/${ASSET}" "${BASE_URL}/${ASSET}"
  curl -fsSL -o "${TMP}/${ASSET}.sha256" "${BASE_URL}/${ASSET}.sha256"
else
  wget -q -O "${TMP}/${ASSET}" "${BASE_URL}/${ASSET}"
  wget -q -O "${TMP}/${ASSET}.sha256" "${BASE_URL}/${ASSET}.sha256"
fi

# Verify checksum (sha256 file contains "<hash>  <path>").
EXPECTED="$(awk '{print $1}' "${TMP}/${ASSET}.sha256")"
if command -v sha256sum >/dev/null; then
  ACTUAL="$(sha256sum "${TMP}/${ASSET}" | awk '{print $1}')"
elif command -v shasum >/dev/null; then
  ACTUAL="$(shasum -a 256 "${TMP}/${ASSET}" | awk '{print $1}')"
else
  echo "!! No sha256 tool found; skipping verification" >&2
  ACTUAL="${EXPECTED}"
fi
if [[ "${ACTUAL}" != "${EXPECTED}" ]]; then
  echo "Checksum mismatch! Expected ${EXPECTED}, got ${ACTUAL}" >&2
  exit 1
fi
echo "   checksum OK"

tar -xzf "${TMP}/${ASSET}" -C "${TMP}"
# Tarball contains the binary (symlinked as codexbar) at the root.
BINARY_SRC="$(find "${TMP}" -maxdepth 2 -name 'CodexBarCLI' -o -maxdepth 2 -name 'codexbar' | head -1)"
install -m 0755 "${BINARY_SRC}" "${BIN}"
echo "   installed to ${BIN}"

# --- Config ----------------------------------------------------------------
if [[ ! -f "${CFG}" ]]; then
  echo "==> Configuring providers (first run only)"
  read -r -p "OpenRouter API key (sk-or-...): " OR_KEY
  read -r -p "DeepSeek API key (sk-...): " DS_KEY
  cat > "${CFG}" <<EOF
{
  "version": 1,
  "providers": [
    {"id": "claude", "enabled": true, "source": "oauth"},
    {"id": "codex", "enabled": true, "source": "oauth"},
    {"id": "openrouter", "enabled": true, "source": "api", "apiKey": "${OR_KEY}"},
    {"id": "deepseek", "enabled": true, "source": "api", "apiKey": "${DS_KEY}"}
  ]
}
EOF
  chmod 600 "${CFG}"
  echo "   wrote ${CFG} (0600)"
else
  echo "==> ${CFG} exists; leaving it untouched"
fi

# --- Exporter --------------------------------------------------------------
if [[ ! -f "${EXP}" ]]; then
  cp "${EXPORTER_SRC}" "${EXP}"
  chmod 755 "${EXP}"
fi

# prometheus_client dependency
if ! python3 -c 'import prometheus_client' 2>/dev/null; then
  echo "==> Installing prometheus_client"
  python3 -m pip install --user --quiet prometheus_client
fi
PYTHON="$(command -v python3)"

# --- Service ---------------------------------------------------------------
echo "==> Installing service"
if [[ "${OS}" == "Darwin" ]]; then
  PLIST="${HOME}/Library/LaunchAgents/com.hermes.ai-usage-exporter.plist"
  mkdir -p "$(dirname "${PLIST}")"
  cat > "${PLIST}" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>com.hermes.ai-usage-exporter</string>
  <key>ProgramArguments</key>
  <array>
    <string>${PYTHON}</string>
    <string>${EXP}</string>
  </array>
  <key>EnvironmentVariables</key>
  <dict>
    <key>CODEXBAR_BIN</key><string>${BIN}</string>
    <key>CODEXBAR_CONFIG</key><string>${CFG}</string>
    <key>HOME</key><string>${HOME}</string>
  </dict>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>StandardOutPath</key><string>${EXP_DIR}/exporter.log</string>
  <key>StandardErrorPath</key><string>${EXP_DIR}/exporter.err.log</string>
</dict>
</plist>
EOF
  launchctl unload "${PLIST}" 2>/dev/null || true
  launchctl load "${PLIST}"
  echo "   launchd agent loaded: ${PLIST}"
else
  UNIT="${HOME}/.config/systemd/user/ai-usage-exporter.service"
  mkdir -p "$(dirname "${UNIT}")"
  cat > "${UNIT}" <<EOF
[Unit]
Description=AI allowance/usage exporter (codexbar)
After=network-online.target

[Service]
ExecStart=${PYTHON} ${EXP}
Environment=CODEXBAR_BIN=${BIN}
Environment=CODEXBAR_CONFIG=${CFG}
Environment=HOME=${HOME}
Restart=always
RestartSec=10

[Install]
WantedBy=default.target
EOF
  systemctl --user daemon-reload
  systemctl --user enable --now ai-usage-exporter.service
  echo "   systemd user service enabled: ai-usage-exporter"
fi

echo
echo "==> Done. Exporter serving on :9100"
echo "    Test: curl -s http://localhost:9100/metrics | head"
echo "    Note: allow inbound TCP 9100 from the cluster nodes (kube-prometheus"
echo "          scrapes this host directly). See HomelabArgoCD PR for the"
echo "          scrape config / desktop IP."
