#!/usr/bin/env bash
# Install Prompt Maxxer on Arch / Omarchy: the engine's Python environment, the
# shell, and the few system settings that reading the keyboard needs.
#
#   ./install.sh                 everything
#   ./install.sh --system-only   just the steps that need sudo
#   ./install.sh --no-system     everything else (re-run after an update)
#
# Nothing is installed system-wide except what is listed in system_setup, and
# every command in it is printed before it runs. Everything else lives in your
# home directory:
#   ~/.local/share/prompt-maxxer/venv   the engine's Python environment
#   ~/.cache/prompt-maxxer/target       build output (kept off this folder: it
#                                       may be on a slow or non-Unix filesystem)
#   ~/.local/bin                        prompt-maxxer, prompt-maxxer-engine
set -euo pipefail

ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
DATA=${XDG_DATA_HOME:-$HOME/.local/share}
VENV=$DATA/prompt-maxxer/venv
export CARGO_TARGET_DIR=${CARGO_TARGET_DIR:-${XDG_CACHE_HOME:-$HOME/.cache}/prompt-maxxer/target}
BIN=$HOME/.local/bin
PNPM=(npx --yes pnpm@10.34.6)

PACKAGES=(webkit2gtk-4.1 gtk3 gtk-layer-shell libayatana-appindicator portaudio wtype wl-clipboard
          base-devel linux-api-headers nodejs npm rust)

say() { printf '\n\033[1m==> %s\033[0m\n' "$*"; }

system_setup() {
  say "System setup (needs sudo)"

  local missing=()
  for pkg in "${PACKAGES[@]}"; do
    pacman -Qq "$pkg" >/dev/null 2>&1 || missing+=("$pkg")
  done
  # node and rust may come from mise/rustup instead of pacman.
  local still=()
  for pkg in "${missing[@]}"; do
    case $pkg in
      nodejs|npm) command -v npm >/dev/null || still+=("$pkg") ;;
      rust)       command -v cargo >/dev/null || still+=("$pkg") ;;
      *)          still+=("$pkg") ;;
    esac
  done
  if ((${#still[@]})); then
    echo "Missing packages: ${still[*]}"
    ( set -x; sudo pacman -S --needed "${still[@]}" )
  else
    echo "All required packages are installed."
  fi

  echo
  echo "Prompt Maxxer reads your keyboard from /dev/input, because Wayland gives an"
  echo "application no other way to see a key pressed in another window. That needs your"
  echo "user in the 'input' group. Be aware that this lets any program you run read"
  echo "keystrokes, not only this one."
  if id -nG "$USER" | tr ' ' '\n' | grep -qx input; then
    echo "$USER is already in the input group."
  else
    ( set -x; sudo usermod -aG input "$USER" )
    NEED_RELOGIN=1
  fi

  ( set -x
    sudo install -Dm644 "$ROOT/packaging/60-prompt-maxxer-uinput.rules" /etc/udev/rules.d/60-prompt-maxxer-uinput.rules
    sudo install -Dm644 "$ROOT/packaging/prompt-maxxer-uinput.conf" /etc/modules-load.d/prompt-maxxer-uinput.conf
    sudo modprobe uinput
    sudo udevadm control --reload
    sudo udevadm trigger --name-match=uinput )
}

python_env() {
  say "Engine environment ($VENV)"
  local py=${PYTHON:-python3}
  "$py" -c 'import sys; sys.exit(sys.version_info < (3, 10))' ||
    { echo "Python 3.10 or newer is needed."; exit 1; }
  [[ -x $VENV/bin/python ]] || "$py" -m venv "$VENV"
  "$VENV/bin/pip" install --quiet --upgrade pip
  # Every dependency at the exact version it was tested with. The CUDA
  # libraries come from NVIDIA's wheels, because the system's CUDA is a different
  # major version from the one CTranslate2 is built against.
  local extra=""
  command -v nvidia-smi >/dev/null && extra="[cuda]"
  "$VENV/bin/pip" install -c "$ROOT/engine/requirements.lock" -e "$ROOT/engine$extra"
}

shell_build() {
  say "Shell"
  ( cd "$ROOT/shell"
    "${PNPM[@]}" install --frozen-lockfile
    "${PNPM[@]}" tauri build --no-bundle )

  mkdir -p "$BIN" "$DATA/applications" "$DATA/icons/hicolor/128x128/apps"
  install -Dm755 "$CARGO_TARGET_DIR/release/prompt-maxxer" "$BIN/prompt-maxxer"
  install -Dm644 "$ROOT/shell/src-tauri/icons/128x128.png" "$DATA/icons/hicolor/128x128/apps/prompt-maxxer.png"
  sed "s|@BIN@|$BIN|" "$ROOT/packaging/prompt-maxxer.desktop" > "$DATA/applications/prompt-maxxer.desktop"
  # `prompt-maxxer-engine --ptt press|release` talks to the running engine.
  printf '#!/bin/sh\nexec "%s/bin/python" -m prompt_maxxer "$@"\n' "$VENV" > "$BIN/prompt-maxxer-engine"
  chmod +x "$BIN/prompt-maxxer-engine"
  command -v update-desktop-database >/dev/null && update-desktop-database "$DATA/applications" || true
}

NEED_RELOGIN=0
case "${1:-}" in
  --system-only) system_setup ;;
  --no-system)   python_env; shell_build ;;
  "")            system_setup; python_env; shell_build ;;
  *) echo "usage: $0 [--system-only | --no-system]"; exit 2 ;;
esac

say "Done"
if ((NEED_RELOGIN)); then
  echo "Log out and back in once, so the new 'input' group takes effect."
fi
case ":$PATH:" in *":$BIN:"*) ;; *) echo "Note: $BIN is not on your PATH." ;; esac
echo "Start it with: prompt-maxxer   (or run.sh from this folder)"
