"""Exercise the controls the settings window drives, against a real engine.

Covers the Origin check, the connect snapshot, the idle mic monitor, hotkey
capture (refused, saved, cancelled) and push-to-talk on the new key, the tape
log and clearing it, model recommendation, switching models and back, and
downloading a model.

Keys are synthesised with a virtual keyboard (uinput_keys.py), which the engine
grabs so the desktop never sees them, and recordings are forced to be discarded
as silence, so nothing is typed into other windows. Runs on its own port with a
throwaway config file, so the real app's settings are untouched. Stop the real
app first: two engines would both react.
"""

import json
import tempfile
import time
from pathlib import Path

from evdev import ecodes as ec
from websockets.exceptions import InvalidStatus
from websockets.sync.client import connect

import prompt_maxxer  # noqa: F401
from prompt_maxxer import models
from prompt_maxxer.config import Config
from prompt_maxxer.pipeline import Pipeline
from uinput_keys import VirtualKeyboard, access_problem

PORT = 8799
URL = f"ws://127.0.0.1:{PORT}"
APP_ORIGIN = "http://tauri.localhost"

KEY_ESC, KEY_A, KEY_F13, KEY_RCTRL = ec.KEY_ESC, ec.KEY_A, ec.KEY_F13, ec.KEY_RIGHTCTRL
START_WITH = "small.en"
SWITCH_TO = "tiny.en"  # ~75 MB, downloaded first if it is not on disk
kbd: VirtualKeyboard

results: list[tuple[str, bool]] = []


def check(name: str, ok, detail: str = "") -> None:
    results.append((name, bool(ok)))
    print(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f"  ({detail})" if detail else ""))


def key(code: int, up: bool = False) -> None:
    kbd.key(code, up)


def tap(code: int, hold: float = 0.05) -> None:
    kbd.tap(code, hold)


def drain(ws, seconds: float) -> list[dict]:
    events, end = [], time.monotonic() + seconds
    while (remaining := end - time.monotonic()) > 0:
        try:
            events.append(json.loads(ws.recv(timeout=remaining)))
        except TimeoutError:
            break
    return events


def wait_for(ws, predicate, timeout: float) -> dict | None:
    end = time.monotonic() + timeout
    while (remaining := end - time.monotonic()) > 0:
        try:
            event = json.loads(ws.recv(timeout=remaining))
        except TimeoutError:
            return None
        if predicate(event):
            return event
    return None


def send(ws, **command) -> None:
    ws.send(json.dumps(command))


def capture_and_press(app, code: int) -> dict | None:
    send(app, cmd="capture_hotkey")
    if not wait_for(app, lambda e: e["event"] == "hotkey_capture" and e["status"] == "listening", 3):
        return None
    tap(code)
    return wait_for(app, lambda e: e["event"] == "hotkey_capture" and e["status"] != "listening", 3)


