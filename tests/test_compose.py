import json
import re

import pytest

import synth
from fakes import FakeBackend, FakeWeb, by_prompt, fake_searcher, install, user_of
from lecture_recon import compose, lint, llm, scaffold, verify
from lecture_recon.__main__ import main
from lecture_recon.config import LectureReconError, Talk
from test_verify import PAGES, RESULTS, judge, write_claims

CONTENT = """## 1. 인사와 주제
- 발표자는 42dot 이희석이라고 소개하고 데이터에서 주행까지를 다룬다고 했다([01-001]).
- 근거 없는 문장이다.
- 없는 문단을 인용한다([01-099]).
- 발표 흐름은 <placeholder> 없이 이어진다([01-001], [01-099]).

## 2. 데이터와 학습
- 첫 주제는 데이터 수집, 둘째는 모델 학습이다([01-002], [01-003]) (슬라이드 02).
- 범위 밖 슬라이드를 언급한다([01-003]) (슬라이드 09).
"""
FINAL = """## 요약

데이터 수집에서 모델 학습까지의 흐름을 소개한 발표다.

## 연구 라인에 걸리는 것

### 로봇 정책 학습
- 오토 라벨링: 자동 라벨링 언급([01-002]) → 정책 학습용 데이터 라벨링의 비교점이다.
- 근거 없는 연결.

### 설정에 없는 라인
- 무엇([01-001]).

### LLM 평가 벤치마크
- 근거 없는 연결만 있다.

## 확인이 필요한 것

- 42dot 소속 표기([01-001]).
"""
REPORT = """목적: 이 리포트는 합성 행사의 데이터 흐름을 정리한다.

## 관통 주제

### 1. 데이터 중심 주행
도입: 발표 하나가 이 흐름이다.
- [[01#1. 인사와 주제]]: 데이터에서 주행까지([01-001]).
- 다르게 본 점: 없다.
- 다루지 않은 발표: 아무거나

### 2. 학습
- [[01#없는 절]]: 모델 학습([01-003], [01-555]).

## 연구에 옮길 아이디어

### 로봇 정책 학습
- 오토 라벨링([[01]]): 근거([01-002]) → 적용.

### LLM 평가 벤치마크
- 노트에 없는 라인([[01]]).

## 확인이 필요한 것

- [[01#확인이 필요한 것]]: 소속 표기([01-001]).
- 공통: 공식 프로그램을 확인하지 않았다.
"""
LINES_YAML = """lines:
  - id: robot-policy
    name: 로봇 정책 학습
    keywords: [robot, policy, labeling]
  - id: llm-eval
    name: LLM 평가 벤치마크
    keywords: [benchmark, evaluation]
"""


def write_lines(text: str = LINES_YAML):
    """설정 디렉터리(conftest 가 임시 디렉터리로 돌린다)의 기본 연구 라인 설정."""
    path = llm.config_dir() / llm.RESEARCH_LINES_FILE
    path.write_text(text, encoding="utf-8")
    return path


@pytest.fixture
def built_with_lines(tmp_path):
    """연구 라인 설정을 먼저 두고 scaffold 까지 만든 합성 행사(골격에도 연구 라인 절이 있다)."""
    write_lines()
    return synth.build_scaffolded(tmp_path)


def note_responder(messages):
    user = messages[1]["content"]
    return FINAL if user.startswith("[작업] 마무리") else CONTENT


