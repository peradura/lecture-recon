import importlib.util
import json

import pytest
from PIL import Image

import synth
from lecture_recon import lint, photos
from lecture_recon.__main__ import main
from lecture_recon.config import load_event
from lecture_recon.scaffold import NO_PHOTOS_LINE

NOTE_01 = "01_42dot_이희석.md"
TR_01 = f"../{synth.SLUG}-transcripts/{NOTE_01}"


@pytest.fixture
def built(tmp_path):
    """init → (event.yaml·사진·가짜 asr json) → 사진 ingest → segment → align → scaffold."""
    assert main(["init", synth.SLUG, "--root", str(tmp_path), "--date", "2026-10-20"]) == 0
    event_dir = synth.build_event(tmp_path)
    ev = load_event(event_dir)
    photos.ingest(ev, ev.talks[0])
    assert main(["segment", str(event_dir)]) == 0
    assert main(["align", str(event_dir)]) == 0
    assert main(["scaffold", str(event_dir)]) == 0
    return ev


def lint_of(ev):
    return lint.run(ev)


def edit(path, old, new):
    text = path.read_text(encoding="utf-8")
    assert old in text
    path.write_text(text.replace(old, new, 1), encoding="utf-8")


def test_slugify_github_style():
    assert lint.slugify("1. 데이터 수집 — *Pipeline*") == "1-데이터-수집--pipeline"
    assert lint.slugify("[00:12:30] 장 제목") == "001230-장-제목"
    assert lint.slugify("Q&A: `snake_case` [링크](x.md)") == "qa-snake_case-링크"


def test_pipeline_outputs(built):
    ev = built
    tr = (ev.transcripts_dir / NOTE_01).read_text(encoding="utf-8")
    assert '<a id="segment-01-002"></a>\n**[01-002] [00:00:11]** 첫 번째 주제는 데이터 수집입니다. [불명확: "엔드투엔드 오토 라벨링"]' in tr
    note1 = ev.note_path(ev.talks[0]).read_text(encoding="utf-8")
    assert "## 슬라이드" in note1
    assert f"| 02 | IMG_0002 | 00:00:13 | <제목> | <핵심 내용> | [01-002]({TR_01}#segment-01-002) |" in note1
    assert f"(문단 3개) · 사진 3장" in note1
    note2 = ev.note_path(ev.talks[1]).read_text(encoding="utf-8")
    assert NO_PHOTOS_LINE in note2 and "## 슬라이드" not in note2
    for path in (ev.notes_dir / "README.md", ev.transcripts_dir / "README.md", ev.report_path):
        assert path.exists()
    with Image.open(ev.slides_dir(ev.talks[0]) / "IMG_0001.jpg") as im:
        assert im.size == (1600, 1200)


def test_fresh_scaffold_lints_with_warnings_only(built):
    result = lint_of(built)
    assert result.errors == []
    joined = "\n".join(result.warnings)
    assert "[규칙6]" in joined and "장 제목" in joined
    unresolved = (built.transcripts_dir / "unresolved-segments.md").read_text(encoding="utf-8")
    assert "| [01-002](01_42dot_이희석.md#segment-01-002) | 00:00:11 | 엔드투엔드 오토 라벨링 |  |" in unresolved
    assert main(["lint", str(built.root)]) == 0


def test_bad_segment_link_fails(built):
    edit(built.note_path(built.talks[0]), "## 요약\n", f"## 요약\n\n주장 ([01-099]({TR_01}#segment-01-099)).\n")
    result = lint_of(built)
    assert any("[규칙1]" in e and "segment-01-099" in e for e in result.errors)
    assert main(["lint", str(built.root)]) == 1


def test_heading_anchor_and_missing_file(built):
    report = built.report_path
    edit(report, "## 한눈에 보기\n", "## 한눈에 보기\n\n"
         f"- [이희석]({synth.SLUG}-notes/{NOTE_01}#01-이희석-42dot--from-data-to-driving)\n"
         f"- [요약]({synth.SLUG}-notes/{NOTE_01}#요약)\n")
    assert lint_of(built).errors == []
    edit(report, "#요약)", "#없는-절)")
    edit(report, "## 한눈에 보기\n", f"## 한눈에 보기\n\n- [없음]({synth.SLUG}-notes/99_x.md)\n")
    errors = lint_of(built).errors
    assert any("[규칙2] 앵커 없음" in e for e in errors)
    assert any("[규칙2] 링크 파일 없음" in e for e in errors)


