"""Verify text injection without typing into any real window.

Injection drives other programs - wtype, wl-copy, wl-paste, hyprctl - so this
test puts stand-ins for all four first on PATH. Each stand-in records how it was
called, and the test checks what the injector asked for. Nothing here touches the
desktop, the clipboard or the keyboard, and it needs no Wayland session.

What it cannot show is that wtype types correctly into a real window; that is
what `python test_inject.py --live` is for. It counts down, then types a line
into whichever window has focus, so point it at a scratch editor first.
"""

import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import prompt_maxxer  # noqa: F401
from prompt_maxxer import inject as inj
from prompt_maxxer.config import InjectConfig

results: list[bool] = []


def check(name: str, ok, detail: str = "") -> None:
    results.append(bool(ok))
    print(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f"  ({detail})" if detail else ""))


def make_fakes(directory: Path, window_class: str, clipboard: str | None, pid: int | None = None) -> Path:
    """Executables that log their arguments and stdin to <name>.log."""
    for name in ("wtype", "wl-copy"):
        script = directory / name
        script.write_text(
            "#!/bin/sh\n"
            f'printf "%s\\n" "$*" >> "{directory}/{name}.args"\n'
            # Like the real tools, read stdin only when asked to: wtype's "-".
            + ('cat >> "%s/%s.stdin"\n' if name == "wl-copy" else 'for a; do [ "$a" = "-" ] && cat >> "%s/%s.stdin"; done; exit 0\n')
            % (directory, name)
        )
        script.chmod(script.stat().st_mode | stat.S_IEXEC)

    paste = directory / "wl-paste"
    if clipboard is None:
        paste.write_text("#!/bin/sh\nexit 1\n")
    else:
        paste.write_text(f"#!/bin/sh\nprintf %s '{clipboard}'\n")
    paste.chmod(paste.stat().st_mode | stat.S_IEXEC)

    hyprctl = directory / "hyprctl"
    hyprctl.write_text(f"#!/bin/sh\necho '{json.dumps({'class': window_class, **({'pid': pid} if pid else {})})}'\n")
    hyprctl.chmod(hyprctl.stat().st_mode | stat.S_IEXEC)
    return directory


def read(directory: Path, name: str) -> str:
    path = directory / name
    return path.read_text() if path.exists() else ""


def fresh(window_class: str = "firefox", clipboard: str | None = "previous clipboard", pid: int | None = None) -> Path:
    directory = Path(tempfile.mkdtemp(prefix="prompt-maxxer-inject-"))
    make_fakes(directory, window_class, clipboard, pid)
    os.environ["PATH"] = f"{directory}:{ORIGINAL_PATH}"
    return directory


ORIGINAL_PATH = os.environ["PATH"]