def fill_slides(ev, capsys):
    assert main(["slides", str(ev.root), "1", "--backend", "manual"]) == 3
    prompt = (ev.work_dir / "prompts" / "slides-01-1.md").read_text(encoding="utf-8")
    assert "IMG_0001" in prompt and "첨부" in prompt
    rid = prompt.split("`request_id: ")[1].split("`")[0]
    rows = [{"photo": f"IMG_000{i}", "title": f"제목 {i}", "content": f"내용 {i} | 표"} for i in (1, 2, 3)]
    resp = ev.work_dir / "responses" / "slides-01-1.md"
    resp.write_text(f"request_id: {rid}\n" + json.dumps({"slides": rows[:2]}, ensure_ascii=False), encoding="utf-8")
    assert main(["slides", str(ev.root), "1", "--backend", "manual"]) == 3      # 사진 하나 빠짐 → 다시
    assert "빠짐 IMG_0003" in capsys.readouterr().err
    resp.write_text(f"request_id: {rid}\n" + json.dumps(rows, ensure_ascii=False), encoding="utf-8")
    assert main(["slides", str(ev.root), "1", "--backend", "manual"]) in (0, 1)
    note = ev.note_path(ev.talks[0]).read_text(encoding="utf-8")
    assert "| 02 | IMG_0002 | 00:00:13 | 제목 2 | 내용 2 \\| 표 |" in note
    assert "사진 3장(슬라이드 3장)" in note


def test_slides_api_backend_is_not_implemented(built, monkeypatch, capsys):
    install(monkeypatch, FakeBackend(lambda m: "{}"))
    assert main(["slides", str(built.root), "1"]) == 0
    assert "구현하지 않았다" in capsys.readouterr().out


def test_notes_are_lint_clean_and_take_verdicts_from_verify(built_with_lines, monkeypatch, capsys):
    ev, talk = built_with_lines, built_with_lines.talks[0]
    fill_slides(ev, capsys)
    write_claims(ev)
    verify.run_verify(ev, talk, llm.LLM(FakeBackend(judge), ev.work_dir),
                      fake_searcher(ev.work_dir, FakeWeb(PAGES), RESULTS), progress=lambda m: None)
    backend = FakeBackend(by_prompt(notes=note_responder))
    install(monkeypatch, backend)
    assert main(["notes", str(ev.root), "1"]) == 0
    out = capsys.readouterr().out
    assert "오류 0" in out and "근거 ID 없는 줄 2개" in out and "01-099" in out
    note = ev.note_path(talk).read_text(encoding="utf-8")
    result = lint.run(ev)
    assert result.errors == []
    assert not [w for w in result.warnings if "01_42dot" in w and "notes" in w]
    tr = "../synth-day-2026-transcripts/01_42dot_이희석.md"
    assert f"소개하고 데이터에서 주행까지를 다룬다고 했다([01-001]({tr}#segment-01-001))." in note
    assert "근거 없는 문장이다" not in note and "segment-01-099" not in note
    assert "〈placeholder〉" in note
    assert "(슬라이드 02)" in note and "슬라이드 09" not in note and "언급한다([01-003]" in note
    # 원문 대조 결과는 verify json 그대로(4분류, 판정을 지어내지 않음)
    section = note.split("## 원문 대조 결과", 1)[1].split("## 연구 라인에 걸리는 것", 1)[0]
    assert section.count("### ") == 4
    confirmed = section.split("### 확인", 1)[1].split("### 발언과 원문이 다른 것", 1)[0]
    assert "97.8" in confirmed and "https://a.example/rldx" in confirmed and "40ms" not in confirmed
    unverified = section.split("### 확인하지 않은 것", 1)[1]
    assert "인용문 불일치" in unverified and "81.5" in unverified
    # 연구 라인: 설정에 있는 이름만, 근거 있는 줄만
    research = note.split("## 연구 라인에 걸리는 것", 1)[1].split("## 확인이 필요한 것", 1)[0]
    assert "### 로봇 정책 학습" in research
    assert "설정에 없는 라인" not in research and "LLM 평가" not in research and "근거 없는 연결" not in research
    final_user = next(user_of(m) for m in backend.calls if user_of(m).startswith("[작업] 마무리"))
    assert final_user.startswith("[작업] 마무리 (## 요약, ## 연구 라인에 걸리는 것, ## 확인이 필요한 것)")
    assert "- 로봇 정책 학습: 키워드 robot, policy, labeling" in final_user
    check = note.split("## 확인이 필요한 것", 1)[1]
    assert "42dot 소속 표기" in check and "아무 데도 없는 주장" in check and "검색했으나 찾지 못한 것" in check
    assert "> 대조일: " in note and "사진 3장(슬라이드 3장)" in note
    # 두 번째 실행은 캐시로(LLM 호출 없음), 손으로 고친 노트는 --force 없이 덮어쓰지 않는다
    n_calls = len(backend.calls)
    assert main(["notes", str(ev.root), "1"]) == 0 and len(backend.calls) == n_calls
    ev.note_path(talk).write_text(note + "\n손으로 쓴 메모([01-001]).\n", encoding="utf-8")
    assert main(["notes", str(ev.root), "1"]) == 2
    assert main(["notes", str(ev.root), "1", "--force"]) == 0
    assert list((ev.work_dir / "backup").glob("01_42dot_이희석.*.md"))


