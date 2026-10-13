# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

Prompt Maxxer for Linux is a local-first push-to-talk dictation app (hold a key, speak, release; text is typed at the caret) for **Wayland / Hyprland on Arch (Omarchy)**. It is a fork of the Windows app in `../Prompt-Maxxer/source`; the speech engine, IPC protocol and UI are shared in spirit, everything OS-facing was rewritten. `README.md` is the full reference.

## Commands

```bash
./install.sh                # sudo setup + engine venv + shell build + install to ~/.local
./install.sh --system-only  # just the sudo steps (input group, uinput rule, packages)
./install.sh --no-system    # venv + build + install; what to run after changing code
./run.sh                    # run the built shell from this checkout (it starts the engine)
./run.sh --engine           # engine alone, foreground, verbose

# engine tests are plain scripts, not pytest; run from engine/
PY=~/.local/share/prompt-maxxer/venv/bin/python
cd engine
$PY test_inject.py          # stand-in wtype/wl-copy/hyprctl on PATH: safe, no desktop access
$PY test_hotkey.py          # fake keyboards: takeover race, cleanup, passive Right Ctrl
$PY test_spectrum.py
$PY test_streaming.py <speech.wav>
$PY test_first_run.py       # downloads a small model, then removes it
$PY test_controls.py        # needs uinput + input group; skips with a message without them
$PY test_pipeline.py        # likewise

cd shell && npx --yes pnpm@10.34.6 build          # tsc + vite build
cd shell/src-tauri && CARGO_TARGET_DIR=~/.cache/prompt-maxxer/target cargo test --lib    # autostart tests
prompt-maxxer-engine --transcribe file.wav        # recognise a WAV, no app or mic
```

No linter is configured. pnpm is not installed; use `npx pnpm@10.34.6` (pinned in `package.json`). UI states preview without an engine at `http://localhost:1420/overlay.html?demo=rec&theme=dark` under `pnpm dev` (see `shell/src/dev.ts`).

Build state lives **outside the repo**: venv in `~/.local/share/prompt-maxxer/venv` (editable install of `engine/`, so engine edits need no reinstall), cargo output in `~/.cache/prompt-maxxer/target`. The repo sits on an NTFS drive; keep it that way.

## Architecture

Two processes joined by a WebSocket on `127.0.0.1:8765`; the engine is the source of truth and dictation must keep working if the shell dies.

- **Engine** (`engine/prompt_maxxer/`, Python): `hotkey.py` reads `/dev/input` via python-evdev (Wayland has no global hook) → `audio.py` (PortAudio/PipeWire, stream kept warm) → `pipeline.py` (the central loop and the command handler) → `asr/` (faster-whisper) → `cleanup.py` (LLM slot, stub) → `inject.py` (`wtype` for short text, `wl-copy` + paste shortcut for long). `streaming.py` makes the overlay's live partials; partials are display-only and **never typed**. `ipc.py` fans events out and enforces an **Origin allow-list**.
- **Shell** (`shell/`, Tauri 2 + Vite TS): tray, settings window (created lazily), overlay pill, autostart. `src-tauri/src/engine.rs` finds the venv Python (`PROMPT_MAXXER_PYTHON` overrides) and starts the engine with `--die-with-parent`: it keeps a pipe on the engine's stdin, which closes when the shell dies however it dies, and the engine exits on EOF. Do not replace this with PDEATHSIG (it is tied to the spawning thread).
- **Overlay is a layer-shell surface** (`src-tauri/src/layer.rs`), not a normal window: Wayland has no always-on-top or app-set position, and a normal window would take keyboard focus from the window being dictated into. `libgtk-layer-shell` is loaded at run time and must precede libwayland, so `main` re-execs once with `LD_PRELOAD` (then clears it). If the library is missing the overlay is left **hidden** on purpose. The overlay window starts `visible: false` in `tauri.conf.json` and is shown by `place_overlay`.
- **Key handling:** `hotkey.py` is passive by default. Keep it passive for Right Ctrl. `hotkey.suppress` grabs keyboards exclusively and forwards other keys through a uinput virtual keyboard (names start with `VIRTUAL_PREFIX`, excluded from scanning). Held keys are checked again after virtual-device creation; pending devices are reused until idle and closed during cleanup. The state check and grab are separate kernel calls; see `docs/keyboard-takeover-bug.md`. Keys are evdev codes (`hotkey.key_code`, `keys.py`).
- `--ptt press|release` sends `ptt_press`/`ptt_release` to a running engine, for Hyprland binds that avoid the `input` group. If the key reader cannot start (no permission), the engine keeps running and emits an error event.
- CUDA: the system CUDA is 13 and CTranslate2 needs 12, so the venv holds NVIDIA's cu12 wheels. `cuda_paths.py` preloads them with `ctypes.CDLL(..., RTLD_GLOBAL)` at import (setting `LD_LIBRARY_PATH` after start does nothing). There is no first-launch download here, unlike Windows.
- Config `~/.config/prompt-maxxer/config.json` (schema in `config.py`), logs `~/.local/state/prompt-maxxer/logs/engine.log`. Dictation is never written to disk.
- An RMS gate (`audio.min_rms`) drops quiet audio before the model, because Whisper hallucinates on silence. Don't remove it.

