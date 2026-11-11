import shutil
import subprocess
import wave

import pytest

from lecture_recon import audio
from lecture_recon.__main__ import main

pytestmark = pytest.mark.skipif(not (shutil.which("ffmpeg") and shutil.which("ffprobe")), reason="ffmpeg 없음")


def tone(path, seconds=1.0):
    path.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-f", "lavfi",
                    "-i", f"sine=frequency=440:duration={seconds}", "-ac", "2", "-ar", "44100", str(path)], check=True)


def test_to_wav_16k_mono(tmp_path):
    src, dst = tmp_path / "in.m4a", tmp_path / "out" / "01.wav"
    tone(src)
    audio.to_wav(src, dst)
    with wave.open(str(dst)) as w:
        assert (w.getnchannels(), w.getframerate()) == (1, 16000)
    assert abs(audio.duration(dst) - 1.0) < 0.1


def test_ingest_command(event_dir):
    for nn in ("01", "02"):
        tone(event_dir / "raw" / "audio" / f"{nn}.m4a")
    assert main(["ingest", str(event_dir)]) == 0
    assert (event_dir / "work" / "audio" / "01.wav").exists()
    assert (event_dir / "work" / "audio" / "02.wav").exists()
    assert (event_dir / "work" / "photos" / "01.json").exists()
    assert len(list((event_dir / "synth-day-2026-notes" / "slides" / "01").glob("*.jpg"))) == 3
