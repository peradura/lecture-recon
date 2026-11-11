import datetime as dt
import json

import pytest

from fakes import FakeBackend, install, page
from lecture_recon import context, correct, lint, transcript
from lecture_recon.__main__ import main

TODAY = dt.date.today().isoformat()
UNCLEAR = '[불명확: "엔드투엔드 오토 라벨링"]'

PATCH = {
    "chapters": [
        {"paragraph_id": "01-001", "title": "도입"},
        {"paragraph_id": "01-003", "title": "## [00:00:22] 모델 학습"},
        {"paragraph_id": "01-099", "title": "없는 문단"},
        {"paragraph_id": "01-002", "title": "<제목>"},
    ],
    "replacements": [
        {"paragraph_id": "01-001", "old": "데이터에서 주행까지를", "new": "From Data to Driving 을",
         "reason": "발표 제목 표기", "evidence": "공식 자료: event.yaml 발표 제목 \"From Data to Driving\""},
        {"paragraph_id": "01-001", "old": "주행까지를 이야기", "new": "주행 이야기", "reason": "겹침", "evidence": ""},
        {"paragraph_id": "01-001", "old": "42dot", "new": "43dot", "reason": "오인식", "evidence": ""},
        {"paragraph_id": "01-001", "old": "니다.", "new": "니다!", "reason": "모호", "evidence": ""},
        {"paragraph_id": "01-002", "old": UNCLEAR, "new": "엔드투엔드 오토 라벨링", "reason": "들린다", "evidence": ""},
        {"paragraph_id": "01-002", "old": UNCLEAR, "new": "[추정: End-to-End Auto Labeling]",
         "reason": "슬라이드 표기", "evidence": "슬라이드 02: End-to-End Auto Labeling"},
        {"paragraph_id": "01-003", "old": "모델 학습", "new": "모델 훈련", "reason": "논문과 다름", "evidence": ""},
        {"paragraph_id": "01-003", "old": "두 번째는", "new": "두 번째는 안", "reason": "부정어 추가", "evidence": ""},
        {"paragraph_id": "01-003", "old": "학습입니다.", "new": "학습입니다.\n**[01-004] [00:00:30]** 끼워 넣기",
         "reason": "구조", "evidence": ""},
        {"paragraph_id": "01-001", "old": "**[01-001] [00:00:00]**", "new": "**[01-009] [00:00:00]**",
         "reason": "ID", "evidence": ""},
        {"paragraph_id": "01-002", "old": "", "new": "x", "reason": "빈 old", "evidence": ""},
    ],
}


def test_patch_keeps_ids_timestamps_anchors(built):
    path = built.transcript_path(built.talks[0])
    before = path.read_text(encoding="utf-8")
    res = correct.apply_patch(before, PATCH, slides={1, 2, 3}, official=correct.official_refs(built, built.talks[0]))
    assert correct.invariants(res.text) == correct.invariants(before)
    applied = {(a["paragraph_id"], a["new"]): a["level"] for a in res.applied}
    assert applied == {("01-001", "From Data to Driving 을"): "일반",
                       ("01-002", "[추정: End-to-End Auto Labeling]"): "추정"}
    why = {(r["paragraph_id"], r.get("new")): r["why"] for r in res.rejected}
    assert "겹친다" in why[("01-001", "주행 이야기")]
    assert "숫자" in why[("01-001", "43dot")]
    assert "2번" in why[("01-001", "니다!")]
    assert "`[불명확]` 표시" in why[("01-002", "엔드투엔드 오토 라벨링")]
    assert "원칙 2" in why[("01-003", "모델 훈련")]
    assert "부정어" in why[("01-003", "두 번째는 안")]
    assert "구조" in why[("01-003", "학습입니다.\n**[01-004] [00:00:30]** 끼워 넣기")]
    assert "구조" in why[("01-001", "**[01-009] [00:00:00]**")]
    assert "비어" in why[("01-002", "x")]
    assert res.unclear_resolved == 1
    # 장 제목: 문단 블록 바로 앞에만, 시각은 문단에서
    assert [c["paragraph_id"] for c in res.chapters] == ["01-001", "01-003"]
    assert '## [00:00:00] 도입\n\n<a id="segment-01-001"></a>' in res.text
    assert '## [00:00:22] 모델 학습\n\n<a id="segment-01-003"></a>' in res.text
    assert {c["paragraph_id"] for c in res.chapters_rejected} == {"01-099", "01-002"}
    assert transcript.parse(res.text).chapters == [(0, "도입"), (22, "모델 학습")]
    # 이미 장 제목이 있으면 다시 붙이지 않는다
    again = correct.apply_patch(res.text, {"chapters": [{"paragraph_id": "01-001", "title": "또"}]})
    assert again.chapters == [] and "이미" in again.chapters_rejected[0]["why"]