def test_notes_without_verify_say_so(built, monkeypatch, capsys):
    install(monkeypatch, FakeBackend(by_prompt(notes=note_responder)))
    assert main(["notes", str(built.root), "1"]) == 0
    note = built.note_path(built.talks[0]).read_text(encoding="utf-8")
    assert "원문 대조를 하지 않았다" in note and "> 대조일: 없음(원문 대조 전)" in note
    assert lint.run(built).errors == []


def test_report_is_lint_clean_and_recomputes_uncovered(built_with_lines, monkeypatch, capsys):
    built = built_with_lines
    backend = FakeBackend(by_prompt(notes=note_responder, report=REPORT))
    install(monkeypatch, backend)
    assert main(["notes", str(built.root), "1"]) == 0
    assert main(["report", str(built.root)]) == 0
    out = capsys.readouterr().out
    assert "관통 주제 2개" in out and "노트에 없는 연구 라인이라 뺐다: LLM 평가 벤치마크" in out
    assert "[작업] 목적 줄 + 세 절 전부" in user_of(backend.calls[-1])
    report = built.report_path.read_text(encoding="utf-8")
    assert report.startswith("# 합성 테크 데이 종합 리포트\n")
    assert "[이희석](synth-day-2026-notes/01_42dot_이희석.md#1-인사와-주제)" in report
    assert "[이희석](synth-day-2026-notes/01_42dot_이희석.md#확인이-필요한-것)" in report
    assert "[이희석](synth-day-2026-notes/01_42dot_이희석.md)" in report        # 없는 절 → 노트 링크
    assert "아무거나" not in report and report.count("- 다루지 않은 발표: 없음") == 2
    assert "(녹음·노트가 없는 발표 1건은 대상 밖)" in report
    assert "segment-01-555" not in report and "### LLM 평가 벤치마크" not in report
    ideas = report.split("## 연구에 옮길 아이디어", 1)[1].split("## 확인이 필요한 것", 1)[0]
    assert "### 로봇 정책 학습" in ideas and "segment-01-002" in ideas
    assert "- 이 리포트는 합성 행사의 데이터 흐름을 정리한다." in report
    assert "원문 대조: 하지 않았다" in report and "02 김 철수" in report
    result = lint.run(built)
    assert result.errors == []
    assert not [w for w in result.warnings if "report" in w]


