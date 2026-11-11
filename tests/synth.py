"""테스트용 합성 행사: 발표 2건(01 = EXIF 사진 3장, 02 = 사진 없음) + 가짜 ASR json.

01 문단 (침묵 ≥1.5초로 끊김): 01-001 @0s, 01-002 @11s(불명확 1개), 01-003 @22s.
01 사진 촬영 시각 = 녹음 시작(10:05:00) + 2s / 13s / 40s → 01-001 / 01-002 / 01-003.
"""

from __future__ import annotations

import json
from pathlib import Path

from PIL import Image

from lecture_recon import photos
from lecture_recon.__main__ import main
from lecture_recon.config import load_event

SLUG = "synth-day-2026"

EVENT_YAML = f"""\
slug: {SLUG}
name: 합성 테크 데이
date: 2026-10-20
timezone: Asia/Seoul
venue: 테스트홀
organizer: DAIS
sources: [https://example.org/event]
reconstructor: 홍길동
talks:
  - number: 1
    speaker: 이희석
    org: 42dot
    title: "From Data to Driving"
    language: ko
    recordings:
      - file: raw/audio/01.m4a
        started_at: "2026-10-20T10:05:00"
        offset_sec: 0
    photos_dir: raw/photos/01
  - number: 2
    speaker: 김 철수
    org: 서울대
    title: "Robot Foundation Models"
    language: en
    recordings:
      - file: raw/audio/02.m4a
"""

ASR_01 = [
    {"start": 0.0, "end": 4.0, "text": " 안녕하세요 42dot 이희석입니다.", "avg_logprob": -0.2, "no_speech_prob": 0.01},
    {"start": 4.2, "end": 9.0, "text": " 오늘은 데이터에서 주행까지를 이야기합니다.", "avg_logprob": -0.3, "no_speech_prob": 0.02},
    {"start": 11.0, "end": 15.0, "text": " 첫 번째 주제는 데이터 수집입니다.", "avg_logprob": -0.25, "no_speech_prob": 0.01},
    {"start": 15.1, "end": 20.0, "text": " 엔드투엔드 오토 라벨링", "avg_logprob": -1.4, "no_speech_prob": 0.1},
    {"start": 22.0, "end": 30.0, "text": " 두 번째는 모델 학습입니다.", "avg_logprob": -0.2, "no_speech_prob": 0.01},
]
ASR_02 = [
    {"start": 0.5, "end": 6.0, "text": " Robot foundation models need data.", "avg_logprob": -0.3, "no_speech_prob": 0.01},
    {"start": 6.2, "end": 12.0, "text": " We scale teleoperation.", "avg_logprob": -0.4, "no_speech_prob": 0.02},
]
PHOTO_TIMES = ["2026:10:20 10:05:02", "2026:10:20 10:05:13", "2026:10:20 10:05:40"]


def asr_json(segments: list[dict], duration: float) -> dict:
    return {"audio": "", "source": "", "model": "large-v3", "device": "cpu", "compute_type": "auto",
            "created": "2026-10-21", "language": "ko", "duration": duration, "segments": segments}


def write_photo(path: Path, exif_time: str | None, size=(2000, 1500)) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    im = Image.new("RGB", size, (40, 90, 160))
    exif = Image.Exif()
    if exif_time:
        exif.get_ifd(0x8769)[36867] = exif_time
    im.save(path, "JPEG", exif=exif)


def build_event(root: Path) -> Path:
    """root/<SLUG>/ 에 event.yaml, 사진 3장, work/asr/01.json·02.json 을 만든다."""
    event_dir = root / SLUG
    event_dir.mkdir(parents=True, exist_ok=True)
    (event_dir / "event.yaml").write_text(EVENT_YAML, encoding="utf-8")
    for i, when in enumerate(PHOTO_TIMES, 1):
        write_photo(event_dir / "raw" / "photos" / "01" / f"IMG_{i:04d}.jpg", when)
    asr_dir = event_dir / "work" / "asr"
    asr_dir.mkdir(parents=True, exist_ok=True)
    (asr_dir / "01.json").write_text(json.dumps(asr_json(ASR_01, 60.0), ensure_ascii=False), encoding="utf-8")
    (asr_dir / "02.json").write_text(json.dumps(asr_json(ASR_02, 30.0), ensure_ascii=False), encoding="utf-8")
    return event_dir


def build_scaffolded(root: Path):
    """init → 사진 ingest → segment → align → scaffold 까지 된 합성 행사(Event). 설정(연구 라인·템플릿)은 먼저 둔다."""
    assert main(["init", SLUG, "--root", str(root), "--date", "2026-10-20"]) == 0
    event_dir = build_event(root)
    ev = load_event(event_dir)
    photos.ingest(ev, ev.talks[0])
    assert main(["segment", str(event_dir)]) == 0
    assert main(["align", str(event_dir)]) == 0
    assert main(["scaffold", str(event_dir)]) == 0
    return ev