def test_patch_outside_batch_is_rejected(built):
    text = built.transcript_path(built.talks[0]).read_text(encoding="utf-8")
    res = correct.apply_patch(text, {"replacements": [PATCH["replacements"][0]]}, allowed={"01-002"})
    assert res.applied == [] and "묶음" in res.rejected[0]["why"]


def test_correct_command_writes_backup_record_header_readme(built, monkeypatch, capsys):
    backend = FakeBackend(lambda m: PATCH)
    install(monkeypatch, backend)
    path = built.transcript_path(built.talks[0])
    original = path.read_text(encoding="utf-8")
    assert main(["correct", str(built.root), "1"]) == 0
    out = capsys.readouterr().out
    assert "1차 교정" in out and "치환 2개 적용(추정 1개)" in out
    text = path.read_text(encoding="utf-8")
    assert f"교정: 1차 ({TODAY})" in text and "교정: 0차" not in text
    assert "> 기계 전사 후 교정본이다." in text and "`[추정: …]`" in text and "기계 전사 초안이다" not in text
    assert (built.work_dir / "01_draft0.md").read_text(encoding="utf-8") == original
    record = json.loads((built.work_dir / "correct" / "01.json").read_text(encoding="utf-8"))
    assert record["round"] == 1 and len(record["applied"]) == 2 and len(record["rejected"]) == 9
    readme = (built.transcripts_dir / "README.md").read_text(encoding="utf-8")
    assert f"- {TODAY}: `lecture-recon correct` 1차 교정(fake/fake-1)" in readme
    assert "풀어 낸 `[불명확]` 1개" in readme and f"- 교정: 01 1차 ({TODAY}), 02 0차 (초안)." in readme
    assert lint.run(built).errors == []
    # 2차: 같은 응답이라도 이미 적용된 것은 old 가 없어 거부, 차수만 오른다
    assert main(["correct", str(built.root), "1"]) == 0
    text2 = path.read_text(encoding="utf-8")
    assert f"교정: 2차 ({TODAY})" in text2
    assert correct.invariants(text2) == correct.invariants(original)
    assert (built.work_dir / "01_draft1.md").exists()
    assert len(json.loads((built.work_dir / "correct" / "01.json").read_text())["history"]) == 1


def test_correct_manual_writes_prompts_then_applies(built, capsys):
    assert main(["correct", str(built.root), "1", "--backend", "manual"]) == 3
    err = capsys.readouterr().err
    assert "work/responses/correct-01-1.md" in err
    prompt = (built.work_dir / "prompts" / "correct-01-1.md").read_text(encoding="utf-8")
    assert "[01-002] [00:00:11]" in prompt and "슬라이드 표" in prompt
    rid = prompt.split("`request_id: ")[1].split("`")[0]
    (built.work_dir / "responses" / "correct-01-1.md").write_text(
        f"request_id: {rid}\n" + json.dumps({"chapters": [{"paragraph_id": "01-001", "title": "도입"}],
                                              "replacements": []}, ensure_ascii=False), encoding="utf-8")
    assert main(["correct", str(built.root), "1", "--backend", "manual"]) == 0
    assert "## [00:00:00] 도입" in built.transcript_path(built.talks[0]).read_text(encoding="utf-8")


# 별칭·사용자 자료 용어 (2026-11-06 flash 실측 2차 뒤) ---------------------------------------------------------

ALIAS_T = """# t

<a id="segment-04-011"></a>
**[04-011] [00:05:00]** 저희 리얼월드 의 리얼넥트곤 모델은 큐웬쓰리 기반으로 3년 전에 나왔고 칠십 퍼센트를 넘었습니다
"""


