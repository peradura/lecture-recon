"""사진 촬영 시각 ↔ 전사 문단 시각 정렬 → work/align/NN.json."""

from __future__ import annotations

import bisect
import datetime as dt
import json
from zoneinfo import ZoneInfo

from . import audio, photos, transcript
from .config import Event, LectureReconError, Talk


def recording_start(event: Event, talk: Talk) -> tuple[dt.datetime, bool]:
    """(주 녹음 시작 시각, 추정 여부). started_at 이 없으면 원본 파일 mtime - 길이로 추정한다."""
    rec = talk.recordings[0]
    if rec.started_at:
        return rec.started_at, False
    src = event.root / rec.file
    if not src.exists():
        raise LectureReconError(f"{talk.nn}: started_at 이 없고 녹음 파일도 없어 시작 시각을 추정할 수 없다: {rec.file}")
    asr_path = event.asr_path(talk)
    length = None
    if asr_path.exists():
        length = json.loads(asr_path.read_text(encoding="utf-8")).get("duration")
    if not length:
        length = audio.duration(src)
    end = dt.datetime.fromtimestamp(src.stat().st_mtime, ZoneInfo(event.timezone)).replace(tzinfo=None)
    return end - dt.timedelta(seconds=float(length)), True


def align_photos(shots: list[dict], start: dt.datetime, offset_sec: float,
                 paragraphs: list[transcript.Paragraph], duration: float | None = None) -> list[dict]:
    """shots: [{"photo": str, "taken_at": datetime}]. 녹음 시작 기준 위치에 해당하는 문단을 붙인다.

    offset_in_talk = (taken_at - start) - offset_sec  (offset_sec 양수 = 사진이 늦게 찍힌 것으로 취급).
    녹음 범위(0 ~ duration) 밖이면 paragraph_id 는 None. slide_hint 는 촬영 순서(1부터).
    """
    paras = sorted(paragraphs, key=lambda p: p.start)
    starts = [p.start for p in paras]
    out = []
    for i, shot in enumerate(sorted(shots, key=lambda s: (s["taken_at"], s["photo"])), 1):
        offset = (shot["taken_at"] - start).total_seconds() - offset_sec
        pid = None
        if paras and offset >= 0 and (duration is None or offset <= duration):
            pid = paras[max(bisect.bisect_right(starts, offset) - 1, 0)].id
        out.append({
            "photo": shot["photo"],
            "taken_at": shot["taken_at"].isoformat(),
            "offset_in_talk_sec": round(offset, 1),
            "paragraph_id": pid,
            "slide_hint": i,
        })
    return out


def run(event: Event, talk: Talk) -> tuple[list[dict], bool]:
    """(정렬 결과, 시작 시각 추정 여부). work/align/NN.json 에 쓴다."""
    manifest = photos.load_manifest(event, talk)
    if manifest is None:
        raise LectureReconError(f"{talk.nn}: 사진 manifest 없음 — 먼저 `lecture-recon ingest`")
    tpath = event.transcript_path(talk)
    if not tpath.exists():
        raise LectureReconError(f"{talk.nn}: 전사 없음 ({event.rel(tpath)}) — 먼저 `lecture-recon segment`")
    parsed = transcript.parse(tpath.read_text(encoding="utf-8"))
    start, estimated = recording_start(event, talk)
    shots = [{"photo": p["slide_copy"], "taken_at": dt.datetime.fromisoformat(p["taken_at"])}
             for p in manifest["photos"]]
    result = align_photos(shots, start, talk.recordings[0].offset_sec, parsed.paragraphs, parsed.duration)
    out = event.align_path(talk)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(result, ensure_ascii=False, indent=1), encoding="utf-8")
    return result, estimated


def load(event: Event, talk: Talk) -> list[dict] | None:
    path = event.align_path(talk)
    if not path.exists():
        return None
    return json.loads(path.read_text(encoding="utf-8"))
