"""End-to-end smoke test of the engine loop, with no human in it.

Starts the real pipeline, connects a WebSocket client the way the shell does,
then synthesises a Right Ctrl press/release on a virtual keyboard (see
uinput_keys.py), which the engine reads exactly as it reads a real one. That
exercises the whole path: hook -> capture -> recognise -> emit.

Audio is whatever the microphone hears, so the transcript is expected to be
empty. What is under test is the state machine and the wiring.
"""

import asyncio
import json
import tempfile
import threading
import time
from pathlib import Path

import websockets
from evdev import ecodes as ec

import prompt_maxxer  # noqa: F401
from prompt_maxxer import inject as inject_mod
from prompt_maxxer.config import Config
from prompt_maxxer.pipeline import Pipeline
from uinput_keys import VirtualKeyboard, access_problem

seen: list[dict] = []


def tap_hotkey(kbd: VirtualKeyboard, hold_s: float) -> None:
    """Synthesise a real Right Ctrl press and release."""
    kbd.tap(ec.KEY_RIGHTCTRL, hold_s)


async def listen(stop: asyncio.Event) -> None:
    async with websockets.connect("ws://127.0.0.1:8765") as ws:
        while not stop.is_set():
            try:
                raw = await asyncio.wait_for(ws.recv(), timeout=0.5)
            except asyncio.TimeoutError:
                continue
            event = json.loads(raw)
            if event.get("event") != "level":  # too chatty to log
                seen.append(event)
                print(f"  <- {event}")


def main() -> int:
    problem = access_problem()
    if problem:
        print(f"SKIPPED: {problem}.\nRun ./install.sh --system-only, then log out and back in.")
        return 0
    # A throwaway config, so the real settings are untouched, and a stubbed
    # injector, so whatever the microphone hears is never typed anywhere.
    cfg_path = Path(tempfile.mkdtemp(prefix="prompt-maxxer-pipeline-")) / "config.json"
    cfg = Config.load(cfg_path)
    cfg.asr.model = "small.en"
    cfg.audio.min_rms = 0.0  # a quiet room must still reach the model; the gate is tested elsewhere
    inject_mod.inject = lambda text, inject_cfg: "stub"
    cfg.hotkey.key_code = ec.KEY_RIGHTCTRL
    cfg.hotkey.suppress = True  # grabs the virtual keyboard, so the desktop never sees it
    # Keep the model out of the way of what is being tested here.
    print(f"hotkey: {cfg.hotkey.label}  engine: {cfg.asr.model} on {cfg.asr.device}")

    pipeline = Pipeline(cfg, config_path=cfg_path)
    print("starting pipeline (loads the model)...")
    t0 = time.perf_counter()
    pipeline.start()
    print(f"ready in {time.perf_counter() - t0:.1f}s\n")
    kbd = VirtualKeyboard(pipeline)

    stop = asyncio.Event()
    loop = asyncio.new_event_loop()

    def run_client() -> None:
        asyncio.set_event_loop(loop)
        loop.run_until_complete(listen(stop))

    client = threading.Thread(target=run_client, daemon=True)
    client.start()
    time.sleep(1.0)  # let the socket connect

    print("simulating a 1.5s push-to-talk press...")
    tap_hotkey(kbd, 1.5)
    time.sleep(3.0)

    loop.call_soon_threadsafe(stop.set)
    kbd.close()
    pipeline.stop()

    states = [e["state"] for e in seen if e.get("event") == "state"]
    print(f"\nstates observed: {states}")

    checks = [
        ("hook fired, capture began", "recording" in states),
        ("capture ended, ASR ran", "transcribing" in states),
        ("returned to idle", states and states[-1] == "idle"),
        ("shell received events", len(seen) > 0),
    ]
    print()
    failed = 0
    for label, ok in checks:
        if not ok:
            failed += 1
        print(f"[{'PASS' if ok else 'FAIL'}] {label}")
    print(f"\n{len(checks) - failed}/{len(checks)} passed")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
