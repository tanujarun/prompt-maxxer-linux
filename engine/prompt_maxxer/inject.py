"""Text injection into whatever window currently has focus, on Wayland.

Two strategies, because neither is good at both jobs:

* `wtype` types real keystrokes through the compositor's virtual-keyboard
  protocol. It works in essentially every window, needs no clipboard, and fires
  the input events editors and web inputs react to - but it costs roughly a
  millisecond per character, so a long paragraph visibly types itself out.
* The clipboard plus a paste shortcut is O(1) regardless of length, but it
  clobbers whatever the user had copied, so we save and restore it.

Short text takes the first path, long text the second.

Wayland forbids one client from injecting into another, so both go through
tools that speak protocols the compositor chooses to offer: `wtype`
(zwp_virtual_keyboard) and `wl-clipboard` (wlr-data-control). Hyprland offers
both. The paste shortcut differs by window - a terminal wants Ctrl+Shift+V,
everything else Ctrl+V - so "auto" asks Hyprland which window has focus.
"""

from __future__ import annotations

import json
import logging
import re
import shutil
import subprocess
import threading

log = logging.getLogger(__name__)

#: Window classes whose paste shortcut is Ctrl+Shift+V.
TERMINALS = frozenset(
    {
        "alacritty", "kitty", "foot", "footclient", "ghostty", "com.mitchellh.ghostty",
        "wezterm", "org.wezfurlong.wezterm", "xterm", "st", "st-256color", "konsole",
        "org.kde.konsole", "gnome-terminal", "gnome-terminal-server", "org.gnome.terminal",
        "terminator", "tilix", "xfce4-terminal", "urxvt", "lxterminal", "terminology",
        "contour", "rio", "cool-retro-term", "com.raggesilver.blackbox", "org.gnome.console",
        "ptyxis", "org.gnome.ptyxis", "blackbox", "sakura", "yakuake", "hyper",
    }
)

#: Paste shortcuts as wtype arguments.
_SHORTCUTS = {
    "ctrl+v": ["-M", "ctrl", "-k", "v", "-m", "ctrl"],
    "ctrl+shift+v": ["-M", "ctrl", "-M", "shift", "-k", "v", "-m", "shift", "-m", "ctrl"],
    "shift+insert": ["-M", "shift", "-k", "Insert", "-m", "shift"],
}

_TIMEOUT_S = 5

_WHITESPACE_KEYS = {"\n": "Return", "\t": "Tab"}


class InjectionUnavailable(RuntimeError):
    pass


def _need(tool: str, package: str) -> str:
    path = shutil.which(tool)
    if path is None:
        raise InjectionUnavailable(f"'{tool}' is not installed. Install it with: sudo pacman -S {package}")
    return path


def type_text(text: str, delay_ms: int = 0) -> None:
    """Type text as synthetic keystrokes."""
    wtype = _need("wtype", "wtype")
    timeout = _TIMEOUT_S + len(text) * delay_ms / 1000

    # wtype does not document what it does with a newline or a tab in its text,
    # so those are pressed as keys, which is what a person would do. Everything
    # between them goes over stdin, never argv: a dictation that begins with a
    # dash would otherwise be read as an option.
    for piece in re.split(r"([\n\t])", text.replace("\r", "")):
        if not piece:
            continue
        if piece in _WHITESPACE_KEYS:
            subprocess.run(
                [wtype, "-k", _WHITESPACE_KEYS[piece]], stdin=subprocess.DEVNULL, check=True, timeout=timeout
            )
            continue
        command = [wtype] + (["-d", str(delay_ms)] if delay_ms > 0 else []) + ["-"]
        subprocess.run(command, input=piece.encode("utf-8"), check=True, timeout=timeout)


def get_clipboard_text() -> str | None:
    wl_paste = shutil.which("wl-paste")
    if wl_paste is None:
        return None
    try:
        result = subprocess.run(
            [wl_paste, "--no-newline", "--type", "text"],
            capture_output=True,
            timeout=2,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    # Non-zero when the clipboard is empty or holds something that is not text.
    if result.returncode != 0:
        return None
    return result.stdout.decode("utf-8", errors="replace")


def set_clipboard_text(text: str) -> bool:
    wl_copy = shutil.which("wl-copy")
    if wl_copy is None:
        return False
    try:
        # wl-copy forks a child that keeps serving the selection. If its stdout
        # or stderr were pipes we hold, run() would wait on them for ever.
        subprocess.run(
            [wl_copy],
            input=text.encode("utf-8"),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=2,
            check=True,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return True


def focused_window_class() -> str | None:
    """The class of the window with keyboard focus, from Hyprland."""
    hyprctl = shutil.which("hyprctl")
    if hyprctl is None:
        return None
    try:
        result = subprocess.run([hyprctl, "-j", "activewindow"], capture_output=True, timeout=1)
        return str(json.loads(result.stdout).get("class", "")).lower() or None
    except (OSError, subprocess.SubprocessError, ValueError, AttributeError):
        return None


def paste_shortcut(setting: str) -> list[str]:
    """The wtype arguments that press the paste shortcut for the focused window."""
    if setting in _SHORTCUTS:
        return _SHORTCUTS[setting]
    return _SHORTCUTS["ctrl+shift+v" if focused_window_class() in TERMINALS else "ctrl+v"]


def paste_via_clipboard(
    text: str, restore: bool = True, restore_delay_ms: int = 450, shortcut: str = "auto"
) -> None:
    previous = get_clipboard_text() if restore else None

    if not set_clipboard_text(text):
        log.warning("clipboard unavailable; typing instead")
        type_text(text)
        return

    subprocess.run(
        [_need("wtype", "wtype"), *paste_shortcut(shortcut)],
        stdin=subprocess.DEVNULL,
        check=True,
        timeout=_TIMEOUT_S,
    )

    if previous is None:
        return

    # The paste shortcut only queues the paste; the target reads the clipboard
    # whenever it gets round to it. Restoring inline raced that and the app
    # pasted the *old* contents. So restore on a timer, and off the calling
    # thread - the dictation pipeline must not block on this.
    timer = threading.Timer(restore_delay_ms / 1000, set_clipboard_text, args=(previous,))
    timer.daemon = True
    timer.start()


def inject(text: str, cfg) -> str:
    """Insert text at the caret. Returns the strategy used."""
    if not text:
        return "noop"
    if len(text) <= cfg.clipboard_threshold:
        type_text(text, cfg.keystroke_delay_ms)
        return "keystrokes"
    paste_via_clipboard(text, cfg.restore_clipboard, cfg.restore_delay_ms, cfg.paste_shortcut)
    return "clipboard"
