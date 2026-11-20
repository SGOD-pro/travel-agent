#!/usr/bin/env bash
set -euo pipefail

# Verifies the packaged browser.zip inside an isolated Amazon Linux 2023 container
# with a read-only root filesystem (/), writable /tmp, and zero host PATH dependencies.

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BACKEND_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"
ZIP_PATH="${BACKEND_DIR}/build/browser.zip"

if [ ! -f "${ZIP_PATH}" ]; then
  echo "browser.zip not found at ${ZIP_PATH}. Building artifact first..."
  cd "${BACKEND_DIR}"
  uv run python -m functions.browser.packaging
fi

echo "Verifying ${ZIP_PATH} in clean amazonlinux:2023 container with read-only root and writable /tmp..."

TMP_DIR="$(mktemp -d)"
trap 'rm -rf "${TMP_DIR}"' EXIT

unzip -q "${ZIP_PATH}" -d "${TMP_DIR}/task"

# Build an AL2023 test runner image with Python 3.12 pre-installed
IMAGE_NAME="al2023-test-runner-py312:latest"
if ! docker image inspect "${IMAGE_NAME}" >/dev/null 2>&1; then
  echo "Preparing AL2023 Python 3.12 runner image..."
  docker build -t "${IMAGE_NAME}" - << 'EOF'
FROM amazonlinux:2023
RUN dnf install -y python3.12 glibc && dnf clean all
EOF
fi

docker run --rm \
  --read-only \
  --tmpfs /tmp:rw,exec \
  -v "${TMP_DIR}/task:/var/task:ro" \
  "${IMAGE_NAME}" bash -c "
    set -euo pipefail
    export PYTHONPATH=/var/task
    export LD_LIBRARY_PATH=/var/task/lib
    export OBSCURA_PATH=/var/task/bin/obscura
    export PATH=/usr/bin:/bin
    export HOME=/tmp
    cd /tmp
    python3.12 -S -P -c '
import os, sys, asyncio, json, subprocess, tempfile
from pathlib import Path

# Verify runtime and architecture
assert sys.version_info[:2] == (3, 12)

# Verify Obscura binary in read-only /var/task
obscura_bin = \"/var/task/bin/obscura\"
assert os.path.exists(obscura_bin)
assert os.access(obscura_bin, os.X_OK)

# Verify Playwright bundled node in read-only /var/task
from playwright.async_api import async_playwright
import playwright
node_bin = Path(playwright.__file__).parent / \"driver\" / \"node\"
assert node_bin.exists()
assert os.access(node_bin, os.X_OK)

# Test Obscura serve and dynamic DOM render with read-only root and writable /tmp
async def main():
    with tempfile.NamedTemporaryFile(suffix=\".json\", dir=\"/tmp\", delete=False) as rf:
        rf_path = rf.name

    cmd = [
        obscura_bin,
        \"serve\",
        \"--port\", \"0\",
        \"--host\", \"127.0.0.1\",
        \"--ready-file\", rf_path,
        \"--quiet\",
    ]
    env = os.environ.copy()
    env[\"HOME\"] = \"/tmp\"
    proc = subprocess.Popen(cmd, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        ready = None
        for _ in range(50):
            await asyncio.sleep(0.05)
            if os.path.exists(rf_path) and os.path.getsize(rf_path) > 0:
                with open(rf_path) as f:
                    ready = json.load(f)
                break
        assert ready is not None, \"Obscura failed to publish ready file\"
        assert ready.get(\"host\") == \"127.0.0.1\"

        async with async_playwright() as p:
            cdp_port = ready[\"port\"]
            browser = await p.chromium.connect_over_cdp(f\"http://127.0.0.1:{cdp_port}\")
            page = await browser.new_page()
            await page.set_content(
                \"<html><head><title>Clean Runtime Proof</title></head>\"
                \"<body><div id=\\\"content\\\">Unrendered</div>\"
                \"<script>document.getElementById(\\\"content\\\").innerText = \\\"AL2023_READONLY_ROOT_SUCCESS\\\";</script>\"
                \"</body></html>\"
            )
            text = await page.inner_text(\"#content\")
            title = await page.title()
            await browser.close()
            assert text == \"AL2023_READONLY_ROOT_SUCCESS\"
            assert title == \"Clean Runtime Proof\"
            print(json.dumps({\"status\": \"VERIFIED\", \"text\": text, \"title\": title}))
    finally:
        proc.kill()
        proc.wait()
        if os.path.exists(rf_path):
            os.unlink(rf_path)

asyncio.run(main())
'
"
echo "Clean runtime proof in Amazon Linux 2023 with read-only root and writable /tmp PASSED!"
