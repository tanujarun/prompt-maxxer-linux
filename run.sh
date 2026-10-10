#!/usr/bin/env bash
# Run the build in this checkout, without installing it.
#   ./run.sh              the shell, which starts the engine
#   ./run.sh --engine     only the engine, in the foreground with its log
set -euo pipefail
ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
export CARGO_TARGET_DIR=${CARGO_TARGET_DIR:-${XDG_CACHE_HOME:-$HOME/.cache}/prompt-maxxer/target}
VENV=${XDG_DATA_HOME:-$HOME/.local/share}/prompt-maxxer/venv

if [[ ${1:-} == --engine ]]; then
  shift
  exec "$VENV/bin/python" -m prompt_maxxer -v "$@"
fi
[[ -x $CARGO_TARGET_DIR/release/prompt-maxxer ]] || { echo "Not built yet: run ./install.sh --no-system"; exit 1; }
exec "$CARGO_TARGET_DIR/release/prompt-maxxer" "$@"
