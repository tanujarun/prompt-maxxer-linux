#!/usr/bin/env bash
# Remove what install.sh put in your home directory. Settings, logs and the
# downloaded speech models are left alone; --system also removes the udev rule
# and the module-load entry (not the 'input' group membership, which other
# tools may rely on).
set -euo pipefail
DATA=${XDG_DATA_HOME:-$HOME/.local/share}
CACHE=${XDG_CACHE_HOME:-$HOME/.cache}

pkill -x prompt-maxxer 2>/dev/null || true
rm -rf "$DATA/prompt-maxxer" "$CACHE/prompt-maxxer"
rm -f "$HOME/.local/bin/prompt-maxxer" "$HOME/.local/bin/prompt-maxxer-engine" \
      "$DATA/applications/prompt-maxxer.desktop" \
      "$DATA/icons/hicolor/128x128/apps/prompt-maxxer.png" \
      "${XDG_CONFIG_HOME:-$HOME/.config}/autostart/prompt-maxxer.desktop"
if [[ ${1:-} == --system ]]; then
  ( set -x; sudo rm -f /etc/udev/rules.d/60-prompt-maxxer-uinput.rules /etc/modules-load.d/prompt-maxxer-uinput.conf )
fi
echo "Removed. Left in place: ~/.config/prompt-maxxer, ~/.local/state/prompt-maxxer, ~/.cache/huggingface"