def test_slide_range_and_required_sections(built):
    note = built.note_path(built.talks[0])
    edit(note, "## 요약\n", "## 요약\n\n데이터를 다룬다 (슬라이드 02). 사진 3장(슬라이드 3장).\n")
    assert lint_of(built).errors == []
    edit(note, "(슬라이드 02)", "(슬라이드 07)")
    edit(note, "## 확인이 필요한 것", "## 확인할 것")
    edit(note, "## 슬라이드\n", "## 슬라이드 목록\n")
    errors = "\n".join(lint_of(built).errors)
    assert "[규칙3] `슬라이드 07`" in errors
    assert "[규칙5] 필수 절 `## 확인이 필요한 것`" in errors
    assert "[규칙5] 사진이 있는 발표는 `## 슬라이드`" in errors


def test_unresolved_keeps_handwritten_note(built):
    lint_of(built)
    path = built.transcripts_dir / "unresolved-segments.md"
    edit(path, "엔드투엔드 오토 라벨링 |  |", "엔드투엔드 오토 라벨링 | 슬라이드 확인 필요 |")
    tr = built.transcripts_dir / NOTE_01
    edit(tr, "두 번째는 모델 학습입니다.", "두 번째는 [불명확] 모델 학습입니다.")
    result = lint_of(built)
    text = path.read_text(encoding="utf-8")
    assert "| 엔드투엔드 오토 라벨링 | 슬라이드 확인 필요 |" in text
    assert "| [01-003](01_42dot_이희석.md#segment-01-003) | 00:00:22 |  |  |" in text
    assert any("2행으로 갱신 (이전 1행)" in i for i in result.infos)


def test_segment_and_scaffold_protect_existing_files(built):
    tr = built.transcripts_dir / NOTE_01
    tr.write_text(tr.read_text(encoding="utf-8") + "\n교정 메모\n", encoding="utf-8")
    assert main(["segment", str(built.root), "--talk", "01"]) == 1
    assert "교정 메모" in tr.read_text(encoding="utf-8")
    assert main(["segment", str(built.root), "--talk", "01", "--force"]) == 0
    assert "교정 메모" not in tr.read_text(encoding="utf-8")

    note = built.note_path(built.talks[0])
    note.write_text("손으로 쓴 노트\n", encoding="utf-8")
    assert main(["scaffold", str(built.root)]) == 0
    assert note.read_text(encoding="utf-8") == "손으로 쓴 노트\n"
    assert main(["scaffold", str(built.root), "--force"]) == 0
    assert "## 요약" in note.read_text(encoding="utf-8")


def test_status(built, capsys):
    assert main(["status", str(built.root)]) == 0
    out = capsys.readouterr().out
    assert "| 01 | 이희석, 42dot | 0/1 | 1/1 | O (3문단) | - | 3 | O | O |" in out
    assert "| 02 | 김 철수, 서울대 | 0/1 | 1/1 | O (1문단) | - | - | n/a | O |" in out
    assert "02: started_at 없음" in out and "report: O" in out


@pytest.mark.skipif(importlib.util.find_spec("faster_whisper") is not None, reason="faster-whisper 설치됨")
def test_asr_without_faster_whisper_gives_install_hint(built, capsys):
    assert main(["asr", str(built.root)]) == 2
    assert 'uv pip install -e ".[asr]"' in capsys.readouterr().err


def test_asr_json_schema_with_fake_model(built, monkeypatch):
    from types import SimpleNamespace

    from lecture_recon import asr

    class FakeModel:
        def transcribe(self, path, language, beam_size):
            seg = SimpleNamespace(start=0.0, end=2.5, text=" 안녕하세요", avg_logprob=-0.31234, no_speech_prob=0.01)
            return iter([seg]), SimpleNamespace(language=language or "en", duration=3.0)

    monkeypatch.setattr(asr, "load_model", lambda *a: FakeModel())
    talk = built.talks[0]
    wav = built.audio_path(talk)
    wav.parent.mkdir(parents=True, exist_ok=True)
    wav.write_bytes(b"")
    assert main(["asr", str(built.root), "--talk", "1"]) == 0
    data = json.loads(built.asr_path(talk).read_text(encoding="utf-8"))
    assert data["language"] == "ko" and data["duration"] == 3.0 and data["model"] == "large-v3"
    assert data["segments"] == [{"start": 0.0, "end": 2.5, "text": "안녕하세요", "avg_logprob": -0.3123, "no_speech_prob": 0.01}]
