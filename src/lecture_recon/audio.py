"""ffmpeg/ffprobe 호출 (subprocess)."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

from .config import LectureReconError


def _tool(name: str) -> str:
    path = shutil.which(name)
    if not path:
        raise LectureReconError(f"{name} 를 찾을 수 없다. PATH 에 {name} 가 있어야 한다.")
    return path


def to_wav(src: Path, dst: Path) -> None:
    """src 를 16 kHz mono PCM WAV 로 변환한다."""
    dst.parent.mkdir(parents=True, exist_ok=True)
    cmd = [
        _tool("ffmpeg"), "-hide_banner", "-loglevel", "error", "-y",
        "-i", str(src), "-vn", "-ac", "1", "-ar", "16000", "-c:a", "pcm_s16le", str(dst),
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        dst.unlink(missing_ok=True)
        raise LectureReconError(f"ffmpeg 변환 실패 ({src}): {proc.stderr.strip()}")


def duration(path: Path) -> float:
    """미디어 길이(초)."""
    cmd = [
        _tool("ffprobe"), "-v", "error", "-show_entries", "format=duration",
        "-of", "default=noprint_wrappers=1:nokey=1", str(path),
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True)
    try:
        return float(proc.stdout.strip())
    except ValueError:
        raise LectureReconError(f"ffprobe 로 길이를 읽지 못했다 ({path}): {proc.stderr.strip()}") from None
