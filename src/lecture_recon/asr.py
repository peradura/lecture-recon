"""faster-whisper 전사 → work/asr/NN[_k].json. faster-whisper 는 선택 의존성(asr extra)."""

from __future__ import annotations

import datetime as dt
import json
import sys
from pathlib import Path

from .config import Event, LectureReconError, Talk

INSTALL_HINT = (
    "faster-whisper 가 설치돼 있지 않다. 레포 루트에서 "
    '`uv pip install -e ".[asr]"` 로 설치한 뒤 다시 실행한다.'
)


def load_model(model: str, device: str, compute_type: str):
    try:
        from faster_whisper import WhisperModel
    except ImportError as e:
        raise LectureReconError(INSTALL_HINT) from e
    return WhisperModel(model, device=device, compute_type=compute_type)


def transcribe(model, wav: Path, language: str | None) -> dict:
    segments, info = model.transcribe(str(wav), language=language, beam_size=5)
    out = []
    for s in segments:
        out.append({
            "start": round(s.start, 2),
            "end": round(s.end, 2),
            "text": s.text.strip(),
            "avg_logprob": round(s.avg_logprob, 4),
            "no_speech_prob": round(s.no_speech_prob, 4),
        })
        if len(out) % 50 == 0:
            print(f"    … {len(out)} 세그먼트, {s.end / 60:.1f}분", file=sys.stderr, flush=True)
    return {"language": info.language, "duration": round(info.duration, 2), "segments": out}


def run(event: Event, talks: list[Talk], model_name: str, device: str,
        compute_type: str, language: str | None) -> bool:
    """talks 의 모든 녹음을 전사한다. 하나라도 실패하면 False."""
    model = load_model(model_name, device, compute_type)
    ok = True
    for talk in talks:
        lang = talk.asr_language if language is None else (None if language in ("mixed", "auto") else language)
        for k, rec in enumerate(talk.recordings):
            wav = event.audio_path(talk, k)
            if not wav.exists():
                print(f"{talk.nn}: {event.rel(wav)} 없음 — 먼저 `lecture-recon ingest`", file=sys.stderr)
                ok = False
                continue
            print(f"{talk.nn}: {event.rel(wav)} 전사 중 (model={model_name}, device={device}, language={lang or 'auto'})",
                  flush=True)
            result = transcribe(model, wav, lang)
            data = {
                "audio": event.rel(wav),
                "source": rec.file,
                "model": model_name,
                "device": device,
                "compute_type": compute_type,
                "created": dt.date.today().isoformat(),
                **result,
            }
            out = event.asr_path(talk, k)
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
            print(f"{talk.nn}: 세그먼트 {len(result['segments'])}개 → {event.rel(out)}")
    return ok
