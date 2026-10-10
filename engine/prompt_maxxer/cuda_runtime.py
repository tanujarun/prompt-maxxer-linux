"""The NVIDIA GPU libraries, as the Linux build handles them.

On Windows the installed app downloads cuBLAS and cuDNN on first launch, because
an installer cannot hold them. Nothing of the kind is needed here: install.sh
puts NVIDIA's wheels in the engine's virtualenv at exact pinned versions, and
cuda_paths.py loads them. This module only reports whether they are there.
"""

from __future__ import annotations

from . import cuda_paths

#: Kept so callers written for the download flow still see a total; there is
#: nothing to download.
TOTAL_BYTES = 0


def available() -> bool:
    """Whether every library CUDA inference needs could be loaded."""
    loaded = cuda_paths.register()
    return all(stem in loaded for stem in cuda_paths.REQUIRED)


def install(_on_progress=None):
    raise RuntimeError(
        "The CUDA libraries are not in the engine's virtualenv. Re-run ./install.sh to install them."
    )
