#!/usr/bin/env bash
set -euo pipefail

# Build pinned Obscura x86_64 binaries in Amazon Linux 2023 container for AWS Lambda
# Upstream repository: https://github.com/h4ckf0r0day/obscura
# Pinned Tag: v0.2.7
# Pinned Commit: 0ecc41770da5c5300227dd83a15bfa1e5100d5d4
# Archive SHA256: 11b53d27cd743e00e196e216920e7f3f794cf378e3f1792b8d25d3beabaf9cad

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BACKEND_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"
TARGET_NAME="al2023"
DEST_BIN_DIR="${BACKEND_DIR}/build/bin"
TARGET_BIN_DIR="${DEST_BIN_DIR}/${TARGET_NAME}"
mkdir -p "${TARGET_BIN_DIR}" "${DEST_BIN_DIR}"

OBSCURA_COMMIT="0ecc41770da5c5300227dd83a15bfa1e5100d5d4"
OBSCURA_TAG="v0.2.7"
ARCHIVE_SHA256="11b53d27cd743e00e196e216920e7f3f794cf378e3f1792b8d25d3beabaf9cad"

echo "Building Obscura ${OBSCURA_TAG} (${OBSCURA_COMMIT}) for AL2023 / Lambda x86_64..."

# Dedicated isolated source and cargo cache directories for AL2023
SRC_DIR="/tmp/obscura-src-al2023"
CARGO_TARGET_CACHE="/tmp/obscura-target-al2023"
mkdir -p "${CARGO_TARGET_CACHE}"

if [ ! -d "${SRC_DIR}" ]; then
  git clone https://github.com/h4ckf0r0day/obscura.git "${SRC_DIR}"
  cd "${SRC_DIR}"
  git checkout "${OBSCURA_COMMIT}"
fi

# 1. Validate pinned source SHA on every build and reject dirty or unexpected checkouts
cd "${SRC_DIR}"
if [ -n "$(git status --porcelain)" ]; then
  echo "Error: Source checkout at ${SRC_DIR} is dirty! Rejecting build." >&2
  echo "Hint: Stash or discard uncommitted changes in ${SRC_DIR} (e.g. 'git -C ${SRC_DIR} stash')." >&2
  exit 1
fi

CURRENT_SHA="$(git rev-parse HEAD)"
if [ "${CURRENT_SHA}" != "${OBSCURA_COMMIT}" ]; then
  echo "Error: Source checkout at ${SRC_DIR} is at ${CURRENT_SHA}, expected pinned SHA ${OBSCURA_COMMIT}! Rejecting build." >&2
  echo "Hint: Checkout the pinned commit via 'git -C ${SRC_DIR} checkout ${OBSCURA_COMMIT}'." >&2
  exit 1
fi

# 2. Build inside clean Amazon Linux 2023 container with separate target cache
docker run --rm \
  -v "${SRC_DIR}:/workspace" \
  -v "${CARGO_TARGET_CACHE}:/cargo-target" \
  -v "${TARGET_BIN_DIR}:/output" \
  -e CARGO_TARGET_DIR=/cargo-target \
  -w /workspace \
  amazonlinux:2023 bash -c "
    set -euo pipefail
    dnf install -y rust cargo clang cmake pkgconfig gcc-c++ openssl-devel
    cargo build --profile release-dist -p obscura-cli --bins --features render
    cp /cargo-target/release-dist/obscura /output/obscura
    cp /cargo-target/release-dist/obscura-worker /output/obscura-worker
    chmod 0755 /output/obscura /output/obscura-worker
    strip --strip-unneeded /output/obscura || true
    strip --strip-unneeded /output/obscura-worker || true
"

# Copy to canonical build/bin destination as active deployment binary
cp "${TARGET_BIN_DIR}/obscura" "${DEST_BIN_DIR}/obscura"
cp "${TARGET_BIN_DIR}/obscura-worker" "${DEST_BIN_DIR}/obscura-worker"
chmod 0755 "${DEST_BIN_DIR}/obscura" "${DEST_BIN_DIR}/obscura-worker"

# 3. Record resulting binary hashes and target provenance
OBSCURA_HASH="$(sha256sum "${TARGET_BIN_DIR}/obscura" | awk '{print $1}')"
OBSCURA_SIZE="$(stat -c %s "${TARGET_BIN_DIR}/obscura")"
WORKER_HASH="$(sha256sum "${TARGET_BIN_DIR}/obscura-worker" | awk '{print $1}')"
WORKER_SIZE="$(stat -c %s "${TARGET_BIN_DIR}/obscura-worker")"
BUILD_TIMESTAMP="$(date -u +"%Y-%m-%dT%H:%M:%SZ")"

PROVENANCE_JSON=$(cat <<EOF
{
  "target": "al2023",
  "target_platform": "x86_64-linux",
  "obscura_version": "${OBSCURA_TAG}",
  "obscura_commit": "${OBSCURA_COMMIT}",
  "upstream_repo": "https://github.com/h4ckf0r0day/obscura.git",
  "built_at": "${BUILD_TIMESTAMP}",
  "binaries": {
    "obscura": {
      "path": "obscura",
      "sha256": "${OBSCURA_HASH}",
      "size_bytes": ${OBSCURA_SIZE}
    },
    "obscura-worker": {
      "path": "obscura-worker",
      "sha256": "${WORKER_HASH}",
      "size_bytes": ${WORKER_SIZE}
    }
  }
}
EOF
)

echo "${PROVENANCE_JSON}" > "${TARGET_BIN_DIR}/provenance.json"
echo "${PROVENANCE_JSON}" > "${DEST_BIN_DIR}/provenance.json"

echo "Build complete for AL2023. Binaries and provenance written to ${TARGET_BIN_DIR} and ${DEST_BIN_DIR}:"
ls -la "${TARGET_BIN_DIR}"
