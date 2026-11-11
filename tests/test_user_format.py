"""사용자 형식 우선: templates_dir·prompts_dir·required_sections (없으면 이 레포의 형식)."""

import re

import pytest

import synth
from fakes import FakeBackend, by_prompt, install
from lecture_recon import compose, lint, llm, scaffold
from lecture_recon.__main__ import main
from lecture_recon.config import LectureReconError
from test_compose import LINES_YAML, REPORT, note_responder

MY_NOTE = """# {{nn}}. {{speaker_org}} — 내 형식

> 원자료: {{source_line}}

## 요약

요약을 쓴다.

{{slides_section}}

## 내 절

내 절 본문.
"""
# 패키지 템플릿 문구(compose.SKELETON_MARKERS)가 하나도 없는 사용자 노트·리포트 템플릿
MY_FULL_NOTE = """# {{nn}}. {{speaker_org}} — 내 노트

> 원자료: {{source_line}}

## 요약

(요약은 나중에)

{{slides_section}}

## 원문 대조 결과

(대조는 나중에)

## 확인이 필요한 것

(나중에)
"""
MY_REPORT = """# {{name}} — 내 리포트

> 재구성: {{reconstructor}}

## 관통 주제

(주제는 나중에)

## 확인이 필요한 것

(나중에)
"""


def write_config(text: str) -> None:
    llm.config_path().write_text(text, encoding="utf-8")


def template_dir(tmp_path, name: str, files: dict[str, str]):
    d = tmp_path / name
    d.mkdir(exist_ok=True)
    for fname, text in files.items():
        (d / fname).write_text(text, encoding="utf-8")
    return d


def render_note(tmp_path, text, **values):
    write_config(f"templates_dir: {template_dir(tmp_path, 't', {'note.md': text})}\n")
    return scaffold.render("note.md", **values)


def test_templates_dir_overrides_only_files_it_has(tmp_path, built):
    mine = template_dir(tmp_path, "my-templates", {"note.md": MY_NOTE})
    write_config(f"templates_dir: {mine}\n")
    assert llm.template_path("note.md") == mine / "note.md"
    assert main(["scaffold", str(built.root), "--force"]) == 0
    note = built.note_path(built.talks[0]).read_text(encoding="utf-8")
    assert note.startswith(f"{scaffold.SKELETON_MARK}\n# 01. 이희석, 42dot — 내 형식\n")
    assert "## 내 절" in note and "## 원문 대조 결과" not in note
    # 없는 파일은 패키지 것
    assert built.report_path.read_text(encoding="utf-8").startswith(f"{scaffold.SKELETON_MARK}\n# 합성 테크 데이 종합 리포트\n")
    assert "## 행사 정보" in (built.notes_dir / "README.md").read_text(encoding="utf-8")


def test_templates_dir_env_beats_config(tmp_path, monkeypatch):
    from_config = template_dir(tmp_path, "cfg", {"event.gitignore": "config/\n"})
    from_env = template_dir(tmp_path, "env", {"event.gitignore": "env/\n"})
    write_config(f"templates_dir: {from_config}\n")
    assert scaffold.template("event.gitignore") == "config/\n"
    monkeypatch.setenv("LECTURE_RECON_TEMPLATES_DIR", str(from_env))
    assert scaffold.template("event.gitignore") == "env/\n"
    monkeypatch.setenv("LECTURE_RECON_TEMPLATES_DIR", "   ")     # 공백만이면 설정 파일 값
    assert scaffold.template("event.gitignore") == "config/\n"


def test_prompts_dir_env_beats_config_and_falls_back_to_package(tmp_path, monkeypatch):
    from_config, from_env = tmp_path / "cfg-prompts", tmp_path / "env-prompts"
    for d in (from_config, from_env):
        d.mkdir()
        (d / "notes.md").write_text(f"# lecture-recon: 내 노트 지시 (notes) {d.name}\n", encoding="utf-8")
    write_config(f"prompts_dir: {from_config}\n")
    assert llm.prompt("notes").endswith("cfg-prompts\n")
    monkeypatch.setenv("LECTURE_RECON_PROMPTS_DIR", str(from_env))
    assert llm.prompt("notes").endswith("env-prompts\n")
    assert "토큰만" in llm.prompt("report")                     # 없는 파일은 패키지 것