def test_notes_and_report_without_research_lines_have_no_research_sections(built, monkeypatch, capsys):
    """연구 라인 설정이 없으면: 골격·노트·리포트에 연구 라인 절이 없고, 응답에 와도 버리며, lint 를 통과한다."""
    skeleton = built.note_path(built.talks[0]).read_text(encoding="utf-8")
    assert "## 연구 라인에 걸리는 것" not in skeleton and "<!-- if" not in skeleton and "<!-- endif" not in skeleton
    assert "## 연구에 옮길 아이디어" not in built.report_path.read_text(encoding="utf-8")
    backend = FakeBackend(by_prompt(notes=note_responder, report=REPORT))
    install(monkeypatch, backend)
    res = compose.run_notes(built, built.talks[0], llm.LLM(backend, built.work_dir))
    assert res["research_lines"] == 0
    assert res["research_dropped"] == ["설정이 없어 응답의 `## 연구 라인에 걸리는 것` 절을 버렸다"]
    note = built.note_path(built.talks[0]).read_text(encoding="utf-8")
    assert "## 연구 라인에 걸리는 것" not in note and "로봇 정책 학습" not in note
    assert "## 원문 대조 결과" in note and "## 확인이 필요한 것" in note and scaffold.SKELETON_MARK not in note
    final_user = user_of(backend.calls[-1])
    assert final_user.startswith("[작업] 마무리 (## 요약, ## 확인이 필요한 것)")
    assert "[연구 라인]\n없음 — `## 연구 라인에 걸리는 것` 절을 쓰지 않는다." in final_user
    assert lint.run(built).errors == []
    # CLI 출력, 리포트: 노트에 연구 라인 절이 없으면 아이디어 절도 없고, 프롬프트에 쓰지 말라고 알린다
    assert main(["notes", str(built.root), "1", "--force"]) == 0
    assert "연구 라인: 설정 없음(절 생략)" in capsys.readouterr().out
    assert main(["report", str(built.root)]) == 0
    out = capsys.readouterr().out
    assert "`## 연구에 옮길 아이디어` 생략" in out
    report_user = user_of(backend.calls[-1])
    assert "`## 연구에 옮길 아이디어` 는 쓰지 않는다" in report_user and "세 절 전부" not in report_user
    report = built.report_path.read_text(encoding="utf-8")
    assert "## 연구에 옮길 아이디어" not in report and "### 로봇 정책 학습" not in report
    assert "## 관통 주제" in report and "## 확인이 필요한 것" in report
    result = lint.run(built)
    assert result.errors == [] and not [w for w in result.warnings if "report" in w]


def test_report_split_without_research_lines_asks_only_for_check(built, monkeypatch):
    """노트가 길어 둘로 나눠 부를 때도 연구 라인 절이 없으면 둘째 요청은 확인 절만."""
    install(monkeypatch, FakeBackend(by_prompt(notes=note_responder)))
    assert main(["notes", str(built.root), "1"]) == 0
    monkeypatch.setattr(compose, "REPORT_SPLIT_CHARS", 10)
    backend = FakeBackend(by_prompt(report=REPORT))
    compose.run_report(built, llm.LLM(backend, built.work_dir))
    u2 = user_of(backend.calls[-1])
    assert "[작업] `## 확인이 필요한 것` 만" in u2 and "`## 연구에 옮길 아이디어` 는 쓰지 않는다" in u2
    assert "## 연구에 옮길 아이디어" not in built.report_path.read_text(encoding="utf-8")


def test_report_split_with_research_lines_asks_for_ideas(built_with_lines, monkeypatch):
    """둘로 나눠 부를 때 노트에 연구 라인 절이 있으면 둘째 요청이 아이디어·확인 두 절을 받고 리포트에 아이디어 절이 있다."""
    built = built_with_lines
    install(monkeypatch, FakeBackend(by_prompt(notes=note_responder)))
    assert main(["notes", str(built.root), "1"]) == 0
    monkeypatch.setattr(compose, "REPORT_SPLIT_CHARS", 10)
    backend = FakeBackend(by_prompt(report=REPORT))
    res = compose.run_report(built, llm.LLM(backend, built.work_dir))
    u2 = user_of(backend.calls[-1])
    assert "[작업] `## 연구에 옮길 아이디어` 와 `## 확인이 필요한 것` 만" in u2 and "쓰지 않는다" not in u2
    assert res["ideas_section"] and res["ideas_dropped"] == ["LLM 평가 벤치마크"]
    ideas = built.report_path.read_text(encoding="utf-8").split("## 연구에 옮길 아이디어", 1)[1]
    assert "### 로봇 정책 학습" in ideas.split("## 확인이 필요한 것", 1)[0]
    assert lint.run(built).errors == []


def test_scaffold_keeps_research_sections_when_lines_are_set(built_with_lines):
    built = built_with_lines
    note = built.note_path(built.talks[0]).read_text(encoding="utf-8")
    assert note.startswith(scaffold.SKELETON_MARK + "\n# 01. ")
    assert "## 연구 라인에 걸리는 것\n\n<연구 라인 설정(research_lines.yaml)의" in note
    assert "<!-- if" not in note and "<!-- endif" not in note
    report = built.report_path.read_text(encoding="utf-8")
    assert "## 연구에 옮길 아이디어\n\n### <연구 라인 이름>" in report
    assert lint.run(built).errors == []


