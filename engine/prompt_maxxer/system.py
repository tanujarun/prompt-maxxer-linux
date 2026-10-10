"""What this machine can run: GPU, VRAM, CPU and memory.

Read once at startup. nvidia-smi is the source of VRAM, and CTranslate2 is the
authority on whether CUDA inference will actually work.
"""

from __future__ import annotations

import logging
import os
import subprocess
from dataclasses import asdict, dataclass, field

log = logging.getLogger(__name__)


@dataclass
class SystemInfo:
    cpu: str
    threads: int
    ram_gb: float
    gpu: str | None
    vram_gb: float | None
    cuda: bool
    cuda_compute_types: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)


def _cpu_name() -> str:
    try:
        with open("/proc/cpuinfo", encoding="utf-8") as cpuinfo:
            for line in cpuinfo:
                if line.startswith("model name"):
                    return line.split(":", 1)[1].strip()
    except OSError:
        pass
    import platform

    return platform.processor() or "Unknown CPU"


def _ram_gb() -> float:
    try:
        with open("/proc/meminfo", encoding="utf-8") as meminfo:
            for line in meminfo:
                if line.startswith("MemTotal:"):
                    return round(int(line.split()[1]) / 2**20, 1)  # kB -> GiB
    except (OSError, ValueError, IndexError):
        pass
    return 0.0


def _nvidia_gpu() -> tuple[str | None, float | None]:
    try:
        result = subprocess.run(
            ["nvidia-smi", "--query-gpu=name,memory.total", "--format=csv,noheader,nounits"],
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        return None, None
    if result.returncode != 0 or not result.stdout.strip():
        return None, None

    # First GPU only: faster-whisper runs on device 0 unless told otherwise.
    parts = [part.strip() for part in result.stdout.strip().splitlines()[0].split(",")]
    name = parts[0] or None
    try:
        return name, round(float(parts[1]) / 1024, 1)
    except (IndexError, ValueError):
        return name, None


def detect() -> SystemInfo:
    import ctranslate2

    try:
        cuda = ctranslate2.get_cuda_device_count() > 0
    except Exception:
        cuda = False
    compute_types = sorted(ctranslate2.get_supported_compute_types("cuda")) if cuda else []

    gpu, vram = _nvidia_gpu()
    info = SystemInfo(
        cpu=_cpu_name(),
        threads=os.cpu_count() or 1,
        ram_gb=_ram_gb(),
        gpu=gpu,
        vram_gb=vram,
        cuda=cuda,
        cuda_compute_types=compute_types,
    )
    log.info("system: %s", info)
    return info
