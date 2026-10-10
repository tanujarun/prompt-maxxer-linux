# Prompt Maxxer for Linux

Say it — don’t type it.

Push-to-talk dictation for Linux, built for Wayland and Hyprland (Omarchy). Hold a
key, speak, release: the text lands wherever your caret is. Recognition runs on
your own machine, so nothing you say leaves it.

This is the Linux fork of the [Windows app](https://github.com/tanujarun/prompt-maxxer). The speech engine, the live transcript,
the models panel and the whole UI are the same; what changed is everything that
touches the operating system: how the key is read, how text is typed, how the
overlay is placed. See [What differs from Windows](#what-differs-from-windows).

---

## Install

Arch Linux or Omarchy, Hyprland, an NVIDIA GPU (optional) and a microphone.

```bash
git clone https://github.com/tanujarun/prompt-maxxer-linux.git
cd prompt-maxxer-linux
./install.sh
```

It does three things, and prints every command that needs `sudo` before running it:

1. **System setup.** Installs missing packages (`gtk-layer-shell`, `wtype`,
   `wl-clipboard`, WebKitGTK and friends), adds you to the `input` group, and
   installs a udev rule so the `input` group can open `/dev/uinput`.
2. **Engine environment** in `~/.local/share/prompt-maxxer/venv`, with every
   dependency at the exact version it was tested with (`engine/requirements.lock`).
   On an NVIDIA machine this includes NVIDIA's CUDA 12 libraries from their own
   wheels, because the system's CUDA is a different major version from the one
   CTranslate2 is built against.
3. **The shell**, built into `~/.cache/prompt-maxxer/target` and installed to
   `~/.local/bin/prompt-maxxer`, plus a launcher entry and icon.

`./install.sh --system-only` runs just step 1; `./install.sh --no-system` runs just
steps 2 and 3, which is what to run after pulling changes.

**Log out and back in once** after the first install, so the new `input` group
applies. Until then the app runs but cannot see your push-to-talk key.

### Why the `input` group

Wayland has no global keyboard hook. By design, an application cannot see keys
pressed while another window has focus. The only way to build push-to-talk is to
read the kernel's input devices (`/dev/input/event*`) directly, which needs the
`input` group. Be aware of what that means: *any* program you run can then read
your keystrokes, not only this one. If that is not acceptable, there is a way to
use the app without it, described under [Binding the key in Hyprland](#binding-the-key-in-hyprland-instead).

### First launch

Run `prompt-maxxer`. The settings window opens by itself the first time:

- Prompt Maxxer checks your hardware and picks the speech model that suits it —
  `large-v3-turbo` on an NVIDIA GPU, a smaller model without one.
- It downloads that model once (75 MB to 1.6 GB) into the shared Hugging Face
  cache, `~/.cache/huggingface/hub`. After that, dictation works offline.
- When the big word says **READY**, hold **Right Ctrl**, speak, and let go.

It lives in the tray. Open settings from the tray menu, or with
`prompt-maxxer --settings` (or `--models`); a second launch just brings up the
running copy's window.

## Using it

Hold the key, speak, release. While you speak, the pill at the bottom of the
screen shows your words as you say them: bright where Whisper is sure, dim where
it may still change its mind. Only the single, final pass after you release is
typed; the live words are for show.

Short text is typed as keystrokes. Long text (over 120 characters) is pasted,
which is instant: the clipboard is saved first and put back afterwards. The paste
shortcut depends on the window — `Ctrl+Shift+V` in a terminal, `Ctrl+V` elsewhere
— which it works out by asking Hyprland which window has focus.

### Settings

Open the tray icon's menu and choose **Settings…** (Linux tray icons offer a menu but
no click events), or run `prompt-maxxer --settings`, which also makes a handy Hyprland binding.

- **Push-to-talk key.** Click the keycap, then press the key you want. Left and
  right modifiers are distinct, and F13–F24 and media keys work. Keys that would
  break normal typing are refused with the reason: letters, digits, Space, Enter,
  arrows, the left-hand Ctrl/Shift/Alt, and both Super keys (Hyprland's modifier).
  Esc cancels.
- **Start at login.** Writes `~/.config/autostart/prompt-maxxer.desktop`. It
  reports what the desktop will actually do: off, switched off outside the app, or
  pointing at a different copy of the program.
- **Visualizer, tape log, models, theme.** As on Windows. The theme follows the
  system's light/dark preference, or can be forced.

### Binding the key in Hyprland instead

If you would rather not join the `input` group, let Hyprland read the key and tell
the engine:

```lua
-- ~/.config/hypr/bindings.lua  (hyprland.conf syntax is the same idea)
hl.bind("F13", hl.dsp.exec_cmd("prompt-maxxer-engine --ptt press"))
hl.bind("F13", hl.dsp.exec_cmd("prompt-maxxer-engine --ptt release"), { release = true })
```

```ini
# hyprland.conf
bind  = , F13, exec, prompt-maxxer-engine --ptt press
bindr = , F13, exec, prompt-maxxer-engine --ptt release
```

The engine keeps running without keyboard access (it says so in the settings
window); `--ptt` presses and releases push-to-talk in the running engine.

### Configuration

Everything settings does not expose is in `~/.config/prompt-maxxer/config.json`:

| Key | What |
|---|---|
| `audio.device` | Microphone, by index or name (`prompt-maxxer-engine --list-devices`) |
| `asr.language` | Dictation language; `null` to autodetect (costs ~70 ms) |
| `audio.min_rms` | The silence gate (below) |
| `streaming` | Live-transcript timing, or `streaming.enabled: false` to keep the GPU quiet |
| `hotkey.suppress` | Swallow the key so other applications never see it (below) |
| `inject.paste_shortcut` | `auto`, `ctrl+v`, `ctrl+shift+v` or `shift+insert` |
| `inject.clipboard_threshold` | Characters above which text is pasted rather than typed |

Where things live:

- Settings: `~/.config/prompt-maxxer/config.json`
- Engine log: `~/.local/state/prompt-maxxer/logs/engine.log`
- Engine environment: `~/.local/share/prompt-maxxer/venv`
- Models: `~/.cache/huggingface/hub`

### Swallowing the key

By default the push-to-talk key is read passively: it also reaches the focused
window. For Right Ctrl, that does nothing. For a key that does something on its
own — Right Alt (AltGr), an F-key, a media key — set `hotkey.suppress` to `true`.
The engine then grabs each keyboard exclusively and re-emits every other key
through a virtual keyboard it creates with uinput, so only the push-to-talk key is
swallowed. Two things to know:

- A grab belongs to the open file, so the kernel releases it the instant the engine
  exits, however it exits. A crash cannot leave a keyboard captured.
- A grabbed keyboard stops driving its own caps-lock LED. This is why it is off by
  default.

### Troubleshooting

| Symptom | What to do |
|---|---|
| Settings says the key is unavailable, "needs the input group" | Run `./install.sh --system-only`, then log out and back in. `id` should list `input`. |
| No recording pill on screen | Needs `gtk-layer-shell` (`sudo pacman -S gtk-layer-shell`). Dictation works without it; the pill is left out because a plain window would take keyboard focus. |
| Nothing is typed | Needs `wtype` (and `wl-clipboard` for long text). Check `engine.log`. |
| It types into the wrong window | The pill never takes focus; the text goes where the compositor says focus is. |
| Settings shows **OFFLINE** | Click **Start engine**. If it will not stay up, read `engine.log`. |
| Wrong microphone | Set `audio.device` in the config file. |
| Running on the CPU unexpectedly | The CUDA libraries are missing from the venv; re-run `./install.sh --no-system`. |
| No tray icon | Your bar needs a StatusNotifier tray; or use `prompt-maxxer --settings`. |

Check recognition on its own, with no app and no microphone:

```bash
prompt-maxxer-engine --transcribe path/to/speech.wav
```

---

## What differs from Windows

| | Windows | Linux |
|---|---|---|
| Reading the key | `WH_KEYBOARD_LL` hook | evdev (`/dev/input`), needs the `input` group |
| Swallowing it | the hook returns 1 | exclusive grab + virtual keyboard (opt-in) |
| Typing | `SendInput`, Unicode events | `wtype` (virtual-keyboard protocol) |
| Long text | clipboard + `Ctrl+V` | `wl-copy` + `Ctrl+V` / `Ctrl+Shift+V` by window class |
| Overlay | topmost, click-through window | Wayland layer-shell surface |
| GPU libraries | downloaded on first launch | NVIDIA wheels in the venv |
| Packaging | PyInstaller + NSIS installer | `install.sh`, from source |
| Updates | signed self-updater | `git pull && ./install.sh --no-system` |
| Autostart | registry Run key | XDG autostart entry |
| Engine lifetime | Win32 job object | stdin pipe: closes when the shell dies |

The Windows-only parts — the updater, the installer, the signing key — are gone,
and so is the code that served them.

## Architecture

Two processes, split by responsibility rather than by language convenience.

```
┌─ SHELL (Tauri 2 / Rust) ────────┐      ┌─ ENGINE (Python 3) ──────────────┐
│  tray icon, settings window     │      │  ▸ evdev key reader (PTT)        │
│  overlay pill (layer-shell)     │◄────►│  ▸ PipeWire mic stream (warm)    │
│  tape log, models, theme        │  WS  │  ▸ faster-whisper (CUDA or CPU)  │
│  autostart                      │ 8765 │  ▸ cleanup stage (LLM slot)      │
│                                 │      │  ▸ wtype / wl-clipboard inject   │
└─────────────────────────────────┘      └──────────────────────────────────┘
        cosmetic, disposable                  owns the entire hot path
```

The engine owns hotkey → capture → recognise → clean → inject end to end.
Dictation keeps working if the shell crashes or was never started. The shell
renders the engine's event stream and sends it commands from settings.

**The overlay** (`shell/src-tauri/src/layer.rs`) is a layer-shell surface, the
mechanism panels and launchers use. A normal window cannot work here: Wayland has
no "always on top", an application cannot position its own window, and the
compositor gives focus to a new window — which would pull it from the window being
dictated into. A layer surface sits above everything, is anchored bottom-centre,
and has keyboard interactivity *none*, so it can never take focus. `libgtk-layer-shell`
has to be loaded before `libwayland-client`, so the program re-executes itself once
at launch with it preloaded; it is loaded at run time, so the app runs without it,
minus the pill.

**The key reader** (`engine/prompt_maxxer/hotkey.py`) never runs callbacks on the
thread that reads events; it queues them for a dispatch thread. It rescans for
keyboards every two seconds, so one plugged in later is picked up, and it only
grabs a keyboard when no key on it is held, since a key held through a grab would
stay down in the compositor for ever.

**The engine's WebSocket** accepts connections only from the app's own windows. Browsers
do not apply the same-origin policy to WebSockets, so without an Origin check any
web page could connect to `127.0.0.1:8765`, read dictation as it happens and send
commands.

### Whisper hallucinates on silence

Recording a quiet room and running it through the model reliably produced
`"Howdy, howdy."` — and VAD did not catch it. In a dictation app that is worse than
useless, because the text is typed into whatever has focus. So an RMS gate runs
before the model: anything below `audio.min_rms` is dropped without a forward pass.
Speech sits around 0.02–0.2 RMS and room tone under 0.002, so the 0.006 default has
a wide margin.

### Live transcript

Whisper has no streaming mode, so the overlay's words come from re-running it over
the whole buffer every 420 ms. A pass costs under a third of the interval, so the
GPU is mostly idle. Partials are never typed, so a wrong guess mid-sentence costs
nothing. Words that survived the previous pass unchanged are drawn bright; the rest
dim (the LocalAgreement idea, at its simplest).

Measured on an RTX 4080 SUPER with `large-v3-turbo` (`int8_float16`, greedy, language
pinned): **113 ms** for a 3 s utterance.

## Design

A tape recorder, drawn with Nothing's rules and Teenage Engineering's instincts:
monochrome, typographic, flat. Red `#D71921` only ever means *recording* (and
errors). TE orange `#FF5F1F` is identity only. They never share the screen. Space
Grotesk, Space Mono and Doto are bundled, so nothing loads from the network.

Every UI state can be previewed without an engine: run `pnpm dev` in `shell/`, then
open `http://localhost:1420/overlay.html?demo=rec&theme=dark` or
`index.html?demo=ready&theme=light`. Overlay demos: `rec`, `rec-empty`, `proc`,
`done`, `nospeech`, `error`, `loading`. Settings demos: `ready`, `rec`, `working`,
`offline`, `capture`, `refused`, `confirm`, `menu`, `models`, `startup-blocked`.

## Development

```bash
./install.sh --no-system          # engine env + shell build + install
./run.sh                          # the shell from this checkout, which starts the engine
./run.sh --engine                 # just the engine, in the foreground with its log
cd shell && npx pnpm@10.34.6 dev  # Vite on :1420 for previewing UI states
```

The engine is installed into its venv in editable mode, so engine changes take
effect on the next launch without reinstalling.

Build output goes to `~/.cache/prompt-maxxer/target`, not into this folder, so
this folder can live on any filesystem.

### Tests

The engine's tests are plain scripts, run from `engine/`:

```bash
PY=~/.local/share/prompt-maxxer/venv/bin/python
$PY test_inject.py            # injection, against stand-in wtype/wl-copy/hyprctl: safe anywhere
$PY test_spectrum.py          # visualizer bands on tones
$PY test_streaming.py ../path/to/speech.wav   # live partials over a real clip
$PY test_first_run.py         # first launch on a machine without CUDA (downloads a small model, removes it)
$PY test_controls.py          # hotkey capture, tape log, models, origin lock
$PY test_pipeline.py          # the whole loop, from a key press to a transcript
cd ../shell/src-tauri && CARGO_TARGET_DIR=~/.cache/prompt-maxxer/target cargo test --lib   # autostart
```

`test_controls.py` and `test_pipeline.py` press keys on a virtual keyboard the
engine grabs, so your desktop never sees them. They need `/dev/uinput` and
`/dev/input` access (the `input` group) and skip with a message without it. Stop
the real app first: two engines would both react. Nothing in the suite types into a
real window; `test_inject.py --live` does, after a five-second countdown, and is
only for pointing at a scratch editor.

### Layout

```
install.sh         system setup, engine venv, shell build, install
run.sh             run from this checkout
uninstall.sh       remove what install.sh added to your home directory
packaging/         udev rule, module-load entry, launcher entry
engine/
  prompt_maxxer/
    hotkey.py      evdev reader: PTT detection, capture, optional grab
    keys.py        key names, and which keys may be the push-to-talk key
    audio.py       PortAudio stream, opened once at boot and never closed
    spectrum.py    voice bands for the visualizer
    asr/           swappable recognisers behind one narrow interface
    cleanup.py     filler/punctuation stage — the LLM slot
    inject.py      wtype for short text, wl-clipboard + paste for long
    ipc.py         event stream and commands between engine and shell
    pipeline.py    the loop everything else serves
    system.py      GPU, VRAM, CPU and memory detection
    models.py      model catalog, fit, recommendation, pinned revisions, downloads
    cuda_paths.py  loads the venv's CUDA libraries so CTranslate2 finds them
  requirements.lock  exact Python dependency versions
shell/
  src/             overlay pill, settings window, visualizer, models panel
  src-tauri/       tray, windows, engine supervision, autostart
    src/layer.rs     the overlay as a layer-shell surface
```

## Known constraints

* **Wayland only.** There is no X11 path: `wtype` and the layer-shell overlay are
  Wayland tools. Omarchy is Wayland.
* **Keyboard keys only.** Mouse side buttons cannot be the push-to-talk key yet.
* **The `input` group** (see above), or bind the key in Hyprland.
* **Typing into applications that ignore virtual keyboards** — a few games and
  remote-desktop clients — will not work; copy-paste mode would.
* **Recognition is not tuned to your vocabulary.** Names and jargon are often
  misheard — exactly the gap a personal dictionary fills.
* **Single utterance at a time.** Transcription is serialised so two fast utterances
  cannot interleave their injected text.
* **Downloads cannot be cancelled** once started.

## Roadmap

- [ ] Cleanup LLM in the stage that is already stubbed for it
- [ ] Personal dictionary, seeded by corrections
- [ ] Command mode: edit selected text by voice
- [ ] Parakeet TDT backend for English-only (better WER, faster)
- [ ] Mouse side buttons as the push-to-talk key (evdev makes this straightforward)
- [ ] A Quickshell overlay in place of the WebKit window, if the shell's memory matters

## License

MIT. See [`LICENSE`](LICENSE).
