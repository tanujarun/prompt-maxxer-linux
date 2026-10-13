"""Global push-to-talk detection and hotkey capture, straight from evdev.

Wayland gives an application no way to see keys pressed while another window
has focus: there is no global keyboard hook, by design. What a process *can* do
is read the kernel's input devices (/dev/input/event*), which sit below the
compositor and see every key from every keyboard. That needs the user to be in
the `input` group, which install.sh sets up.

Three things shape this module:

1. Reading is passive by default. The key still reaches whatever has focus. For
   a key that does something on its own (AltGr, an F-key a window manager
   binds), `suppress` grabs each keyboard exclusively and forwards every other
   key through a virtual keyboard created with uinput, so the push-to-talk key
   alone is swallowed. A grab belongs to the open file, so the kernel drops it
   the moment this process exits, however it exits: a crash can never leave a
   keyboard captured.
2. Grabbing while a key is held can leave that key stuck down in the
   compositor, which never sees its release. Check again after creating the
   virtual keyboard: device creation can block while the user presses a key.
   The state check and grab are separate kernel calls, so passive mode remains
   the safest choice. Release forwarded keys before giving a grab back.
3. Callbacks never run on the reader thread. They run on a dispatch thread, so
   a slow observer cannot delay the next key event.

Choosing a new key happens here too, rather than in the settings window: the
settings window only sees keys while it has focus, and cannot tell left Ctrl
from right.
"""

from __future__ import annotations

import errno
import glob
import logging
import os
import queue
import selectors
import threading
import time
from typing import Callable, Optional, Tuple

from evdev import InputDevice, UInput, ecodes as e, list_devices

log = logging.getLogger(__name__)

PRESS = "press"
RELEASE = "release"
CAPTURED = "captured"

#: (key_code,) of a captured key, or None if capture was cancelled.
Captured = Optional[Tuple[int]]

#: Our own virtual keyboards carry this prefix, so they are never mistaken for
#: a real keyboard and read back in.
VIRTUAL_PREFIX = "Prompt Maxxer virtual keyboard"

#: How often to look for keyboards that were plugged in after start.
RESCAN_S = 2.0

#: Keys that, on a device with no letters, still make it worth listening to:
#: the consumer-control interfaces that carry media keys.
_CONSUMER_KEYS = frozenset(
    {e.KEY_VOLUMEUP, e.KEY_VOLUMEDOWN, e.KEY_MUTE, e.KEY_PLAYPAUSE, e.KEY_NEXTSONG,
     e.KEY_PREVIOUSSONG, e.KEY_MICMUTE, e.KEY_HOMEPAGE, e.KEY_MAIL, e.KEY_CALC}
)

PERMISSION_HELP = (
    "Cannot read the keyboard. Prompt Maxxer reads /dev/input directly, which needs "
    "your user to be in the 'input' group: run ./install.sh (or "
    "'sudo usermod -aG input $USER'), then log out and back in."
)


def _keys_of(dev: InputDevice) -> frozenset[int]:
    return frozenset(dev.capabilities().get(e.EV_KEY, ()))


def _is_keyboard(dev: InputDevice) -> bool:
    if dev.name.startswith(VIRTUAL_PREFIX):
        return False
    keys = _keys_of(dev)
    return e.KEY_A in keys or bool(keys & _CONSUMER_KEYS)


class _Keyboard:
    """One physical keyboard we are reading, and maybe holding."""

    def __init__(self, dev: InputDevice) -> None:
        self.dev = dev
        self.path = dev.path
        self.is_full = e.KEY_A in _keys_of(dev)
        self.ui: UInput | None = None
        self._pending_ui: UInput | None = None
        self.down: set[int] = set()  # keys the compositor believes are down

    @property
    def grabbed(self) -> bool:
        return self.ui is not None

    def try_grab(self) -> bool:
        """Grab if no key is down. False means try again later."""
        if self.ui is not None:
            return True
        if not self.is_full or self.dev.active_keys():
            return False
        if self._pending_ui is None:
            self._pending_ui = UInput.from_device(
                self.dev, name=f"{VIRTUAL_PREFIX} ({self.dev.name})"
            )
        # UInput creation waits for the new device node (at least 100 ms with
        # python-evdev). The earlier idle check is stale after that wait. Keep
        # this device for the retry so every retry does not open the same gap.
        if self.dev.active_keys():
            log.debug("deferring keyboard grab: keys became held during device creation")
            return False
        try:
            self.dev.grab()
        except OSError:
            self._pending_ui.close()
            self._pending_ui = None
            raise
        self.ui = self._pending_ui
        self._pending_ui = None
        self.down.clear()
        return True

    def forward(self, event) -> None:
        if self.ui is None:
            return
        if event.type == e.EV_KEY:
            if event.value:
                self.down.add(event.code)
            else:
                self.down.discard(event.code)
        self.ui.write_event(event)

    def release_grab(self) -> None:
        if self._pending_ui is not None:
            self._pending_ui.close()
            self._pending_ui = None
        if self.ui is None:
            return
        # A key still down here would stay down in the compositor for ever.
        try:
            for code in sorted(self.down):
                self.ui.write(e.EV_KEY, code, 0)
            self.ui.syn()
        except OSError:
            pass
        self.down.clear()
        try:
            self.dev.ungrab()
        except OSError:
            pass
        self.ui.close()
        self.ui = None

    def close(self) -> None:
        self.release_grab()
        try:
            self.dev.close()
        except OSError:
            pass


