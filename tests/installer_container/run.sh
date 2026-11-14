#!/bin/bash
set -euo pipefail

ROOT=$(cd "$(dirname "$0")" && pwd)
FIXTURES=$(mktemp -d /tmp/codex-installer-fixtures.XXXXXX)
trap 'rm -rf -- "$FIXTURES"' EXIT
/opt/codex-deploy/.venv/bin/python "$ROOT/prepare.py" "$FIXTURES"

docker run --rm -e FIXTURES=/fixtures -v "$ROOT:/work:ro" -v "$FIXTURES:/fixtures:ro" \
    ubuntu:24.04 bash /work/run-inside.sh fresh
docker run --rm -e FIXTURES=/fixtures -v "$ROOT:/work:ro" -v "$FIXTURES:/fixtures:ro" \
    ubuntu:22.04 bash /work/run-inside.sh fresh-py310
docker run --rm -e FIXTURES=/fixtures -v "$ROOT:/work:ro" -v "$FIXTURES:/fixtures:ro" \
    debian:12 bash /work/run-inside.sh existing
docker run --rm -e FIXTURES=/fixtures -v "$ROOT:/work:ro" -v "$FIXTURES:/fixtures:ro" \
    debian:12 bash /work/run-inside.sh bad-signature
docker run --rm -e FIXTURES=/fixtures -v "$ROOT:/work:ro" -v "$FIXTURES:/fixtures:ro" \
    ubuntu:24.04 bash /work/run-inside.sh codex-failure
docker run --rm -e FIXTURES=/fixtures -v "$ROOT:/work:ro" -v "$FIXTURES:/fixtures:ro" \
    ubuntu:24.04 bash /work/run-inside.sh health-failure
docker run --rm -e FIXTURES=/fixtures -v "$ROOT:/work:ro" -v "$FIXTURES:/fixtures:ro" \
    debian:12 bash /work/run-inside.sh proxy
docker run --rm -e FIXTURES=/fixtures -v "$ROOT:/work:ro" -v "$FIXTURES:/fixtures:ro" \
    ubuntu:24.04 bash /work/run-inside.sh invalid-subscription
docker run --rm -e FIXTURES=/fixtures -v "$ROOT:/work:ro" -v "$FIXTURES:/fixtures:ro" \
    debian:12 bash /work/run-inside.sh custom-socks

for mode in partial-none partial-codex partial-claude partial-tavily prompt-disabled prompt-omitted; do
    docker run --rm -e FIXTURES=/fixtures -v "$ROOT:/work:ro" -v "$FIXTURES:/fixtures:ro" \
        ubuntu:22.04 bash /work/run-inside.sh "$mode"
done
