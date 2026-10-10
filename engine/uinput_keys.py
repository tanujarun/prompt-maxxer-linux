"""Test helper: a virtual keyboard the engine's hook reads, and nothing else does.

Synthesising a key press means creating a keyboard with uinput, and the desktop
would see that keyboard too. So tests turn on `hotkey.suppress` before they
start the pipeline, which makes the hook grab the virtual keyboard exclusively:
from then on its events reach the hook alone, and the desktop never sees them.
`VirtualKeyboard` refuses to send a key until the grab is in place.

Needs write access to /dev/uinput and read access to /dev/input, which
`./install.sh --system-only` sets up (and a fresh login to apply).
"""

from __future__ import annotations

import os
import time

from evdev import UInput, ecodes as e, list_devices

NAME = "prompt-maxxer-test-keyboard"


def access_problem() -> str | None:
    """Why key synthesis cannot work here, or None if it can."""
    if not os.access("/dev/uinput", os.W_OK):
        return "/dev/uinput is not writable"
    if not list_devices():
        return "/dev/input cannot be read (is your user in the 'input' group?)"
    return None


class VirtualKeyboard:
    def __init__(self, pipeline, timeout: float = 15.0) -> None:
        self._pipeline = pipeline
        self._ui = UInput(
            {e.EV_KEY: [e.KEY_A, e.KEY_ENTER, e.KEY_ESC, e.KEY_F13, e.KEY_RIGHTCTRL, e.KEY_RIGHTALT]},
            name=NAME,
        )
        deadline = time.monotonic() + timeout
        while not self._grabbed():
            if time.monotonic() > deadline:
                self.close()
                raise RuntimeError("the engine never grabbed the test keyboard (is hotkey.suppress on?)")
            time.sleep(0.1)

    def _grabbed(self) -> bool:
        return any(
            kb.dev.name == NAME and kb.grabbed for kb in list(self._pipeline.hook._keyboards.values())
        )

    def key(self, code: int, up: bool = False) -> None:
        self._ui.write(e.EV_KEY, code, 0 if up else 1)
        self._ui.syn()

    def tap(self, code: int, hold: float = 0.05) -> None:
        self.key(code)
        time.sleep(hold)
        self.key(code, up=True)

    def close(self) -> None:
        self._ui.close()