def test_scaffold_follows_explicit_research_lines_setting(tmp_path, monkeypatch):
    """scaffold 는 config `research_lines:` 를 따른다: 상대 경로는 설정 디렉터리 기준, false 면 기본 파일이 있어도 끈다."""
    (llm.config_dir() / "my-lines.yaml").write_text(LINES_YAML, encoding="utf-8")
    llm.config_path().write_text("research_lines: my-lines.yaml\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)                                  # CWD 기준이 아님을 본다
    ev = synth.build_scaffolded(tmp_path / "a")
    assert "## 연구 라인에 걸리는 것" in ev.note_path(ev.talks[0]).read_text(encoding="utf-8")
    write_lines()
    llm.config_path().write_text("research_lines: false\n", encoding="utf-8")
    ev = synth.build_scaffolded(tmp_path / "b")
    assert "## 연구 라인에 걸리는 것" not in ev.note_path(ev.talks[0]).read_text(encoding="utf-8")
    assert "## 연구에 옮길 아이디어" not in ev.report_path.read_text(encoding="utf-8")


def test_research_lines_resolution(tmp_path, monkeypatch):
    """config `research_lines:`(`~` 확장, 상대 경로는 설정 디렉터리 기준, false 면 끔, 빈 문자열은 기본 탐색)
    → 설정 디렉터리의 research_lines.yaml → 없으면 []."""
    assert llm.research_lines() == [] and llm.research_lines_path() is None
    default = write_lines("lines:\n  - {id: a, name: 기본 라인}\n")
    assert [x["name"] for x in llm.research_lines()] == ["기본 라인"]
    assert compose.research_lines is llm.research_lines                  # 호환용 alias
    monkeypatch.setenv("HOME", str(tmp_path))
    (tmp_path / "mine.yaml").write_text("lines:\n  - {id: b, name: 내 라인}\n  - {id: c}\n", encoding="utf-8")
    llm.config_path().write_text("research_lines: ~/mine.yaml\n", encoding="utf-8")
    assert [x["name"] for x in llm.research_lines()] == ["내 라인"]
    (llm.config_dir() / "sub").mkdir()
    (llm.config_dir() / "sub" / "rel.yaml").write_text("lines:\n  - {id: r, name: 상대 라인}\n", encoding="utf-8")
    llm.config_path().write_text("research_lines: sub/rel.yaml\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    assert [x["name"] for x in llm.research_lines()] == ["상대 라인"]
    llm.config_path().write_text("research_lines: false\n", encoding="utf-8")
    assert default.is_file() and llm.research_lines() == [] and llm.research_lines_path() is None
    llm.config_path().write_text('research_lines: ""\n', encoding="utf-8")
    assert [x["name"] for x in llm.research_lines()] == ["기본 라인"]
    llm.config_path().write_text("research_lines: missing.yaml\n", encoding="utf-8")
    absolute = re.escape(str(llm.config_dir().absolute() / "missing.yaml"))
    with pytest.raises(LectureReconError, match=f"research_lines 파일이 없다: {absolute} "):
        llm.research_lines()


@pytest.mark.parametrize("text, match", [
    ("- {id: a, name: 목록}\n", "최상위가 매핑"),
    ("lines: [\n", "YAML 파싱 실패"),
    ("lines: 하나\n", "`lines:` 는 목록"),
])
def test_broken_research_lines_file_is_an_error(text, match):
    write_lines(text)
    with pytest.raises(LectureReconError, match=match):
        llm.research_lines()


def test_research_lines_file_without_valid_lines_warns(built, capsys):
    """파일은 있는데 name 있는 라인이 0개면 절을 두지 않고 경고한다."""
    write_lines("lines:\n  - {id: a}\n")
    assert main(["scaffold", str(built.root), "--force"]) == 0
    assert "파일은 있으나 라인 0개" in capsys.readouterr().err
    assert "## 연구 라인에 걸리는 것" not in built.note_path(built.talks[0]).read_text(encoding="utf-8")
    assert main(["notes", str(built.root), "1", "--plan", "--backend", "manual"]) == 0
    assert "파일은 있으나 라인 0개" in capsys.readouterr().out


def test_lint_requires_research_section_only_when_lines_are_set(built):
    """기본 필수 절: 연구 라인 설정이 있으면 `연구 라인에 걸리는 것` 도. config `required_sections:` 를 적으면 그대로."""
    missing = "[규칙5] 필수 절 `## 연구 라인에 걸리는 것` 없음"
    assert lint.required_sections() == lint.REQUIRED_SECTIONS
    assert missing not in "\n".join(lint.run(built).errors)
    write_lines()
    assert lint.required_sections() == ("요약", "원문 대조 결과", "연구 라인에 걸리는 것", "확인이 필요한 것")
    assert missing in "\n".join(lint.run(built).errors)            # 연구 라인 전에 만든 골격
    llm.config_path().write_text("required_sections: [요약, 원문 대조 결과, 확인이 필요한 것]\n", encoding="utf-8")
    assert missing not in "\n".join(lint.run(built).errors)


def test_report_refuses_without_notes(built, monkeypatch):
    install(monkeypatch, FakeBackend(lambda m: REPORT))
    assert main(["report", str(built.root)]) == 2


# 인용 ID 정규화 (2026-11-06 flash 실측 2차: `[04-02]` 를 백틱으로 감싸 모든 줄이 버려졌다) -------------------

def cites(*pids):
    c = compose.Cites(None, "../e-transcripts")
    c.known = {p: Talk(4, "신진우", "KAIST", "제목", "ko", []) for p in pids}
    return c


def seg(pid):
    return f"[{pid}](../e-transcripts/04_KAIST_신진우.md#segment-{pid})"


def test_backticked_short_citation_becomes_link():
    c = cites("04-002")
    assert c.convert("- 발표자는 인공지능 연구를 시작했다 `[04-02]`.") == \
        (f"- 발표자는 인공지능 연구를 시작했다 {seg('04-002')}.", 1)
    assert c.convert("- 발표 번호 한 자리도 맞춘다([4-02]).") == (f"- 발표 번호 한 자리도 맞춘다({seg('04-002')}).", 1)
    assert c.convert("- 둘 다 한 자리는 인용이 아니다([4-2]).") == ("- 둘 다 한 자리는 인용이 아니다([4-2]).", 0)
    assert c.convert("- 그대로 있는 ID([04-002]).") == (f"- 그대로 있는 ID({seg('04-002')}).", 1)
    assert c.invalid == []


def test_backticked_citation_lists_are_all_linked():
    c = cites("04-047", "04-048")
    assert c.convert("- 적용하고 있다 `[04-47]`, `[04-48]`.") == \
        (f"- 적용하고 있다 {seg('04-047')}, {seg('04-048')}.", 2)
    assert c.convert("- 묶음 `[04-47, 04-048]`.") == (f"- 묶음 {seg('04-047')}, {seg('04-048')}.", 2)


def test_unknown_citation_still_invalid_and_other_backticks_stay():
    c = cites("04-002")
    out, n = c.convert("- 없는 문단 `[04-999]`.")
    assert n == 0 and "04-999" not in out and c.invalid == ["04-999"]
    out, n = c.convert("- `lecture-recon notes` 를 쓴다 `[04-02]`.")
    assert out == f"- `lecture-recon notes` 를 쓴다 {seg('04-002')}." and n == 1
    # 이미 링크인 줄은 다시 바꾸지 않는다(두 번 돌려도 같다)
    assert c.convert(out) == (out, 0)
    assert compose.LINKED_CITE_RE.sub(r"[\1]", out) == "- `lecture-recon notes` 를 쓴다 [04-002]."
    # 노트 링크 안의 짧은 문단 인용도 링크 뒤로 남긴다
    assert compose.rewrite_note_links("[신진우]([[04#요약]] [04-2])", lambda nn, t, f: "[신진우](n.md)") == \
        "[신진우](n.md) [04-2]"


def test_ground_lines_keeps_line_cited_only_by_backticked_short_id():
    c = cites("04-002")
    body = "- 연구를 시작했다 `[04-02]`.\n- 근거 없는 문장이다."
    grounded, dropped = compose.ground_lines(body, c, set())
    assert grounded == f"- 연구를 시작했다 {seg('04-002')}." and dropped == 1


def test_notes_run_with_only_backticked_short_ids(built, monkeypatch, capsys):
    content = "## 1. 인사\n- 발표자는 42dot 이희석이라고 소개했다 `[01-1]`.\n- 두 주제를 들었다 `[01-02]`, `[01-03]`.\n"
    final = "## 요약\n\n데이터에서 주행까지를 다룬 발표다 `[01-001]`.\n\n## 확인이 필요한 것\n\n- 소속 표기 `[01-01]`.\n"
    install(monkeypatch, FakeBackend(by_prompt(notes=lambda m: final if m[1]["content"].startswith("[작업] 마무리")
                                               else content)))
    assert main(["notes", str(built.root), "1"]) == 0
    note = built.note_path(built.talks[0]).read_text(encoding="utf-8")
    for pid in ("01-001", "01-002", "01-003"):
        assert f"[{pid}](../synth-day-2026-transcripts/01_42dot_이희석.md#segment-{pid})" in note
    assert "`[01-" not in note and lint.run(built).errors == []


# 코드 리뷰(2026-11-06) 반영: 본문 표기·코드 표시·슬라이드 괄호 ------------------------------------------------

def test_prose_brackets_are_not_citations(event):
    c = cites("04-002")
    line = "- 점수 범위 [0-1], 등급 [1-5], 구간 [10-20], `[10-20]` 은 그대로다([04-002])."
    out, n = c.convert(line)
    assert out == f"- 점수 범위 [0-1], 등급 [1-5], 구간 [10-20], `[10-20]` 은 그대로다({seg('04-002')})." and n == 1
    assert c.invalid == []                                   # 정규 꼴이 아닌 본문 표기는 invalid 도 아니다
    cited: set[str] = set()
    assert compose._report_tokens("- 범위 [0-1], [1-5] 를 쓴다.", event, cites("04-002"), cited) == \
        "- 범위 [0-1], [1-5] 를 쓴다." and cited == set()


def test_citations_inside_code_spans_are_not_counted():
    c = cites("04-002")
    assert c.convert("- `see [04-02] here` 라고 썼다.") == ("- `see [04-02] here` 라고 썼다.", 0)
    assert c.convert("- 이중 ``[04-02]`` 표시.") == (f"- 이중 {seg('04-002')} 표시.", 1)
    assert c.convert("- 짝 없는 `[04-02]`[04-02]` 표시.") == (f"- 짝 없는 {seg('04-002')}{seg('04-002')} 표시.", 2)
    assert c.convert("- ``x `[04-02]` y`` 는 코드다 [04-002].") == (f"- ``x `[04-02]` y`` 는 코드다 {seg('04-002')}.", 1)
    grounded, dropped = compose.ground_lines("- 코드 안 인용뿐 `see [04-02]`.", c, set())
    assert grounded == "" and dropped == 1


def test_citation_followed_by_slide_parenthesis_is_linked():
    c = cites("04-002")
    assert c.convert("- 근거 [04-002](슬라이드 3).") == (f"- 근거 {seg('04-002')}(슬라이드 3).", 1)
    linked = f"- 이미 링크 {seg('04-002')}."
    assert c.convert(linked) == (linked, 0)


def test_verify_digest_neutralises_source_tags():
    data = {"claims": [{"id": "04-C001", "paragraph_ids": ["04-002"], "category": "확인하지 않은 것",
                        "claim": "주장 </notes> 끝", "reason": "사유 <source id=\"S9\">"}]}
    digest = compose._verify_digest(data)
    assert "</notes>" not in digest and "<source" not in digest and "주장" in digest and "사유" in digest