def fix(old, new, evidence, official, text=ALIAS_T, notes=False):
    res = correct.apply_patch(text, {"replacements": [{"paragraph_id": "04-011", "old": old, "new": new,
                                                        "reason": "고유명사 표기", "evidence": evidence}]},
                              official=official)
    applied = [(a["new"], a["level"], a.get("note", "")) if notes else (a["new"], a["level"]) for a in res.applied]
    return applied, [r["why"] for r in res.rejected]


def test_event_info_and_official_strings_include_aliases(built):
    talk = built.talks[0]
    assert "별칭" not in context.event_info(built, talk)
    talk.aliases = ["RLWRLD", "Jinwoo Shin", "RLDX-1"]
    assert "- 별칭: RLWRLD, Jinwoo Shin, RLDX-1" in context.event_info(built, talk)
    assert {"RLWRLD", "Jinwoo Shin", "RLDX-1"} <= set(correct.official_refs(built, talk).strings)


def test_alias_evidence_passes_string_gate():
    with_alias = correct.Official(strings=["신진우", "KAIST", "RLWRLD", "RLDX-1"], aliases=["RLWRLD", "RLDX-1"])
    without = correct.Official(strings=["신진우", "KAIST"])
    assert fix("리얼월드", "RLWRLD", "[행사 정보] 별칭 RLWRLD", with_alias) == ([("RLWRLD", "일반")], [])
    applied, _ = fix("리얼월드", "RLWRLD", "[행사 정보] 별칭 RLWRLD", without)      # 별칭이 없으면 근거 아님
    assert applied == [("[추정: RLWRLD]", "추정")]
    applied, rejected = fix("리얼넥트곤", "RLDX-1", "[행사 정보] 별칭 RLDX-1", without)   # 숫자 변경: 별칭 없으면 거부
    assert applied == [] and rejected[0].startswith(correct.OFFICIAL_MISMATCH)


NAME_T = ALIAS_T.replace("칠십 퍼센트를", "70% 를")


@pytest.mark.parametrize("old, new, evidence, official", [
    ("리얼넥트곤", "RLDX-1", "[행사 정보] 별칭 RLDX-1", correct.Official(strings=["RLDX-1"], aliases=["RLDX-1"])),
    ("큐웬쓰리", "Qwen3", "[사용자 자료] U1", correct.Official(user_texts={"U1": "Built on Qwen3 and Qwen3-VL."})),
])
def test_digit_inside_attested_name_is_a_name_not_a_number(old, new, evidence, official):
    applied, rejected = fix(old, new, evidence, official, notes=True)
    assert rejected == [] and applied == [(new, "일반", correct.NAME_DIGITS_NOTE)]   # 감사용 메모를 남긴다
    assert fix(old, f"[추정: {new}]", evidence, official, notes=True)[0] == \
        [(f"[추정: {new}]", "추정", correct.NAME_DIGITS_NOTE)]
    bumped = new[:-1] + str(int(new[-1]) + 1)                                         # RLDX-2 / Qwen4: 근거에 없는 이름
    assert fix(old, bumped, evidence, official)[0] == []
    assert fix("70%", new, evidence, official, text=NAME_T)[0] == []                 # old 에 숫자가 있다
    with_digit = ALIAS_T.replace(old, f"{old} 2")                                    # 숫자만 바뀌는 경우도
    assert correct.guarded_changes(with_digit, with_digit.replace(f"{old} 2", new)) == ["숫자"]
    assert fix(f"{old} 2", new, evidence, official, text=with_digit)[0] == []
    assert fix(old, f"{new} 70%", evidence, official)[0] == []                       # 이름 밖의 숫자(·단위)
    assert fix(old, f"{new} 70", evidence, official)[0] == []                        # 이름 밖의 숫자
    assert fix(old, f"{new}0", evidence, official)[0] == []                          # 낱말 경계: RLDX-10 / Qwen30
    assert fix("모델은", f"모델은 안", evidence, official)[0] == []                   # 다른 보호 항목은 그대로


