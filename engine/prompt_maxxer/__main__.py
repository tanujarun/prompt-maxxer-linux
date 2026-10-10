"""Engine entry point."""

from __future__ import annotations

import argparse
import logging
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path

from .audio import list_input_devices
from .config import Config, config_dir, state_dir
from .pipeline import Pipeline

_instance_lock = None


def _claim_single_instance() -> bool:
    """One engine per session. A second would grab the keyboards again, and
    both engines would record and type every take."""
    global _instance_lock
    import fcntl
    import os

    runtime = os.environ.get("XDG_RUNTIME_DIR") or "/tmp"
    # Held for the life of the process and released by the kernel when it
    # exits, however it exits, so a crash never leaves a stale lock behind.
    _instance_lock = open(os.path.join(runtime, "prompt-maxxer-engine.lock"), "w")
    try:
        fcntl.flock(_instance_lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        return False
    return True


def _configure_logging(verbose: bool) -> Path:
    """Log to stderr and to a rotating file.

    The file matters most on other people's machines: the engine runs hidden
    behind the app, so when something goes wrong there, the log is the only
    record of what happened.
    """
    log_dir = state_dir() / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    log_file = log_dir / "engine.log"

    handlers: list[logging.Handler] = [
        RotatingFileHandler(log_file, maxBytes=1_000_000, backupCount=3, encoding="utf-8")
    ]
    if sys.stderr is not None:
        handlers.append(logging.StreamHandler())

    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)-22s %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        handlers=handlers,
    )
    # -v is for debugging Prompt Maxxer, not for watching huggingface_hub
    # negotiate HTTP/1.1. These drown the log at DEBUG and hide what matters.
    for noisy in ("httpx", "httpcore", "urllib3", "huggingface_hub", "filelock"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    return log_file


def _transcribe_file(path: str, cfg: Config) -> int:
    """Diagnostic: transcribe a WAV with the configured model, print it, exit.

    Exercises the whole recognition stack - CUDA libraries, model load, a real
    forward pass - without a microphone, a hotkey or the app, so it can check
    an installed build on any machine.
    """
    import time
    import wave

    import numpy as np

    from .asr import build_engine

    with wave.open(path, "rb") as wav:
        rate, channels, width = wav.getframerate(), wav.getnchannels(), wav.getsampwidth()
        raw = wav.readframes(wav.getnframes())
    if width != 2:
        print("expected a 16-bit PCM WAV file")
        return 2

    audio = np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0
    if channels > 1:
        audio = audio.reshape(-1, channels).mean(axis=1)
    if rate != 16000:
        import soxr

        audio = soxr.resample(audio, rate, 16000).astype(np.float32)

    engine = build_engine(cfg.asr)
    started = time.perf_counter()
    engine.load()
    loaded = time.perf_counter() - started
    result = engine.transcribe(audio, 16000)

    print(f"model: {cfg.asr.model} on {engine.device_in_use} ({cfg.asr.compute_type}), loaded in {loaded:.1f}s")
    print(f"audio: {result.audio_s:.1f}s transcribed in {result.latency_s * 1000:.0f} ms")
    print(f"text:  {result.text}")
    return 0 if result.text.strip() else 1


def _exit_with_parent() -> None:
    """Shut down when stdin closes.

    The shell starts the engine with a pipe on stdin and keeps the other end
    open for as long as it lives. The kernel closes it when the shell dies,
    however it dies, so an engine can never outlive its interface - which would
    leave a keyboard it may have grabbed with nothing to let it go.
    """
    import os
    import signal
    import threading

    def watch() -> None:
        try:
            while sys.stdin.buffer.read(4096):
                pass
        except (OSError, ValueError):
            pass
        os.kill(os.getpid(), signal.SIGTERM)

    threading.Thread(target=watch, name="prompt-maxxer-parent-watch", daemon=True).start()


def _send_ptt(action: str, cfg: Config) -> int:
    """Press or release push-to-talk in the running engine, then exit.

    For binding the key in Hyprland itself rather than reading it from evdev,
    which needs no `input` group membership:

        bind  = , F13, exec, prompt-maxxer-engine --ptt press
        bindr = , F13, exec, prompt-maxxer-engine --ptt release
    """
    import json

    from websockets.sync.client import connect

    try:
        with connect(f"ws://{cfg.ipc.host}:{cfg.ipc.port}", open_timeout=2) as ws:
            ws.send(json.dumps({"cmd": f"ptt_{action}"}))
    except OSError as exc:
        print(f"the engine is not running: {exc}", file=sys.stderr)
        return 1
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(prog="prompt-maxxer-engine", description="Prompt Maxxer dictation engine")
    parser.add_argument("--list-devices", action="store_true", help="list input devices and exit")
    parser.add_argument("--transcribe", metavar="WAV", help="transcribe a WAV file with the configured model and exit")
    parser.add_argument("--ptt", choices=("press", "release"), help="press or release push-to-talk in the running engine and exit")
    parser.add_argument("--die-with-parent", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--device", help="input device index or name substring")
    parser.add_argument("--model", help="override the ASR model")
    parser.add_argument("--cpu", action="store_true", help="force CPU inference")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args()

    if args.list_devices:
        for dev in list_input_devices():
            print(f"[{dev['index']:>2}] {dev['name']}  ({dev['default_samplerate']:.0f} Hz)")
        return 0

    if args.ptt:
        return _send_ptt(args.ptt, Config.load())

    log_file = _configure_logging(args.verbose)
    log = logging.getLogger(__name__)

    if args.transcribe:
        # Read the config without creating one, so a diagnostic run on a fresh
        # machine does not use up its first-run setup.
        existing = config_dir() / "config.json"
        cfg = Config.load() if existing.exists() else Config()
    else:
        if not _claim_single_instance():
            log.error("the engine is already running; exiting")
            return 0
        cfg = Config.load()

    if args.device is not None:
        cfg.audio.device = int(args.device) if args.device.isdigit() else args.device
    if args.model:
        cfg.asr.model = args.model
    if args.cpu:
        cfg.asr.device = "cpu"
        cfg.asr.compute_type = "int8"

    if args.transcribe:
        if cfg.asr.device == "cuda":
            from . import cuda_runtime

            if not cuda_runtime.available():
                print("CUDA libraries are not installed in this environment (./install.sh adds them); "
                      "checking on the CPU instead.")
                cfg.asr.device = "cpu"
                cfg.asr.compute_type = "int8"
        return _transcribe_file(args.transcribe, cfg)

    import signal

    def _terminate(_signum, _frame):
        # Unwinds through pipeline.stop(), which hands the keyboards back.
        raise KeyboardInterrupt

    signal.signal(signal.SIGTERM, _terminate)
    if args.die_with_parent:
        _exit_with_parent()

    frozen = getattr(sys, "frozen", False)
    log.info("config: %s | log: %s | %s", config_dir() / "config.json", log_file,
             "frozen build" if frozen else "source checkout")

    # On the first launch, pick the model for this machine - unless one was
    # asked for explicitly on the command line.
    first_run = cfg.first_run and not args.model and not args.cpu
    pipeline = Pipeline(cfg, first_run=first_run)
    try:
        pipeline.start()
        pipeline.run_forever()
    except KeyboardInterrupt:
        pass
    except Exception:
        log.exception("engine stopped")
        return 1
    finally:
        pipeline.stop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