def test_override_dir_is_expanded_and_must_exist(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    (tmp_path / "tpl").mkdir()
    (tmp_path / "tpl" / "event.gitignore").write_text("raw/\n", encoding="utf-8")
    write_config("templates_dir: ~/tpl\n")
    assert scaffold.template("event.gitignore") == "raw/\n"
    write_config("templates_dir: ~/없는-디렉터리\n")
    with pytest.raises(LectureReconError, match="templates_dir 디렉터리가 없다"):
        scaffold.template("note.md")


def test_relative_dirs_in_config_are_relative_to_config_dir(tmp_path, monkeypatch):
    """설정 파일의 상대 경로는 설정 파일 디렉터리 기준(CWD 아님). 오류에는 절대 경로와 출처를 적는다."""
    tpl = llm.config_dir() / "my-tpl"
    tpl.mkdir()
    (tpl / "event.gitignore").write_text("rel/\n", encoding="utf-8")
    (tmp_path / "my-tpl").mkdir()                                  # CWD 쪽 같은 이름은 쓰지 않는다
    (tmp_path / "my-tpl" / "event.gitignore").write_text("cwd/\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    write_config("templates_dir: my-tpl\n")
    assert scaffold.template("event.gitignore") == "rel/\n"
    write_config("prompts_dir: none-here\n")
    absolute = re.escape(str(llm.config_dir().absolute() / "none-here"))
    with pytest.raises(LectureReconError, match=rf"prompts_dir 디렉터리가 없다: {absolute} \(.*`prompts_dir:`\)"):
        llm.prompt("notes")
    monkeypatch.setenv("LECTURE_RECON_PROMPTS_DIR", "env-none")
    absolute = re.escape(str(tmp_path / "env-none"))
    with pytest.raises(LectureReconError, match=rf"{absolute} \(환경변수 LECTURE_RECON_PROMPTS_DIR\)"):
        llm.prompt("notes")


# 조건 블록 `<!-- if KEY -->` ---------------------------------------------------------------

def test_unknown_condition_key_is_an_error(tmp_path):
    with pytest.raises(LectureReconError, match="모르는 조건 키 `없는_키`"):
        render_note(tmp_path, "# x\n<!-- if 없는_키 -->\n## 절\n<!-- endif -->\n")


def test_condition_markers_are_whole_lines(tmp_path):
    """마커는 단독 줄(앞뒤 공백 허용)이고 줄째 지운다. 키는 영문·숫자·`_`·`-`."""
    text = "- 목록\n  <!-- if my-key -->  \n  - 안쪽\n\t<!--endif-->\n끝\n"
    assert render_note(tmp_path, text, **{"my-key": True}) == "- 목록\n  - 안쪽\n끝\n"
    assert render_note(tmp_path, text, **{"my-key": False}) == "- 목록\n끝\n"
    assert render_note(tmp_path, "<!-- if a -->\n끝 줄\n<!-- endif -->", a=True) == "끝 줄\n"


@pytest.mark.parametrize("text, match", [
    ("<!-- if a -->\nx\n<!-- if b -->\ny\n<!-- endif -->\n<!-- endif -->\n", "중첩은 지원하지 않는다"),
    ("앞\n<!-- if a -->\nx\n", "짝이 맞지 않거나"),
    ("앞\n<!-- endif -->\n뒤\n", "짝이 맞지 않거나"),
    ("앞 <!-- if a -->\nx\n<!-- endif -->\n", "단독 줄이 아닌"),
    ("<!-- if a -->\nx <!-- endif -->\n", "단독 줄이 아닌"),
])
def test_broken_condition_markers_are_errors(tmp_path, text, match):
    with pytest.raises(LectureReconError, match=match):
        render_note(tmp_path, text, a=True, b=True)


def test_conditions_apply_to_every_template(tmp_path, monkeypatch):
    """research_lines 조건은 노트·리포트뿐 아니라 README·event.yaml 등 모든 템플릿에서 쓴다."""
    readme = scaffold.template("notes_readme.md") + "<!-- if research_lines -->\n## 연구 라인 메모\n<!-- endif -->\n"
    event_yaml = "slug: {{slug}}\nname: {{name}}\ndate: {{date}}\n<!-- if research_lines -->\n# 연구 라인 있음\n<!-- endif -->\n"
    mine = template_dir(tmp_path, "tpl", {"notes_readme.md": readme, "event.yaml": event_yaml})
    write_config(f"templates_dir: {mine}\n")
    ev = synth.build_scaffolded(tmp_path / "off")
    assert "연구 라인" not in (ev.notes_dir / "README.md").read_text(encoding="utf-8")
    assert main(["init", "x", "--root", str(tmp_path / "off")]) == 0
    assert "연구 라인 있음" not in (tmp_path / "off" / "x" / "event.yaml").read_text(encoding="utf-8")
    (llm.config_dir() / llm.RESEARCH_LINES_FILE).write_text(LINES_YAML, encoding="utf-8")
    ev = synth.build_scaffolded(tmp_path / "on")
    assert "## 연구 라인 메모\n" in (ev.notes_dir / "README.md").read_text(encoding="utf-8")
    assert main(["init", "x", "--root", str(tmp_path / "on")]) == 0
    assert "# 연구 라인 있음\n" in (tmp_path / "on" / "x" / "event.yaml").read_text(encoding="utf-8")


# 골격 마커 ----------------------------------------------------------------------------------

def test_user_templates_skeleton_is_not_mistaken_for_hand_written(tmp_path, monkeypatch):
    """패키지 문구가 없는 사용자 note.md·report.md 골격도 마커로 골격으로 본다: notes·report 가 --force 없이 쓴다."""
    write_config(f"templates_dir: {template_dir(tmp_path, 'tpl', {'note.md': MY_FULL_NOTE, 'report.md': MY_REPORT})}\n")
    ev = synth.build_scaffolded(tmp_path)
    skeleton = ev.note_path(ev.talks[0]).read_text(encoding="utf-8")
    assert skeleton.startswith(scaffold.SKELETON_MARK + "\n") and not any(m in skeleton for m in compose.SKELETON_MARKERS)
    assert not compose.written_note(ev, ev.talks[0])
    with pytest.raises(LectureReconError, match="작성된 노트가 하나도 없다"):
        compose.report_inputs(ev)
    backend = FakeBackend(by_prompt(notes=note_responder, report=REPORT))
    compose.run_notes(ev, ev.talks[0], llm.LLM(backend, ev.work_dir))
    compose.run_report(ev, llm.LLM(backend, ev.work_dir))
    note = ev.note_path(ev.talks[0]).read_text(encoding="utf-8")
    report = ev.report_path.read_text(encoding="utf-8")
    assert scaffold.SKELETON_MARK not in note and scaffold.SKELETON_MARK not in report
    assert compose.written_note(ev, ev.talks[0]) and not list((ev.work_dir / "backup").glob("*"))
    assert report.startswith("# 합성 테크 데이 종합 리포트\n") and lint.run(ev).errors == []
    # 손으로 고치면 그때부터는 --force 가 있어야 덮어쓴다
    ev.note_path(ev.talks[0]).write_text(note + "\n손 메모([01-001]).\n", encoding="utf-8")
    install(monkeypatch, backend)
    assert main(["notes", str(ev.root), "1"]) == 2


def test_skeleton_edited_with_marker_left_is_protected(tmp_path, monkeypatch):
    """마커를 지우지 않고 손으로 쓴 노트: scaffold 가 남긴 sha256 과 달라서 골격이 아니다 → notes 는 --force 없이 거부."""
    ev = synth.build_scaffolded(tmp_path)
    path = ev.note_path(ev.talks[0])
    rec = scaffold.skeleton_record(ev, path)
    assert rec.is_file() and compose.is_skeleton(path.read_text(encoding="utf-8"), ev, path)
    path.write_text(path.read_text(encoding="utf-8").replace("## 요약\n\n<세 문장 이내.", "## 요약\n\n손으로 쓴 요약([01-001]).\n<세 문장 이내."),
                    encoding="utf-8")
    assert not compose.is_skeleton(path.read_text(encoding="utf-8"), ev, path)
    assert compose.written_note(ev, ev.talks[0])
    install(monkeypatch, FakeBackend(by_prompt(notes=note_responder)))
    assert main(["notes", str(ev.root), "1"]) == 2
    # fill·slides 처럼 도구가 골격을 고친 것은 sha256 을 갱신하므로 계속 골격이다
    ev2 = synth.build_scaffolded(tmp_path / "b")
    p2 = ev2.note_path(ev2.talks[0])
    scaffold.write_tool(ev2, p2, p2.read_text(encoding="utf-8").replace("<세 문장 이내.", "<세 문장 이내!"))
    assert compose.is_skeleton(p2.read_text(encoding="utf-8"), ev2, p2)


def test_required_sections_override(built):
    note = built.note_path(built.talks[0])
    text = note.read_text(encoding="utf-8").replace("## 원문 대조 결과", "## 대조")
    note.write_text(text, encoding="utf-8")
    assert "[규칙5] 필수 절 `## 원문 대조 결과` 없음" in "\n".join(lint.run(built).errors)
    write_config("required_sections: [요약, 대조, 내 절]\n")
    errors = "\n".join(lint.run(built).errors)
    assert "`## 원문 대조 결과`" not in errors and "[규칙5] 필수 절 `## 내 절` 없음" in errors
    assert "`## 연구 라인에 걸리는 것`" not in errors
    write_config("required_sections: 요약\n")
    with pytest.raises(LectureReconError, match="문자열 목록"):
        lint.run(built)