def run_fakes() -> None:
    cfg = InjectConfig()

    # -- short text is typed --------------------------------------------------------------
    d = fresh()
    strategy = inj.inject("hello world", cfg)
    check("short text is typed with wtype", strategy == "keystrokes" and read(d, "wtype.stdin") == "hello world",
          read(d, "wtype.stdin"))
    check("...reading from stdin, so a leading dash is not an option",
          read(d, "wtype.args").split() == ["-"], read(d, "wtype.args"))

    d = fresh()
    text = "- not an option, héllo wörld ✓ 你好 😀\nsecond line\r\n\tindented"
    inj.inject(text, cfg)
    typed = read(d, "wtype.stdin")
    check("Unicode and emoji pass through intact", typed.startswith("- not an option, héllo wörld ✓ 你好 😀"), repr(typed))
    check("newlines and tabs are pressed as Return and Tab, not typed as text",
          read(d, "wtype.args").split("\n")[:-1] == ["-", "-k Return", "-", "-k Return", "-k Tab", "-"]
          and typed == "- not an option, héllo wörld ✓ 你好 😀second lineindented",
          repr(read(d, "wtype.args")))

    d = fresh()
    inj.type_text("slow", delay_ms=5)
    check("a keystroke delay is passed to wtype", read(d, "wtype.args").split() == ["-d", "5", "-"], read(d, "wtype.args"))

    check("empty text does nothing", inj.inject("", cfg) == "noop")

    # -- long text is pasted --------------------------------------------------------------
    long_text = "word " * 40
    d = fresh(window_class="firefox")
    strategy = inj.inject(long_text, cfg)
    check("long text is pasted", strategy == "clipboard", strategy)
    check("...by putting it on the clipboard", read(d, "wl-copy.stdin").startswith(long_text),
          read(d, "wl-copy.stdin")[:40])
    check("...and sending Ctrl+V in an ordinary window",
          read(d, "wtype.args").split() == "-M ctrl -k v -m ctrl".split(), read(d, "wtype.args"))
    time.sleep(cfg.restore_delay_ms / 1000 + 0.3)
    check("...then restores what was on the clipboard before",
          read(d, "wl-copy.stdin").endswith("previous clipboard"), read(d, "wl-copy.stdin")[-30:])

    for terminal in ("kitty", "Alacritty", "com.mitchellh.ghostty", "foot"):
        d = fresh(window_class=terminal)
        inj.inject(long_text, InjectConfig(restore_clipboard=False))
        check(f"a terminal ({terminal}) gets Ctrl+Shift+V",
              read(d, "wtype.args").split() == "-M ctrl -M shift -k v -m shift -m ctrl".split(),
              read(d, "wtype.args"))

    # A launcher can give a terminal any class (Omarchy: --app-id=org.omarchy.codex), and
    # Codex CLI answers Ctrl+V with "no image on the clipboard". The process decides.
    for process, expected in (("kitty", "ctrl+shift+v"), ("firefox", "ctrl+v")):
        scratch = Path(tempfile.mkdtemp(prefix="prompt-maxxer-proc-"))
        binary = scratch / process
        binary.symlink_to(shutil.which("sleep"))
        child = subprocess.Popen([str(binary), "30"])
        try:
            d = fresh(window_class="org.omarchy.codex", pid=child.pid)
            inj.inject(long_text, InjectConfig(restore_clipboard=False))
            want = ("-M ctrl -M shift -k v -m shift -m ctrl" if expected == "ctrl+shift+v" else "-M ctrl -k v -m ctrl").split()
            check(f"an unfamiliar class (org.omarchy.codex) owned by {process} gets {expected}",
                  read(d, "wtype.args").split() == want, read(d, "wtype.args"))
        finally:
            child.kill()
            child.wait()

    d = fresh(window_class="kitty")
    inj.inject(long_text, InjectConfig(paste_shortcut="shift+insert", restore_clipboard=False))
    check("a forced shortcut overrides the window class",
          read(d, "wtype.args").split() == "-M shift -k Insert -m shift".split(), read(d, "wtype.args"))

    d = fresh(clipboard=None)
    inj.inject(long_text, cfg)
    time.sleep(cfg.restore_delay_ms / 1000 + 0.3)
    check("an empty or non-text clipboard is left as the dictation, not overwritten with nothing",
          read(d, "wl-copy.stdin").count("word") == 40 and not read(d, "wl-copy.stdin").endswith("\n\n"),
          f"{len(read(d, 'wl-copy.stdin'))} bytes written")

    # -- missing tools ------------------------------------------------------------------------
    os.environ["PATH"] = tempfile.mkdtemp(prefix="prompt-maxxer-empty-")
    try:
        inj.inject("hello", cfg)
        check("a missing wtype is reported with the fix", False, "no error raised")
    except inj.InjectionUnavailable as exc:
        check("a missing wtype is reported with the fix", "pacman -S wtype" in str(exc), str(exc))


def run_live() -> None:
    print("Typing into the focused window in 5 seconds. Focus a scratch editor now.")
    for n in range(5, 0, -1):
        print(f"  {n}...", flush=True)
        time.sleep(1)
    inj.inject("Live check: héllo wörld ✓ 你好", InjectConfig())
    print("done. The line above should have appeared in that window.")


if __name__ == "__main__":
    if "--live" in sys.argv:
        run_live()
        raise SystemExit(0)
    run_fakes()
    print(f"\n{sum(results)}/{len(results)} passed")
    raise SystemExit(0 if all(results) else 1)