class PushToTalkHook:
    def __init__(
        self,
        key_code: int,
        on_press: Callable[[], None],
        on_release: Callable[[], None],
        on_capture: Callable[[Captured], None] | None = None,
        suppress: bool = False,
    ) -> None:
        self._key = key_code
        self._on_press = on_press
        self._on_release = on_release
        self._on_capture = on_capture
        self._suppress = suppress

        # True between key-down and key-up. Guards against auto-repeat, which
        # delivers a stream of value-2 events while the key is held, and
        # against two devices reporting the same physical press.
        self._down = False

        self._capturing = False
        # A key whose next key-up must be swallowed: the release of a key picked
        # in capture mode, whose press the focused app never saw.
        self._swallow_up: int | None = None

        self._events: "queue.Queue[tuple | None]" = queue.Queue()
        self._keyboards: dict[str, _Keyboard] = {}
        self._denied: set[str] = set()
        # Devices that were looked at and are not keyboards (mice, HDMI audio
        # jacks, power buttons). Remembered, because looking costs about 20 ms
        # each: forgetting them made every rescan open all of /dev/input, which
        # stalled the key reader for nearly half a second, every two seconds.
        self._ignored: set[str] = set()
        self._stop = threading.Event()
        self._wake_r, self._wake_w = os.pipe()
        self._reader: threading.Thread | None = None
        self._dispatch_thread: threading.Thread | None = None
        self._first_scan = threading.Event()

    # -- control (any thread) --------------------------------------------

    @property
    def key_code(self) -> int:
        return self._key

    @property
    def active(self) -> bool:
        """Whether at least one keyboard is being read."""
        return bool(self._keyboards)

    def set_key(self, key_code: int) -> None:
        """Rebind push-to-talk. Takes effect on the next key event."""
        # Clear the held flag first, so a key that was down when the binding
        # changed cannot leave a recording running with no release to end it.
        self._down = False
        self._key = key_code

    def begin_capture(self) -> None:
        """Treat the next key press as the new push-to-talk key."""
        self._capturing = True

    def cancel_capture(self) -> None:
        if self._capturing:
            self._capturing = False
            self._events.put((CAPTURED, None))

    def press(self) -> None:
        """Press the key without a keyboard, for the control command."""
        if not self._down:
            self._down = True
            self._events.put((PRESS,))

    def release(self) -> None:
        if self._down:
            self._down = False
            self._events.put((RELEASE,))

    # -- one key event (reader thread, hot path) ---------------------------

    def _handle(self, kb: _Keyboard, event) -> None:
        """Decide what happens to one EV_KEY event, and pass it on if it lives."""
        code, value = event.code, event.value
        swallow = False

        if value == 0 and code == self._swallow_up:
            self._swallow_up = None
            swallow = True
        elif self._capturing and value == 1:
            self._capturing = False
            self._swallow_up = code
            self._events.put((CAPTURED, None if code == e.KEY_ESC else (code,)))
            swallow = True
        elif code == self._key:
            if value == 1:
                if not self._down:
                    self._down = True
                    self._events.put((PRESS,))
            elif value == 0:
                if self._down:
                    self._down = False
                    self._events.put((RELEASE,))
            swallow = self._suppress

        if not swallow:
            kb.forward(event)

    # -- threads ---------------------------------------------------------

    def _dispatch_loop(self) -> None:
        while True:
            event = self._events.get()
            if event is None:
                return
            kind = event[0]
            try:
                if kind == PRESS:
                    self._on_press()
                elif kind == RELEASE:
                    self._on_release()
                elif kind == CAPTURED and self._on_capture is not None:
                    self._on_capture(event[1])
            except Exception:
                log.exception("hotkey observer failed on %s", kind)

    def _scan(self, selector: selectors.BaseSelector) -> None:
        """Pick up keyboards that appeared, and grab the ones ready to be."""
        paths = list_devices()
        # A node that has gone may come back as a different device, so forget
        # the verdict on anything no longer present.
        present = set(paths)
        self._ignored &= present
        self._denied &= present
        for path in paths:
            if path in self._keyboards or path in self._denied or path in self._ignored:
                continue
            try:
                dev = InputDevice(path)
            except PermissionError:
                self._denied.add(path)
                continue
            except OSError:
                continue
            if not _is_keyboard(dev):
                dev.close()
                self._ignored.add(path)
                continue
            kb = _Keyboard(dev)
            self._keyboards[path] = kb
            selector.register(dev.fd, selectors.EVENT_READ, kb)
            log.info("keyboard: %s (%s)", dev.name, path)

        if self._suppress:
            for kb in self._keyboards.values():
                if kb.grabbed:
                    continue
                try:
                    if kb.try_grab():
                        log.info("grabbed %s", kb.dev.name)
                except OSError as exc:
                    log.warning("cannot grab %s: %s", kb.dev.name, exc)
                    if exc.errno == errno.EACCES or exc.errno == errno.ENOENT:
                        log.warning("is /dev/uinput writable? install.sh sets that up")
        else:
            for kb in self._keyboards.values():
                kb.release_grab()

    def _drop(self, selector: selectors.BaseSelector, kb: _Keyboard) -> None:
        log.info("keyboard gone: %s", kb.path)
        try:
            selector.unregister(kb.dev.fd)
        except (KeyError, ValueError, OSError):
            pass
        self._keyboards.pop(kb.path, None)
        kb.close()

    def _read_loop(self) -> None:
        selector = selectors.DefaultSelector()
        selector.register(self._wake_r, selectors.EVENT_READ, None)
        next_scan = 0.0
        try:
            while not self._stop.is_set():
                now = time.monotonic()
                if now >= next_scan:
                    self._scan(selector)
                    self._first_scan.set()
                    next_scan = now + RESCAN_S
                # Grabs wait for held keys to come up, so look again soon.
                waiting = self._suppress and any(
                    not kb.grabbed and kb.is_full for kb in self._keyboards.values()
                )
                timeout = 0.2 if waiting else max(0.0, next_scan - time.monotonic())
                for key, _mask in selector.select(timeout):
                    kb = key.data
                    if kb is None:
                        os.read(self._wake_r, 64)
                        continue
                    try:
                        for event in kb.dev.read():
                            if event.type == e.EV_KEY:
                                self._handle(kb, event)
                            elif kb.grabbed:
                                kb.forward(event)
                    except BlockingIOError:
                        pass
                    except OSError as exc:
                        if exc.errno in (errno.ENODEV, errno.EIO, errno.EBADF):
                            self._drop(selector, kb)
                        else:
                            raise
        except Exception:
            log.exception("keyboard reader stopped")
        finally:
            for kb in list(self._keyboards.values()):
                kb.close()
            self._keyboards.clear()
            selector.close()
            self._first_scan.set()
            log.info("keyboards released")

    def start(self) -> None:
        self._dispatch_thread = threading.Thread(
            target=self._dispatch_loop, name="prompt-maxxer-hotkey-dispatch", daemon=True
        )
        self._dispatch_thread.start()
        self._reader = threading.Thread(
            target=self._read_loop, name="prompt-maxxer-hotkey-reader", daemon=True
        )
        self._reader.start()
        self._first_scan.wait(timeout=5)
        if not self._keyboards:
            # list_devices() silently skips nodes the user may not read, so an
            # empty result can mean no permission as easily as no keyboard.
            unreadable = self._denied or any(
                not os.access(path, os.R_OK) for path in glob.glob("/dev/input/event*")
            )
            raise RuntimeError(PERMISSION_HELP if unreadable else "No keyboard found in /dev/input.")
        log.info(
            "listening for %s (code %d)%s",
            e.KEY.get(self._key, self._key),
            self._key,
            ", swallowing it" if self._suppress else "",
        )

    def stop(self) -> None:
        self._stop.set()
        try:
            os.write(self._wake_w, b"x")
        except OSError:
            pass
        self._events.put(None)
        for t in (self._reader, self._dispatch_thread):
            if t is not None:
                t.join(timeout=2)