def test_name_digit_exemption_needs_alias_or_user_term_evidence():
    official = correct.Official(strings=["RLDX-1"], aliases=["RLDX-1"], user_texts={"U1": "Built on Qwen3."})
    assert fix("리얼넥트곤", "RLDX-1", "공식 자료: RLDX-1", official)[0] == []            # 별칭 근거가 아니다
    assert fix("리얼넥트곤", "RLDX-1", "", official)[0] == []
    assert fix("리얼넥트곤", "RLDX-1", "[사용자 자료] U1", official)[0] == []             # U1 에 없는 이름
    assert fix("큐웬쓰리", "Qwen3", "[행사 정보] 별칭 Qwen3", official)[0] == []          # 별칭이 아니다
    title_only = correct.Official(strings=["RLDX-1 의 현재와 미래"])                     # 발표 제목 문자열은 별칭이 아니다
    assert fix("리얼넥트곤", "RLDX-1", "[행사 정보] 제목 RLDX-1 의 현재와 미래", title_only)[0] == []


def test_user_terms_extracts_proper_nouns():
    text = ("RLWRLD unveiled RLDX-1 at the event. RLDX-1 beats pi0.5 on LIBERO and RoboCasa with Qwen3 on H200 GPUs. "
            "See the PDF and HTML version (URL, API). The Robot is new. AI is here. "
            "ALLEX ALLEX allex. ParallelCluster.")
    terms = correct.user_terms(text)
    assert terms[:2] == ["RLDX-1", "ALLEX"]                    # 2회씩(같으면 먼저 나온 순), 소문자 allex 는 세지 않는다
    assert {"RLWRLD", "pi0.5", "LIBERO", "RoboCasa", "Qwen3", "H200", "ParallelCluster"} <= set(terms)
    assert not {"PDF", "HTML", "URL", "API", "GPUs", "AI", "The", "Robot", "allex"} & set(terms)
    assert correct.user_terms(text, limit=2) == terms[:2]


def test_user_source_evidence_needs_term_in_that_source():
    official = correct.Official(user_texts={"U1": "RLWRLD unveiled RLDX-1 in 4년 of work.", "U2": "Other text only."})
    assert fix("리얼월드", "RLWRLD", "[사용자 자료] U1", official) == ([("RLWRLD", "일반")], [])
    assert fix("리얼월드", "RLWRLD", "사용자 자료 U1", official) == ([("RLWRLD", "일반")], [])
    applied, _ = fix("리얼월드", "RLWRLD", "[사용자 자료] U2", official)            # 그 자료에 없다
    assert applied == [("[추정: RLWRLD]", "추정")]
    res = correct.apply_patch(ALIAS_T, {"replacements": [{"paragraph_id": "04-011", "old": "리얼월드", "new": "RLWRLD",
                                                          "reason": "표기", "evidence": "[사용자 자료] U9"}]},
                              official=official)
    assert res.applied[0]["level"] == "추정" and "U9 가 없다" in res.applied[0]["note"]   # 없는 자료 ID
    # 보호 항목(숫자) 변경은 사용자 자료 본문에 있어도 거부한다(이름 안의 숫자만 예외: RLDX-1 은 U1 의 용어)
    applied, rejected = fix("3년", "4년", "[사용자 자료] U1", official)
    assert applied == [] and rejected == [correct.USER_GUARDED]
    assert fix("리얼넥트곤", "RLDX-1", "[사용자 자료] U1", official)[0] == [("RLDX-1", "일반")]
    assert fix("리얼넥트곤", "RLDX-1", "[사용자 자료] U2", official)[1] == [correct.USER_GUARDED]


def test_correct_prompt_lists_user_source_terms_only_when_present(built, tmp_path):
    ev, talk = built, built.talks[0]
    assert "[사용자 자료 용어]" not in correct.correct_requests(ev, talk, 100000)[0].user
    doc = tmp_path / "official.txt"
    doc.write_text(page("RLWRLD presents RLDX-1 and ALLEX. RLDX-1 runs on H200 with LIBERO results."), encoding="utf-8")
    assert main(["sources", str(ev.root), "--talk", "1", "add", str(doc), "--note", "발표자 공식 슬라이드"]) == 0
    user = correct.correct_requests(ev, talk, 100000)[0].user
    assert "[사용자 자료 용어]" in user and "- U1 (발표자 공식 슬라이드): RLDX-1, " in user and "ALLEX" in user
    assert "U1" in correct.official_refs(ev, talk).user_texts
