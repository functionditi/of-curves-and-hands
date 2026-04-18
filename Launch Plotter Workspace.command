#!/bin/zsh
set -euo pipefail

REPO_DIR="/Users/akasegaonkar/Documents/GitHub/of-curves-and-hands"
PROJECT_PYTHON="$REPO_DIR/.venv/bin/python"

cd "$REPO_DIR"
if [[ ! -x "$PROJECT_PYTHON" ]]; then
  PROJECT_PYTHON="$(command -v python3)"
fi

exec "$PROJECT_PYTHON" scripts/launch_plotter_workspace.py \
  --replace-existing-bridge \
  --client-display 1 \
  --dashboard-display 0 \
  --no-open-dashboard \
  "$@"
