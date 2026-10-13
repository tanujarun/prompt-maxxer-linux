"""Keyboard takeover regressions using fake devices only; no desktop access.

Run with the engine virtualenv's Python: python test_hotkey.py.
"""

import errno
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from evdev import ecodes as e

from prompt_maxxer.config import HotkeyConfig
from prompt_maxxer import hotkey as h


class KeyboardTests(unittest.TestCase):
    def setUp(self):
        self.dev = Mock()
        self.dev.name = "fake keyboard"
        self.dev.path = "/dev/input/fake"
        self.dev.fd = 12345
        self.dev.capabilities.return_value = {e.EV_KEY: [e.KEY_A, e.KEY_RIGHTCTRL]}
        self.held = []
        self.dev.active_keys.side_effect = lambda: list(self.held)
        self.ui = Mock()
        self.factory = patch.object(h.UInput, "from_device", return_value=self.ui)
        self.create = self.factory.start()
        self.addCleanup(self.factory.stop)
        self.kb = h._Keyboard(self.dev)
        self.addCleanup(self.kb.close)

    def press_during_creation(self, *args, **kwargs):
        # Deterministically model the key press during python-evdev's wait.
        self.held.append(e.KEY_LEFTMETA)
        return self.ui

    def test_super_pressed_during_creation_defers_grab(self):
        self.create.side_effect = self.press_during_creation
        self.assertFalse(self.kb.try_grab())
        self.dev.grab.assert_not_called()
        self.assertFalse(self.kb.grabbed)
        self.ui.write_event.assert_not_called()

    def test_retry_reuses_pending_device_after_super_release(self):
        self.create.side_effect = self.press_during_creation
        self.assertFalse(self.kb.try_grab())
        self.assertFalse(self.kb.try_grab())
        self.held.clear()
        self.assertTrue(self.kb.try_grab())
        self.create.assert_called_once()
        self.dev.grab.assert_called_once()
        self.assertIs(self.kb.ui, self.ui)
        self.assertIsNone(self.kb._pending_ui)

    def test_held_key_before_creation_does_not_create_device(self):
        self.held.append(e.KEY_LEFTMETA)
        self.assertFalse(self.kb.try_grab())
        self.create.assert_not_called()
        self.dev.grab.assert_not_called()

    def test_successful_grab_is_idempotent(self):
        self.assertTrue(self.kb.try_grab())
        self.assertTrue(self.kb.try_grab())
        self.create.assert_called_once()
        self.dev.grab.assert_called_once()

    def test_grab_failure_closes_candidate_and_allows_retry(self):
        self.dev.grab.side_effect = OSError(errno.EBUSY, "already grabbed")
        with self.assertRaises(OSError):
            self.kb.try_grab()
        self.ui.close.assert_called_once()
        self.assertIsNone(self.kb._pending_ui)
        self.assertFalse(self.kb.grabbed)
        self.dev.grab.side_effect = None
        self.create.return_value = Mock()
        self.assertTrue(self.kb.try_grab())
        self.assertEqual(self.create.call_count, 2)

    def test_close_disposes_pending_device_without_ungrabbing(self):
        self.create.side_effect = self.press_during_creation
        self.assertFalse(self.kb.try_grab())
        self.kb.close()
        self.kb.close()
        self.ui.close.assert_called_once()
        self.dev.ungrab.assert_not_called()

    def test_release_pending_device_when_suppression_is_disabled(self):
        self.create.side_effect = self.press_during_creation
        self.assertFalse(self.kb.try_grab())
        hook = h.PushToTalkHook(e.KEY_RIGHTCTRL, lambda: None, lambda: None)
        hook._keyboards[self.dev.path] = self.kb
        with patch.object(h, "list_devices", return_value=[self.dev.path]):
            hook._scan(Mock())
        self.ui.close.assert_called_once()
        self.assertIsNone(self.kb._pending_ui)

    def test_forwarded_keys_released_before_ungrab(self):
        self.assertTrue(self.kb.try_grab())
        self.kb.forward(SimpleNamespace(type=e.EV_KEY, code=e.KEY_LEFTMETA, value=1))
        order = []
        self.ui.write.side_effect = lambda *args: order.append(("release", args))
        self.ui.syn.side_effect = lambda: order.append(("sync",))
        self.dev.ungrab.side_effect = lambda: order.append(("ungrab",))
        self.kb.release_grab()
        self.assertEqual(order, [
            ("release", (e.EV_KEY, e.KEY_LEFTMETA, 0)), ("sync",), ("ungrab",),
        ])
        self.ui.close.assert_called_once()
        self.assertFalse(self.kb.grabbed)
        self.assertFalse(self.kb.down)

    def test_passive_mode_never_grabs_and_right_ctrl_still_works(self):
        cfg = HotkeyConfig()
        self.assertFalse(cfg.suppress)
        hook = h.PushToTalkHook(cfg.key_code, lambda: None, lambda: None,
                              suppress=cfg.suppress)
        hook._keyboards[self.dev.path] = self.kb
        with patch.object(h, "list_devices", return_value=[self.dev.path]):
            hook._scan(Mock())
        self.create.assert_not_called()
        self.dev.grab.assert_not_called()
        for value in (1, 2, 2, 0):
            hook._handle(self.kb, SimpleNamespace(type=e.EV_KEY,
                                                code=e.KEY_RIGHTCTRL, value=value))
        self.assertEqual(hook._events.get_nowait(), (h.PRESS,))
        self.assertEqual(hook._events.get_nowait(), (h.RELEASE,))
        self.assertTrue(hook._events.empty())
        self.assertFalse(hook._down)


if __name__ == "__main__":
    unittest.main(verbosity=2)
