#!/usr/bin/env bash
# One numbered ChArUco board per tank; reuses the checkout's Python environment and dependency setup.
set -euo pipefail

PROJECT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
exec "$PROJECT_DIR/generate_charuco.sh" tanks "$@"
