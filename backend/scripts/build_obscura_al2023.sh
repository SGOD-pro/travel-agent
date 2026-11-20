#!/usr/bin/env bash
set -euo pipefail

# Build pinned Obscura x86_64 binaries in Amazon Linux 2023 container for AWS Lambda
# Upstream repository: https://github.com/h4ckf0r0day/obscura
# Pinned Tag: v0.2.7
# Pinned Commit: 0ecc41770da5c5300227dd83a15bfa1e5100d5d4
# Archive SHA256: 11b53d27cd743e00e196e216920e7f3f794cf378e3f1792b8d25d3beabaf9cad

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BACKEND_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"
DEST_BIN_DIR="${BACKEND_DIR}/build/bin"
mkdir -p "${DEST_BIN_DIR}"

OBSCURA_COMMIT="0ecc41770da5c5300227dd83a15bfa1e5100d5d4"
OBSCURA_TAG="v0.2.7"
ARCHIVE_SHA256="11b53d27cd743e00e196e216920e7f3f794cf378e3f1792b8d25d3beabaf9cad"

echo "Building Obscura ${OBSCURA_TAG} (${OBSCURA_COMMIT}) for AL2023 / Lambda x86_64..."

SRC_DIR="/tmp/obscura-src"
if [ ! -d "${SRC_DIR}" ]; then
  git clone https://github.com/h4ckf0r0day/obscura.git "${SRC_DIR}"
  cd "${SRC_DIR}"
  git checkout "${OBSCURA_COMMIT}"
fi

docker run --rm \
  -v "${SRC_DIR}:/workspace" \
  -v "${DEST_BIN_DIR}:/output" \
  -w /workspace \
  amazonlinux:2023 bash -c "
    set -euo pipefail
    dnf install -y rust cargo clang cmake pkgconfig gcc-c++ openssl-devel
    cargo build --profile release-dist -p obscura-cli --bins --features render
    cp target/release-dist/obscura /output/obscura
    cp target/release-dist/obscura-worker /output/obscura-worker
    chmod 0755 /output/obscura /output/obscura-worker
    strip --strip-unneeded /output/obscura || true
    strip --strip-unneeded /output/obscura-worker || true
"

echo "Build complete. Binaries written to ${DEST_BIN_DIR}:"
ls -la "${DEST_BIN_DIR}"
