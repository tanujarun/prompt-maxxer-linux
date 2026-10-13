# Super can appear stuck during keyboard takeover

Reported on 2026-10-13 with Omarchy 4.0.4-1, Hyprland 0.56.2-2 and
`hotkey.suppress: true`, using Right Ctrl for push-to-talk.

## Symptoms and evidence

After boot, Super appeared permanently held: ordinary typing launched desktop
shortcuts. Restarting the computer restored normal input.

The affected sessions logged Prompt Maxxer taking exclusive keyboard grabs at
18:14:33 and 18:16:59 IST. In the second session, Tixati had launched at 18:16:36.
Launching Tixati again later did not reproduce the symptom. The available input
recorder excluded modifier holds, and the compositor diagnostic did not capture
a missing Super release in these sessions. These observations establish timing,
not the exact cause of the two reported lockups; they do not establish a Tixati bug.

## Confirmed defect

The old `_Keyboard.try_grab()` checked `active_keys()` before calling
`UInput.from_device()`, then grabbed the physical keyboard without checking again.
The installed python-evdev implementation waits at least 100 ms while finding
the virtual device node. A key could be pressed during that wait.

A deterministic fake-device regression test makes Super become held during
virtual-device creation. The old code still takes the grab. This can divert a
release away from the original keyboard state in the compositor. The test
reproduces the unsafe takeover sequence without injecting any desktop keys;
it does not reproduce or conclusively explain the original desktop lockups.

## Fix and recommended settings

Suppression now checks for held keys again after virtual-device creation and
defers the grab if a key became held. It retains that pending virtual device
for the next attempt, allowing the original keyboard to deliver the key release
without repeating the creation delay. Pending devices are closed when the hook
stops or suppression is disabled; a failed grab closes its candidate too.

The state check and exclusive grab are separate kernel calls. This change removes
the long creation window, but does not make takeover atomic. Passive monitoring
avoids exclusive takeover entirely and remains the default and recommended mode
for Right Ctrl. For an existing configuration that enabled suppression, set:

```json
"hotkey": {
  "key_code": 97,
  "label": "Right Ctrl",
  "suppress": false
}
```

Keep the other configuration sections and restart Prompt Maxxer. Right Ctrl
dictation still works; the key also reaches the focused application and can
participate in its shortcuts if other keys are pressed while it is held.
Existing suppression preferences are not automatically rewritten by this update.

On the reporting machine, passive mode was applied to the saved configuration
and Prompt Maxxer restarted. The engine reported ready, all its virtual keyboard
clones disappeared, and Tixati remained running. Recurrence of the original
symptom would require fresh modifier-state evidence to identify its cause.

## Verification

Run from `engine/` using the engine virtualenv:

```bash
~/.local/share/prompt-maxxer/venv/bin/python test_hotkey.py
```

The tests use fake devices only. They cover Super pressed during creation,
deferral and reuse after release, initially held keys, grab failures, cleanup,
forwarded-key release ordering, and passive Right Ctrl press/release detection
with auto-repeat ignored. They require no microphone, GPU, input permissions or
Wayland session.
