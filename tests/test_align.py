import datetime as dt
import json
import os

from lecture_recon import align
from lecture_recon.transcript import Paragraph

START = dt.datetime(2026, 10, 20, 10, 0, 0)
PARAS = [Paragraph("01-001", 0, "a"), Paragraph("01-002", 30, "b"), Paragraph("01-003", 90, "c")]


def shot(name, sec):
    return {"photo": name, "taken_at": START + dt.timedelta(seconds=sec)}


def test_align_maps_photo_to_containing_paragraph():
    out = align.align_photos([shot("c.jpg", 95), shot("a.jpg", 5), shot("b.jpg", 30)], START, 0, PARAS, duration=120)
    assert [(a["photo"], a["paragraph_id"], a["slide_hint"]) for a in out] == [
        ("a.jpg", "01-001", 1), ("b.jpg", "01-002", 2), ("c.jpg", "01-003", 3)]
    assert out[0]["offset_in_talk_sec"] == 5.0
    assert out[0]["taken_at"] == "2026-10-20T10:00:05"


def test_offset_sec_positive_means_photo_taken_late():
    # 사진 시계가 40초 늦다 → 100초에 찍힌 사진은 녹음 60초 지점(01-002)
    out = align.align_photos([shot("x.jpg", 100)], START, 40, PARAS, duration=120)
    assert out[0]["offset_in_talk_sec"] == 60.0
    assert out[0]["paragraph_id"] == "01-002"
    out = align.align_photos([shot("x.jpg", 100)], START, 0, PARAS)
    assert out[0]["paragraph_id"] == "01-003"


def test_photos_outside_recording_have_no_paragraph():
    out = align.align_photos([shot("early.jpg", -10), shot("late.jpg", 200)], START, 0, PARAS, duration=120)
    assert [a["paragraph_id"] for a in out] == [None, None]
    assert out[0]["offset_in_talk_sec"] == -10.0


def test_recording_start_estimated_from_mtime_minus_duration(event):
    talk = event.talks[1]  # started_at 없음, work/asr/02.json duration = 30
    raw = event.root / talk.recordings[0].file
    raw.parent.mkdir(parents=True, exist_ok=True)
    raw.write_bytes(b"")
    end = dt.datetime(2026, 10, 20, 11, 0, 30, tzinfo=dt.timezone(dt.timedelta(hours=9)))
    os.utime(raw, (end.timestamp(), end.timestamp()))
    start, estimated = align.recording_start(event, talk)
    assert estimated and start == dt.datetime(2026, 10, 20, 11, 0, 0)
    start, estimated = align.recording_start(event, event.talks[0])
    assert not estimated and start == dt.datetime(2026, 10, 20, 10, 5, 0)


def test_run_reads_paragraphs_back_from_transcript(event, event_dir):
    from lecture_recon import photos
    from lecture_recon.__main__ import main

    talk = event.talks[0]
    photos.ingest(event, talk)
    assert main(["segment", str(event_dir), "--talk", "1"]) == 0
    # 교정 후를 흉내: 장 제목을 끼워도 정렬은 문단 시각을 그대로 읽는다
    tpath = event.transcript_path(talk)
    tpath.write_text(tpath.read_text(encoding="utf-8").replace(
        '<a id="segment-01-002">', '## [00:00:11] 데이터 수집\n\n<a id="segment-01-002">'), encoding="utf-8")
    assert main(["align", str(event_dir)]) == 0
    result = json.loads(event.align_path(talk).read_text(encoding="utf-8"))
    assert [a["paragraph_id"] for a in result] == ["01-001", "01-002", "01-003"]
    assert result[0]["photo"] == "synth-day-2026-notes/slides/01/IMG_0001.jpg"
    assert not event.align_path(event.talks[1]).exists()
