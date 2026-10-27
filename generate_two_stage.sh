#!/usr/bin/env bash
# Complete two-stage tank marker set; reuses the checkout's Python environment and dependency setup.
set -euo pipefail

PROJECT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
exec "$PROJECT_DIR/generate_charuco.sh" two-stage "$@"