def run(pipeline: Pipeline, cfg_path: Path) -> None:
    # -- origin -----------------------------------------------------------
    try:
        connect(URL, origin="https://evil.example").close()
        check("a web page's origin is refused", False, "connection was accepted")
    except InvalidStatus as exc:
        check("a web page's origin is refused", exc.response.status_code == 403, f"HTTP {exc.response.status_code}")

    app = connect(URL, origin=APP_ORIGIN)
    snapshot = drain(app, 1.5)
    kinds = [e["event"] for e in snapshot]
    check("the app's origin connects and receives a full snapshot",
          {"ready", "state", "log", "models"} <= set(kinds), ", ".join(kinds))

    ready = next(e for e in snapshot if e["event"] == "ready")
    catalog = next(e for e in snapshot if e["event"] == "models")
    print(f"   system: {catalog['system']}")
    print(f"   recommended: {catalog['recommended']} - {catalog['reason']}")
    print("   fits: " + ", ".join(f"{m['id']}={m['fit']}{'*' if m['downloaded'] else ''}" for m in catalog["models"]))
    check("ready names the active model", ready.get("model") == START_WITH, str(ready.get("model")))
    check("recommends a model that fits this machine", catalog["recommended"] in {m["id"] for m in catalog["models"]})
    on_disk = {m["id"] for m in catalog["models"] if m["downloaded"]}
    check("finds the model already on disk", START_WITH in on_disk, str(sorted(on_disk)))

    # -- idle monitor ---------------------------------------------------------
    monitor = connect(URL, origin=APP_ORIGIN)
    drain(monitor, 0.5)
    send(monitor, cmd="monitor", on=True)
    levels = [e for e in drain(monitor, 1.0) if e["event"] == "level"]
    stray = [e for e in drain(app, 0.3) if e["event"] == "level"]
    check("idle level frames reach a window that asked", len(levels) >= 10, f"{len(levels)} frames in 1s")
    check("...but not windows that did not", not stray, f"{len(stray)} stray frames")
    check("level frames carry peak and 16 voice bands",
          levels and all("peak" in e and len(e.get("bands", [])) == 16 for e in levels))
    monitor.close()

    # -- hotkey capture ------------------------------------------------------------
    # Anything recorded below is discarded as silence, so no test run can type
    # into whatever window has focus.
    pipeline.cfg.audio.min_rms = 10.0

    event = capture_and_press(app, KEY_A)
    check("a letter key is refused", event and event["status"] == "refused" and event["label"] == "A", str(event))

    event = capture_and_press(app, KEY_F13)
    check("F13 is accepted", event and event["status"] == "saved" and event["label"] == "F13", str(event))
    ready = wait_for(app, lambda e: e["event"] == "ready", 3)
    check("ready announces the new key", ready and ready["hotkey"] == "F13", str(ready))
    saved = json.loads(cfg_path.read_text(encoding="utf-8"))["hotkey"]
    check("the new key is written to config", saved["key_code"] == KEY_F13 and saved["label"] == "F13", str(saved))

    event = capture_and_press(app, KEY_ESC)
    check("Esc cancels capture", event and event["status"] == "cancelled", str(event))
    check("...and keeps the key", pipeline.hook.key_code == KEY_F13)

    drain(app, 0.3)
    key(KEY_F13)
    time.sleep(1.0)
    key(KEY_F13, up=True)
    states = [e["state"] for e in drain(app, 2.5) if e["event"] == "state"]
    check("F13 now drives push-to-talk", "recording" in states and states[-1] == "idle", str(states))

    tap(KEY_RCTRL, hold=0.6)
    states = [e["state"] for e in drain(app, 1.5) if e["event"] == "state"]
    check("Right Ctrl no longer records", "recording" not in states, str(states))

    # -- tape log --------------------------------------------------------------------
    pipeline._record_take("First test take.", 1.2, 100, 120, "keystrokes")
    pipeline._record_take("Second test take.", 2.4, 140, 160, "keystrokes")
    late = connect(URL, origin=APP_ORIGIN)
    log_event = next((e for e in drain(late, 1.0) if e["event"] == "log"), None)
    late.close()
    texts = [t["text"] for t in (log_event or {}).get("takes", [])]
    check("a window opened later receives the session's takes",
          texts[-2:] == ["First test take.", "Second test take."], str(texts))

    send(app, cmd="clear_log")
    cleared = wait_for(app, lambda e: e["event"] == "log", 3)
    check("clear_log empties the tape log", cleared is not None and cleared["takes"] == [], str(cleared))
    late = connect(URL, origin=APP_ORIGIN)
    log_event = next((e for e in drain(late, 1.0) if e["event"] == "log"), None)
    late.close()
    check("...for windows opened afterwards too", log_event is not None and log_event["takes"] == [])
    pipeline._record_take("After clearing.", 1.0, 90, 100, "keystrokes")
    check("take numbering restarts after a clear", pipeline._takes[-1]["n"] == 1)

    # -- downloading ----------------------------------------------------------------------
    if models.is_downloaded(SWITCH_TO):
        print(f"   {SWITCH_TO} is already on disk; skipping the download checks")
    else:
        started = time.monotonic()
        send(app, cmd="download_model", model=SWITCH_TO)
        progress, finished = [], None
        while time.monotonic() - started < 300:
            event = wait_for(app, lambda e: e["event"] == "model_download", 300)
            if event is None:
                break
            if event["status"] == "downloading":
                progress.append((event.get("done_bytes") or 0, event.get("total_bytes")))
            else:
                finished = event
                break

        check(f"downloads {SWITCH_TO}", finished and finished["status"] == "done",
              f"{finished} in {time.monotonic() - started:.1f}s")
        moving = sorted({done for done, _ in progress if done > 0})
        total = next((t for _, t in progress if t), None)
        check("reports progress while downloading", len(moving) >= 2,
              f"{len(progress)} updates, {len(moving)} distinct sizes, total {total}")
        listed = wait_for(app, lambda e: e["event"] == "models", 10)
        row = next((m for m in (listed or {}).get("models", []) if m["id"] == SWITCH_TO), None)
        check("the downloaded model is listed as on disk", row and row["downloaded"] and row["size_exact"], str(row))

    # -- switching models --------------------------------------------------------------
    send(app, cmd="switch_model", model="large-v3")
    event = wait_for(app, lambda e: e["event"] == "model_switch", 5)
    if models.is_downloaded("large-v3"):
        print("   large-v3 is on disk, so the refusal check does not apply")
    else:
        check("switching to a model that is not on disk is refused",
              event and event["status"] == "error", str(event))

    for model_id in (SWITCH_TO, START_WITH):
        started = time.monotonic()
        send(app, cmd="switch_model", model=model_id)
        event = wait_for(app, lambda e: e["event"] == "model_switch" and e["status"] != "loading", 120)
        ready = wait_for(app, lambda e: e["event"] == "ready", 5)
        check(f"switches to {model_id}",
              event and event["status"] == "done" and ready and ready["model"] == model_id,
              f"{event} in {time.monotonic() - started:.1f}s")
        written = json.loads(cfg_path.read_text(encoding="utf-8"))["asr"]
        check(f"...and saves {model_id} with a GPU placement",
              written["model"] == model_id and written["device"] == "cuda", f"{written['model']} on {written['device']} ({written['compute_type']})")

    app.close()


def main() -> int:
    global kbd
    problem = access_problem()
    if problem:
        print(f"SKIPPED: {problem}.\nRun ./install.sh --system-only, then log out and back in.")
        return 0
    cfg_path = Path(tempfile.mkdtemp(prefix="prompt-maxxer-test-")) / "config.json"
    cfg = Config.load(cfg_path)
    cfg.ipc.port = PORT
    cfg.asr.model = START_WITH
    # The hook grabs every keyboard it reads, the virtual one included, so test
    # keys reach the engine and not the desktop.
    cfg.hotkey.suppress = True
    pipeline = Pipeline(cfg, config_path=cfg_path)

    print("starting engine...")
    pipeline.start()
    kbd = VirtualKeyboard(pipeline)
    try:
        run(pipeline, cfg_path)
    finally:
        kbd.close()
        pipeline.stop()

    failed = [name for name, ok in results if not ok]
    print(f"\n{len(results) - len(failed)}/{len(results)} passed")
    for name in failed:
        print(f"  FAILED: {name}")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
