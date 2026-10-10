"""Make the CUDA libraries visible to CTranslate2 on Linux.

CTranslate2's CUDA build loads cuBLAS and cuDNN lazily, with dlopen, the first
time a model touches the GPU. The system's CUDA is no use: Arch ships CUDA 13,
and CTranslate2 needs the CUDA 12 builds. So the engine's virtualenv carries
NVIDIA's own wheels (nvidia-cublas-cu12, nvidia-cudnn-cu12, nvidia-cuda-nvrtc-cu12,
pinned in requirements.lock), which put their libraries under
site-packages/nvidia/*/lib.

dlopen finds libraries through LD_LIBRARY_PATH, but glibc reads that once, at
process start, so setting it from Python is too late. What does work is loading
each library by full path first: glibc matches a later dlopen("libcublas.so.12")
against libraries already loaded by SONAME, and finds these.

Runs at import, before a model is loaded.
"""

from __future__ import annotations

import ctypes
import logging
import os
import sys
from pathlib import Path

log = logging.getLogger(__name__)

#: Libraries CTranslate2 and cuDNN look up by name, in the order they must load:
#: each one's own dependencies come before it.
LIBRARIES = (
    "libnvrtc-builtins.so",
    "libnvrtc.so",
    "libcublasLt.so",
    "libcublas.so",
    "libcudnn_graph.so",
    "libcudnn_ops.so",
    "libcudnn_cnn.so",
    "libcudnn_heuristic.so",
    "libcudnn_engines_precompiled.so",
    "libcudnn_engines_runtime_compiled.so",
    "libcudnn.so",
)

#: Every one of these must load for CUDA inference to work.
REQUIRED = ("libcublas.so", "libcublasLt.so", "libcudnn.so", "libnvrtc.so")

_loaded: dict[str, str] = {}


def _library_dirs() -> list[Path]:
    dirs: list[Path] = []
    override = os.environ.get("PROMPT_MAXXER_CUDA_DIR")
    if override:
        dirs.append(Path(override))
    for entry in sys.path:
        root = Path(entry) / "nvidia" if entry else None
        if root is not None and root.is_dir():
            dirs.extend(sorted(root.glob("*/lib")))
    return dirs


def _find(stem: str, dirs: list[Path]) -> Path | None:
    for directory in dirs:
        # Newest version suffix first: libcublas.so.12 over a stray .so.11.
        for candidate in sorted(directory.glob(stem + ".*"), reverse=True):
            if candidate.is_file():
                return candidate
    return None


def register() -> list[str]:
    """Load every CUDA library found. Safe to call repeatedly.

    Returns the stems of the libraries that are loaded.
    """
    dirs = _library_dirs()
    for stem in LIBRARIES:
        if stem in _loaded:
            continue
        path = _find(stem, dirs)
        if path is None:
            continue
        try:
            ctypes.CDLL(str(path), mode=ctypes.RTLD_GLOBAL)
        except OSError as exc:
            log.debug("could not load %s: %s", path, exc)
            continue
        _loaded[stem] = str(path)
        log.debug("loaded %s", path)
    return list(_loaded)
