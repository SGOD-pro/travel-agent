#!/usr/bin/env bash
set -euo pipefail

# Build pinned Obscura x86_64 binaries natively on Arch Linux / WSL host
# Upstream repository: https://github.com/h4ckf0r0day/obscura
# Pinned Tag: v0.2.7
# Pinned Commit: 0ecc41770da5c5300227dd83a15bfa1e5100d5d4

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BACKEND_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"
DEST_BIN_DIR="${BACKEND_DIR}/build/bin"
mkdir -p "${DEST_BIN_DIR}"

OBSCURA_COMMIT="0ecc41770da5c5300227dd83a15bfa1e5100d5d4"
OBSCURA_TAG="v0.2.7"

echo "Building Obscura ${OBSCURA_TAG} (${OBSCURA_COMMIT}) for Arch Linux..."

SRC_DIR="/tmp/obscura-src"
if [ ! -d "${SRC_DIR}" ]; then
  git clone https://github.com/h4ckf0r0day/obscura.git "${SRC_DIR}"
  cd "${SRC_DIR}"
  git checkout "${OBSCURA_COMMIT}"
fi

cd "${SRC_DIR}"
cargo build --profile release-dist -p obscura-cli --bins --features render
cp target/release-dist/obscura "${DEST_BIN_DIR}/obscura"
cp target/release-dist/obscura-worker "${DEST_BIN_DIR}/obscura-worker"
chmod 0755 "${DEST_BIN_DIR}/obscura" "${DEST_BIN_DIR}/obscura-worker"

echo "Build complete. Binaries written to ${DEST_BIN_DIR}:"
ls -la "${DEST_BIN_DIR}"
