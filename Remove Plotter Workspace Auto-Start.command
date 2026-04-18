#!/bin/zsh
set -euo pipefail

REPO_DIR="/Users/akasegaonkar/Documents/GitHub/of-curves-and-hands"

cd "$REPO_DIR"
exec python3 scripts/uninstall_plotter_autostart.py "$@"
