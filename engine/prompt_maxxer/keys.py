"""Naming keys, and deciding which keys may be the push-to-talk key.

Keys are Linux evdev codes (linux/input-event-codes.h), the numbers the kernel
reports for every key on every keyboard. The push-to-talk key can be swallowed
system-wide (see hotkey.py), so a key that types text or drives editing would
stop working everywhere. Those are refused, with a reason the settings window
can show.
"""

from __future__ import annotations

from evdev import ecodes as e

_NAMES: dict[int, str] = {
    e.KEY_RIGHTCTRL: "Right Ctrl",
    e.KEY_RIGHTSHIFT: "Right Shift",
    e.KEY_RIGHTALT: "Right Alt",
    e.KEY_LEFTCTRL: "Left Ctrl",
    e.KEY_LEFTSHIFT: "Left Shift",
    e.KEY_LEFTALT: "Left Alt",
    e.KEY_LEFTMETA: "Left Super",
    e.KEY_RIGHTMETA: "Right Super",
    e.KEY_COMPOSE: "Menu",
    e.KEY_CAPSLOCK: "Caps Lock",
    e.KEY_NUMLOCK: "Num Lock",
    e.KEY_SCROLLLOCK: "Scroll Lock",
    e.KEY_PAUSE: "Pause",
    e.KEY_SYSRQ: "Print Screen",
    e.KEY_INSERT: "Insert",
    e.KEY_MUTE: "Mute",
    e.KEY_VOLUMEDOWN: "Volume Down",
    e.KEY_VOLUMEUP: "Volume Up",
    e.KEY_PLAYPAUSE: "Play/Pause",
    e.KEY_STOPCD: "Stop Media",
    e.KEY_NEXTSONG: "Next Track",
    e.KEY_PREVIOUSSONG: "Previous Track",
    e.KEY_MICMUTE: "Mic Mute",
    e.KEY_BRIGHTNESSDOWN: "Brightness Down",
    e.KEY_BRIGHTNESSUP: "Brightness Up",
    e.KEY_CALC: "Calculator",
    e.KEY_MAIL: "Mail",
    e.KEY_HOMEPAGE: "Browser Home",
    e.KEY_BACK: "Browser Back",
    e.KEY_FORWARD: "Browser Forward",
    e.KEY_REFRESH: "Browser Refresh",
    e.KEY_SEARCH: "Browser Search",
    e.KEY_FAVORITES: "Browser Favorites",
}


def key_label(code: int) -> str:
    """A human name for a key, as the settings window and tray show it."""
    if code in _NAMES:
        return _NAMES[code]
    name = e.KEY.get(code)
    if isinstance(name, (list, tuple)):  # a code with several aliases
        name = name[0]
    if isinstance(name, str) and name.startswith("KEY_"):
        word = name[4:]
        # F1-F24 and single characters read best as they are.
        if len(word) <= 3 or word[0] == "F" and word[1:].isdigit():
            return word
        return word.replace("_", " ").title()
    return f"Key {code}"


def refusal(code: int) -> str | None:
    """Why a key cannot be the push-to-talk key, or None if it can."""
    if e.KEY_1 <= code <= e.KEY_EQUAL or e.KEY_Q <= code <= e.KEY_RIGHTBRACE:
        return "it types text"
    if e.KEY_A <= code <= e.KEY_GRAVE or e.KEY_BACKSLASH <= code <= e.KEY_SLASH:
        return "it types text"
    if e.KEY_KP7 <= code <= e.KEY_KPDOT or code in (e.KEY_KPENTER, e.KEY_KPSLASH, e.KEY_KPEQUAL, e.KEY_102ND):
        return "it types text"
    if code in (e.KEY_BACKSPACE, e.KEY_TAB, e.KEY_ENTER, e.KEY_SPACE, e.KEY_DELETE, e.KEY_ESC):
        return "you need it for typing"
    if code in (
        e.KEY_UP, e.KEY_DOWN, e.KEY_LEFT, e.KEY_RIGHT,
        e.KEY_HOME, e.KEY_END, e.KEY_PAGEUP, e.KEY_PAGEDOWN,
    ):
        return "you need it to move around text"
    if code in (e.KEY_LEFTCTRL, e.KEY_LEFTSHIFT, e.KEY_LEFTALT):
        return "the left one drives everyday shortcuts - try the right-hand key"
    if code in (e.KEY_LEFTMETA, e.KEY_RIGHTMETA):
        return "it is the Hyprland modifier, so every window shortcut starts with it"
    return None