## Rules

- **Never test keystroke injection against the live desktop.** `wtype` types into whichever window has focus, which may be another of the user's terminals or Claude sessions; this has already gone wrong once. Use `test_inject.py` (stand-in binaries) or stub `inject_mod.inject`. Tests that need key input use `uinput_keys.VirtualKeyboard`, which waits until the engine has grabbed the virtual device so the desktop never sees it. Don't use `pkill -f` with a pattern that appears in your own command line.
- **Nothing floats.** Python deps are pinned in `engine/requirements.lock` (installed as a constraints file); also `pnpm-lock.yaml`, `Cargo.lock`, and model revisions in `models.py`. The pins match the tested Windows versions.
- Needing `sudo` (input group, udev rule, pacman) is isolated in `install.sh`'s `system_setup`, which prints each command first. Claude cannot run it; ask the user to.
- Don't edit the user's Hyprland config (`~/.config/hypr/*.lua`, Lua syntax); give snippets instead.
- Commit, push and other outward-facing actions only when asked.

## Gotchas

- `evdev.list_devices()` silently skips nodes the user can't read, so "no devices" can mean "no permission"; `PushToTalkHook.start()` checks `os.access` for this.
- wl-copy forks a child that keeps serving the selection; call it with stdout/stderr on `DEVNULL` or `subprocess.run` never returns.
- `wtype`'s handling of `\n` and `\t` in text is undocumented, so `inject.type_text` presses `Return`/`Tab` as keys and sends the rest over stdin (never argv: a leading `-` is an option).
- Paste shortcut is chosen by `hyprctl -j activewindow` class: `Ctrl+Shift+V` for terminals (`TERMINALS` in `inject.py`), `Ctrl+V` otherwise.
- Linux tray icons (libayatana) emit no click events; settings opens from the tray menu or `prompt-maxxer --settings`.
- The tray icon is opt-in (`prompt-maxxer --tray`). On the author's Omarchy desktop the `tanuj.maxxer` Quickshell bar widget (`~/.config/omarchy/plugins/tanuj.maxxer`) is the app's icon, so a tray icon would be a second one; the widget's card carries the tray's actions (settings, models, quit).
- `WEBKIT_DISABLE_DMABUF_RENDERER=1` is set by default in `lib.rs::start` (blank windows on some NVIDIA setups); a user-set value wins.
- Hyprland here uses the Lua config (`hyprland.lua`); bind syntax is `hl.bind("F13", hl.dsp.exec_cmd(...), { release = true })`, verified against `/usr/share/hypr/stubs/hl.meta.lua`.
- UI follows the `nothing-design` skill: red `#D71921` means recording/errors only; orange `#FF5F1F` is brand only, never together. Fonts are bundled.
