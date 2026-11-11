"""Gemini 무료 티어 실사용(2026-11-06) 에서 드러난 문제의 회귀 테스트:
claims 과소 추출, report 링크 겹침, correct "공식 자료" 근거 게이밍, README·Quality note placeholder, 429 일일 한도·--plan."""

import datetime as dt
import io
import json
import urllib.error
from pathlib import Path

import pytest
import yaml

from fakes import FakeBackend, by_prompt, install, user_of
from lecture_recon import compose, context, correct, lint, llm, scaffold, transcript, verify
from lecture_recon.__main__ import main
from lecture_recon.config import load_event
from test_compose import REPORT, fill_slides, note_responder

TODAY = dt.date.today().isoformat()


def para(pid: str, text: str) -> transcript.Paragraph:
    return transcript.Paragraph(pid, 0.0, text)


# 1. claims: 후보 문장 · 빈 응답 · 인용 정규화 · 묶음별 집계 ------------------------------

def test_candidates_prescan_picks_numbers_units_latin_years_and_skips_unclear():
    paras = [
        para("04-001", '안녕하세요 반갑습니다. [불명확: "RLDX 97.8 점"] 오늘은 날씨가 좋네요.'),
        para("04-002", "인퍼런스를 70밀리초에서 40밀리초로 줄였습니다. 그냥 좋은 이야기입니다. "
                       "RoboCasa 에서 성공률을 크게 올렸고요 2026년에 공개했습니다"),
        para("04-003", "학생이 칠십 퍼센트 정도 됩니다. 이번에 만들어 보았습니다. 삼 년 전부터 했습니다"),
    ]
    got = verify.candidates(paras)
    texts = [c.text for c in got]
    assert [c.no for c in got] == list(range(1, len(got) + 1))
    assert not any("RLDX" in t for t in texts)                       # [불명확] 구간만으로는 후보가 아니다
    assert "인퍼런스를 70밀리초에서 40밀리초로 줄였습니다." in texts
    assert "RoboCasa 에서 성공률을 크게 올렸고요" in texts and "2026년에 공개했습니다" in texts
    assert "학생이 칠십 퍼센트 정도 됩니다." in texts and "삼 년 전부터 했습니다" in texts
    assert not any("그냥 좋은" in t or "이번에" in t or "날씨" in t for t in texts)
    block = verify.candidate_block(got)
    assert block.startswith("[후보 문장]") and f"{len(got)}개" in block and "1. (04-002)" in block
    long = verify.candidates([para("04-009", "어 " * 200 + "정확도는 97.8 이었습니다 " + "그 " * 200)])
    assert len(long[0].text) <= verify.CANDIDATE_CHARS and "97.8" in long[0].text


def test_quote_matches_after_whitespace_punctuation_and_wrapper_normalization():
    p = '인프랑스 스피드를 [추정: 70밀리초에서 40밀리초]에서 사업을 했습니다. [불명확: "그래서 저희가 한 3년 전부터"] 리어벌드,'
    assert verify.quote_in("인프랑스 스피드를 [추정: 70밀리초에서 40밀리초]에서", p)       # 표시 그대로
    assert verify.quote_in("인프랑스 스피드를 70밀리초에서 40밀리초에서 사업을", p)          # 표시를 벗긴 글자
    assert verify.quote_in("인프랑스스피드를  70 밀리초에서, 40밀리초에서", p)                # 공백·문장부호 차이
    assert verify.quote_in("그래서 저희가 한 3년 전부터 리어벌드", p)                        # 불명확 안의 글자
    assert verify.quote_in('[불명확: "그래서 저희가 한 3년 전부터"] 리어벌드', p)
    assert not verify.quote_in("인프랑스 스피드를 30밀리초로", p)                             # 지어낸 인용은 여전히 버린다


CLAIM_OK = {"id": "c1", "candidate": 1, "paragraph_ids": ["01-001"], "quote_from_transcript": "안녕하세요 42dot 이희석입니다",
            "claim": "발표자는 42dot 소속이다", "kind": "고유명사", "queries": ["42dot Lee Heeseok"], "keywords": ["42dot"]}


def test_empty_claims_with_candidates_is_re_requested_with_nudge_and_never_cached(built, monkeypatch, capsys):
    answers = iter([{"claims": []}, {"claims": [CLAIM_OK], "skipped": []}])
    backend = FakeBackend(by_prompt(claims=lambda m: next(answers)))
    install(monkeypatch, backend)
    assert main(["claims", str(built.root), "1", "--chunk-chars", "100000"]) == 0
    assert len(backend.calls) == 2
    nudge = backend.calls[1][-1]["content"]
    assert "후보 문장이" in nudge and "claims 가 비었다" in nudge and "skipped" in nudge
    assert "[후보 문장]" in user_of(backend.calls[0]) and "1. (01-001) 안녕하세요 42dot 이희석입니다." in user_of(backend.calls[0])
    cached = [json.loads(p.read_text())["response"] for p in (built.work_dir / "llm-cache").glob("*.json")]
    assert len(cached) == 1 and '"claims": []' not in cached[0]
    data = yaml.safe_load(verify.claims_path(built, built.talks[0]).read_text(encoding="utf-8"))
    assert [c["claim"] for c in data["claims"]] == ["발표자는 42dot 소속이다"]
    assert data["batch_stats"][0]["candidates"] == 1 and data["candidates"][0]["status"] == "claim"


def test_still_empty_after_nudge_is_recorded_in_dropped(built, monkeypatch, capsys):
    backend = FakeBackend(by_prompt(claims={"claims": []}))
    install(monkeypatch, backend)
    assert main(["claims", str(built.root), "1", "--chunk-chars", "100000"]) == 0
    out = capsys.readouterr().out
    assert "모델이 빈 목록을 반환" in out and "후보 1" in out
    assert not list((built.work_dir / "llm-cache").glob("*.json"))          # 빈 응답은 캐시하지 않는다
    data = yaml.safe_load(verify.claims_path(built, built.talks[0]).read_text(encoding="utf-8"))
    assert data["claims"] == [] and any("모델이 빈 목록을 반환" in d for d in data["dropped"])
    assert data["candidates"][0]["status"] == "none"
    # 다시 실행하면 캐시가 없으니 다시 부른다
    n = len(backend.calls)
    assert main(["claims", str(built.root), "1", "--chunk-chars", "100000"]) == 0 and len(backend.calls) == n + 2


def test_empty_claims_without_candidates_is_valid():
    assert verify._validate_claims('{"claims": []}', {"01-002": "첫 번째 주제는 데이터 수집입니다."}, 0) == ([], [], {})
    # 후보를 모두 사유와 함께 건너뛴 것은 유효하다(사유를 남긴다). 아무것도 분류하지 않은 빈 응답만 EmptyClaims
    keep, dropped, marks = verify._validate_claims(
        '{"claims": [], "skipped": [{"candidate": 1, "reason": "인사말"}]}', {"01-001": "x"}, 1)
    assert keep == [] and marks == {1: ("skip", "인사말")}
    with pytest.raises(verify.EmptyClaims):
        verify._validate_claims('{"claims": [], "skipped": []}', {"01-001": "x"}, 1)
    with pytest.raises(verify.EmptyClaims):
        verify._validate_claims('{"claims": [], "skipped": [{"candidate": 9, "reason": "범위 밖"}]}', {"01-001": "x"}, 1)


def test_per_batch_counts_are_printed_and_recorded(built, monkeypatch, capsys):
    def respond(messages):
        user = user_of(messages)
        if "(01-001)" in user:   # 첫 묶음: 후보 1개 → claim 하나, 버릴 claim 하나
            bad = {**CLAIM_OK, "id": "c2", "candidate": 1, "quote_from_transcript": "지어낸 문장을 인용한다"}
            return {"claims": [CLAIM_OK, bad]}
        return {"claims": []}
    install(monkeypatch, FakeBackend(by_prompt(claims=respond)))
    assert main(["claims", str(built.root), "1", "--chunk-chars", "10"]) == 0
    out = capsys.readouterr().out
    assert "묶음 1/3 (01-001–01-001): 후보 1 · claim 1 · 버림 1 · 건너뜀 0" in out
    assert "묶음 2/3 (01-002–01-002): 후보 0 · claim 0 · 버림 0" in out
    assert "후보 문장 1개: claim 으로 뽑힘 1" in out
    data = yaml.safe_load(verify.claims_path(built, built.talks[0]).read_text(encoding="utf-8"))
    assert [b["candidates"] for b in data["batch_stats"]] == [1, 0, 0]
    assert data["batch_stats"][0] == {"batch": 1, "paragraphs": "01-001–01-001", "candidates": 1, "claims": 1,
                                      "dropped": 1, "skipped": 0, "unclassified": 0}


def test_skipped_and_unclassified_candidates_are_counted():
    paras = {"04-001": "RLDX-1 은 97.8 입니다. GPT-5 도 있습니다. 2026년 공개."}
    text = json.dumps({"claims": [{**CLAIM_OK, "candidate": [1], "paragraph_ids": ["04-001"],
                                   "quote_from_transcript": "RLDX-1 은 97.8 입니다"}],
                       "skipped": [{"candidate": 2, "reason": "검증할 수 없는 의견"}, {"candidate": 99}]},
                      ensure_ascii=False)
    keep, dropped, marks = verify._validate_claims(text, paras, 3)
    assert len(keep) == 1 and dropped == []
    assert marks == {1: ("claim", ""), 2: ("skip", "검증할 수 없는 의견")}       # 3 은 분류 안 됨, 99 는 범위 밖


def test_claims_prompt_states_coverage_target():
    text = llm.prompt("claims")
    for needle in ("[후보 문장]", "skipped", "candidate", "검증할 수 없는 의견이 아니면 모두 claim"):
        assert needle in text


# 2. report 링크: 감싼 토큰 · [text](NN#절) -------------------------------------------------

TRIAL_EVENT = """\
slug: ai-tech-day-2026
name: 2026 AI TECH DAY
date: 2026-10-20
sources: [https://example.org/aitechday]
talks:
  - number: 4
    speaker: 신진우
    org: KAIST
    title: "로봇 파운데이션 모델의 현재와 미래, 그리고 우리의 기회"
    language: ko
    recordings:
      - file: raw/audio/rec.m4a
"""
TRIAL_NOTE = """# 04. 신진우, KAIST — *로봇 파운데이션 모델의 현재와 미래, 그리고 우리의 기회*

## 요약

요약이다.

## 3. 로봇 파운데이션 모델의 구조와 모듈

- 구조.

## 원문 대조 결과

없음.

## 연구 라인에 걸리는 것

근거 있는 연결이 없다.

## 확인이 필요한 것

- 없음.
"""


@pytest.fixture
def trial_like(tmp_path):
    root = tmp_path / "ai-tech-day-2026"
    root.mkdir()
    (root / "event.yaml").write_text(TRIAL_EVENT, encoding="utf-8")
    ev = load_event(root)
    ev.note_path(ev.talks[0]).parent.mkdir(parents=True)
    ev.note_path(ev.talks[0]).write_text(TRIAL_NOTE, encoding="utf-8")
    return ev


def test_report_nested_note_links_become_one_valid_link(trial_like):
    ev = trial_like
    want = "[신진우](ai-tech-day-2026-notes/04_KAIST_신진우.md#3-로봇-파운데이션-모델의-구조와-모듈)"
    cites = compose.Cites.build(ev, f"{ev.slug}-transcripts")
    for form in ("[신진우]([[04#3. 로봇 파운데이션 모델의 구조와 모듈]])",     # 시험에서 나온 그대로
                 "[신진우](04#3. 로봇 파운데이션 모델의 구조와 모듈)",
                 "[[04#3. 로봇 파운데이션 모델의 구조와 모듈]]",
                 "[아무 텍스트]( [[04#3-로봇-파운데이션-모델의-구조와-모듈]] )"):
        cited: set[str] = set()
        assert compose._report_tokens(f"- {form}: 주장이다.", ev, cites, cited) == f"- {want}: 주장이다.", form
        assert cited == {"04"}
    plain = compose._report_tokens("[신진우]([[04]]) 와 [[04#확인이 필요한 것]]", ev, cites, set())
    assert plain == ("[신진우](ai-tech-day-2026-notes/04_KAIST_신진우.md) 와 "
                     "[신진우](ai-tech-day-2026-notes/04_KAIST_신진우.md#확인이-필요한-것)")
    ev.report_path.write_text("# 리포트\n\n## 관통 주제\n\n" + compose._report_tokens(
        "- [신진우]([[04#3. 로봇 파운데이션 모델의 구조와 모듈]]): 주장.", ev, cites, set()) + "\n", encoding="utf-8")
    assert not [e for e in lint.run(ev).errors if "[규칙2]" in e]
    assert "토큰만" in llm.prompt("report")


# 3. correct: "공식 자료" 근거는 등록된 출처와 맞아야 한다 ------------------------------------------

TRIAL_T = """# t

- ASR: small, cpu, x · 교정: 0차 (초안)

<a id="segment-04-004"></a>
**[04-004] [00:01:00]** 오늘은 로봇 바를리더 모델 이야기를 합니다

<a id="segment-04-011"></a>
**[04-011] [00:05:00]** 그때 오프닝 A.I. 나 엔터픽이 3년 전에 나왔습니다
"""


def rep(pid, old, new, evidence, reason="오인식"):
    return {"paragraph_id": pid, "old": old, "new": new, "reason": reason, "evidence": evidence}


def test_official_evidence_must_match_registered_source(trial_like):
    ev = trial_like
    refs = correct.official_refs(ev, ev.talks[0])
    assert "https://example.org/aitechday" in refs.urls and "KAIST" in refs.strings
    patch = {"replacements": [
        # 04-004: event.yaml 의 발표 제목을 인용하고 고친 표기가 그 안에 있다 → 근거(일반)
        rep("04-004", "로봇 바를리더 모델", "로봇 파운데이션 모델",
            "공식 자료: 04. 신진우, KAIST — 로봇 파운데이션 모델의 현재와 미래, 그리고 우리의 기회"),
        # 04-011: 자유 서술 → 근거 아님. 보호 항목 변경이 없으니 추정으로만
        rep("04-011", "오프닝 A.I.", "OpenAI", "공식 자료: AI 분야 주요 기업명(OpenAI)"),
        # event.yaml 문자열(KAIST)을 인용해도 고친 표기와 겹치지 않으면 근거 아님 → 추정
        rep("04-011", "엔터픽이", "Anthropic이", "공식 자료: KAIST"),
    ]}
    res = correct.apply_patch(TRIAL_T, patch, official=refs)
    levels = {a["new"]: a["level"] for a in res.applied}
    assert levels == {"로봇 파운데이션 모델": "일반", "[추정: OpenAI]": "추정", "[추정: Anthropic이]": "추정"}
    notes = [a.get("note", "") for a in res.applied]
    assert sum(correct.OFFICIAL_MISMATCH in n for n in notes) == 2
    assert "[추정: OpenAI]" in res.text and "로봇 파운데이션 모델 이야기" in res.text


def test_correct_command_lists_registered_sources_and_applies_gate(built, monkeypatch):
    from fakes import FakeWeb
    from lecture_recon import sources
    monkeypatch.setattr(sources, "http_get", FakeWeb({"https://example.org/event": "합성 테크 데이 42dot 이희석 " * 40}))
    patch = {"chapters": [], "replacements": [
        rep("01-001", "42dot", "43dot", "공식 자료: 회사 이름 목록"),
        rep("01-001", "이희석입니다", "이희석입니다 (42dot)", "공식 자료: https://example.org/event"),
    ]}
    backend = FakeBackend(lambda m: patch)
    install(monkeypatch, backend)
    assert main(["correct", str(built.root), "1"]) == 0
    assert "- https://example.org/event" in user_of(backend.calls[0])
    record = json.loads((built.work_dir / "correct" / "01.json").read_text(encoding="utf-8"))
    assert record["rejected"][0]["why"].startswith(correct.OFFICIAL_MISMATCH)
    assert [a["level"] for a in record["applied"]] == ["추정"]          # 숫자가 늘어난 변경 + 등록된 URL 본문에 있음


# 4. 기계적 채움: README · Quality note -----------------------------------------------------

def test_fill_after_correct_without_readme_matches_trial_order(built, monkeypatch, capsys):
    """시험 순서: README 가 없을 때 correct → scaffold → fill. 검증 기록·교정 차수·Quality note 가 채워진다."""
    ev = built
    (ev.transcripts_dir / "README.md").unlink()
    install(monkeypatch, FakeBackend(lambda m: {"chapters": [{"paragraph_id": "01-001", "title": "도입"}],
                                                "replacements": []}))
    assert main(["correct", str(ev.root), "1"]) == 0
    assert "README.md 이 없어" in capsys.readouterr().err
    tr = ev.transcript_path(ev.talks[0]).read_text(encoding="utf-8")
    assert "> Quality note: 기계 집계 — ASR `large-v3`(cpu) 세그먼트 5개, 남은 `[불명확]` 1개. 녹음 상태는 사람이 보충한다." in tr
    assert main(["scaffold", str(ev.root)]) == 0
    assert main(["fill", str(ev.root)]) == 0
    readme = (ev.transcripts_dir / "README.md").read_text(encoding="utf-8")
    assert f"- {TODAY}: `lecture-recon correct` 1차 교정(fake/fake-1): 발표 01 장 제목 1개" in readme
    assert f"- 교정: 01 1차 ({TODAY}), 02 0차 (초안)." in readme
    assert "증폭·잡음 제거는 하지 않았다" in readme
    warned = [w for w in lint.run(ev).warnings if "README" in w or ("transcripts/02_" in w and "규칙6" in w)]
    assert warned == []
    tr2 = ev.transcript_path(ev.talks[1]).read_text(encoding="utf-8")
    assert "> Quality note: 기계 집계 — ASR `large-v3`(cpu) 세그먼트 2개, 남은 `[불명확]` 0개." in tr2
    notes_readme = (ev.notes_dir / "README.md").read_text(encoding="utf-8")
    assert "(0–1차 교정본)" in notes_readme and "(남은 `[불명확]` 1개)" in notes_readme
    assert "- 사진: 3장 (슬라이드 3장)" in notes_readme
    assert f"- {TODAY}: `lecture-recon correct 01` 1차 교정(fake/fake-1)." in notes_readme
    # 다시 채워도 바뀌지 않고, 사람이 쓴 Quality note 는 그대로 둔다
    assert scaffold.fill(ev) == []
    path = ev.transcript_path(ev.talks[1])
    human = path.read_text(encoding="utf-8").replace(
        "> Quality note: 기계 집계 — ASR `large-v3`(cpu) 세그먼트 2개, 남은 `[불명확]` 0개. 녹음 상태는 사람이 보충한다.",
        "> Quality note: 강의장 울림이 크다.")
    path.write_text(human, encoding="utf-8")
    assert scaffold.fill(ev) == [] and path.read_text(encoding="utf-8") == human


def test_cli_path_is_lint_clean_without_the_skill(built, monkeypatch, capsys):
    ev = built
    fill_slides(ev, capsys)
    patch = {"chapters": [{"paragraph_id": "01-001", "title": "도입"}, {"paragraph_id": "02-001", "title": "도입"}],
             "replacements": []}                                   # 묶음 밖 장 제목은 거부될 뿐이다
    install(monkeypatch, FakeBackend(by_prompt(correct=patch, notes=note_responder, report=REPORT)))
    for nn in ("1", "2"):
        assert main(["correct", str(ev.root), nn]) == 0
    for nn in ("1", "2"):
        assert main(["notes", str(ev.root), nn]) == 0
    assert main(["report", str(ev.root)]) == 0
    result = lint.run(ev)
    assert result.errors == [] and result.warnings == [], result.warnings
    notes_readme = (ev.notes_dir / "README.md").read_text(encoding="utf-8")
    assert f"`lecture-recon notes 01` 노트 작성(fake/fake-1)" in notes_readme
    assert f"`lecture-recon report` 종합 리포트 작성(fake/fake-1)" in notes_readme
    assert "- 사진: 3장 (슬라이드 3장)" in notes_readme and "(1차 교정본)" in notes_readme


# 5. 일일 한도(429) · --plan -------------------------------------------------------------

DAILY_BODY = json.dumps([{"error": {"code": 429, "message": (
    "You exceeded your current quota, please check your plan and billing details. \n* Quota exceeded for metric: "
    "generativelanguage.googleapis.com/generate_content_free_tier_requests, limit: 20, model: gemini-3.5-flash\n"
    "Please retry in 41.2s."),
    "status": "RESOURCE_EXHAUSTED", "details": [{"violations": [{
        "quotaMetric": "generativelanguage.googleapis.com/generate_content_free_tier_requests",
        "quotaId": "GenerateRequestsPerDayPerProjectPerModel-FreeTier"}]}, {"retryDelay": "41s"}]}}])


MINUTE_BODY = DAILY_BODY.replace("PerDay", "PerMinute").replace("limit: 20", "limit: 5")
COMPAT_MINUTE = json.dumps([{"error": {"code": 429, "message": (
    "You exceeded your current quota... \n* Quota exceeded for metric: generativelanguage.googleapis.com/"
    "generate_content_free_tier_requests, limit: 5, model: gemini-3.5-flash\nPlease retry in 41.2s."),
    "status": "RESOURCE_EXHAUSTED"}}])


def test_daily_quota_detection():
    q = llm.daily_quota(DAILY_BODY)                                         # Gemini: quotaId PerDay 는 retryDelay 가 있어도 일일
    assert q == {"limit": 20, "metric": "generativelanguage.googleapis.com/generate_content_free_tier_requests",
                 "model": "gemini-3.5-flash", "hint": 41.2}
    assert llm.daily_quota(MINUTE_BODY) is None                              # 분 단위는 기다리면 풀린다
    assert llm.daily_quota(COMPAT_MINUTE) is None                            # "limit: N" 만으로는 일일이 아니다
    tpm = COMPAT_MINUTE.replace("generate_content_free_tier_requests, limit: 5", "input_token_count, limit: 250000")
    assert llm.daily_quota(tpm) is None
    groq = ('{"error":{"message":"Rate limit reached for model `llama-3.3-70b-versatile` on tokens per day (TPD): '
            'Limit 100000, Used 99500. Please try again in 6.5s.","code":"rate_limit_exceeded"}}')
    assert llm.daily_quota(groq) is None                                     # 일일 창이라도 6.5초면 기다린다
    assert llm.daily_quota(groq.replace("6.5s", "2h13m")) is not None
    assert llm.daily_quota('{"error":{"message":"Rate limit exceeded: free-models-per-day. ","code":429}}') is not None
    assert llm.daily_quota('{"error": "Too many requests"}') is None


def http_error(code, body):
    return urllib.error.HTTPError("http://x", code, "err", {}, io.BytesIO(body.encode("utf-8")))


def test_daily_429_stops_immediately_and_minute_429_backs_off_with_hint():
    slept = []
    s = llm.load_settings(backend="gemini", env={"GEMINI_API_KEY": "k"}, path=Path("/nonexistent"))

    def daily(req, timeout):
        raise http_error(429, DAILY_BODY)
    with pytest.raises(llm.QuotaExhausted) as exc:
        llm.HTTPBackend(s, sleep=slept.append, opener=daily).complete([{"role": "user", "content": "x"}])
    assert slept == [] and exc.value.exit_code == 4
    msg = str(exc.value)
    assert "20회" in msg and "generate_content_free_tier_requests" in msg and "KST (태평양 시각 자정)" in msg

    script = [http_error(429, COMPAT_MINUTE)]

    def minute(req, timeout):
        if script:
            raise script.pop()
        return type("R", (), {"read": lambda self: b'{"choices": [{"message": {"content": "ok"}}]}',
                              "__enter__": lambda self: self, "__exit__": lambda self, *a: False})()
    assert llm.HTTPBackend(s, sleep=slept.append, opener=minute).complete([{"role": "user", "content": "x"}]) == "ok"
    assert slept == [41.2]                                                  # 본문의 "retry in 41.2s"
    # 분 단위로 보였어도 재시도 5회 뒤에도 429 면 한도로 보고 exit 4
    slept.clear()
    with pytest.raises(llm.QuotaExhausted, match="재시도 5회 뒤에도"):
        llm.HTTPBackend(s, sleep=slept.append, opener=lambda r, timeout: (_ for _ in ()).throw(
            http_error(429, COMPAT_MINUTE))).complete([{"role": "user", "content": "x"}])
    assert len(slept) == 5


def test_quota_exhausted_exits_4_and_keeps_earlier_batches_cached(built, monkeypatch, capsys):
    calls = []

    class Quota(FakeBackend):
        def complete(self, messages, json_mode=False):
            calls.append(1)
            if len(calls) > 1:
                raise llm.QuotaExhausted(llm.quota_message("gemini", "fake-1", llm.daily_quota(DAILY_BODY)))
            return json.dumps({"claims": [CLAIM_OK]}, ensure_ascii=False)
    install(monkeypatch, Quota(lambda m: None))
    assert main(["claims", str(built.root), "1", "--chunk-chars", "10"]) == 4
    err = capsys.readouterr().err
    assert "일일 무료 한도" in err and "exit 4" in err
    assert len(list((built.work_dir / "llm-cache").glob("*.json"))) == 1   # 다시 실행하면 첫 묶음은 캐시에서


def test_plan_counts_calls_without_calling_or_api_key(built, monkeypatch, capsys):
    monkeypatch.setattr(llm, "make_llm", lambda *a, **k: (_ for _ in ()).throw(AssertionError("LLM 을 만들면 안 된다")))
    assert context.default_chunk_chars("gemini") == 12000 and context.default_chunk_chars("groq") == 3000
    assert main(["claims", str(built.root), "1", "--backend", "gemini", "--plan"]) == 0
    out = capsys.readouterr().out
    assert "계획: claims 01 — gemini/gemini-3.5-flash, 묶음 12,000자 → LLM 호출 1회" in out
    assert "후보 문장 1개" in out and "20회/일" in out and "LLM 을 부르지 않았다" in out
    # 캐시 파일이 있어도 형식 검사(빈 claims)를 통과하지 못하면 쓸 수 있는 캐시로 세지 않는다
    rq = verify.claims_requests(built, built.talks[0], 12000)[0]
    key = llm.cache_key("gemini", "gemini-3.5-flash", rq.messages, llm.PRESETS["gemini"].base_url)
    cache = built.work_dir / "llm-cache" / f"{key}.json"
    cache.parent.mkdir(parents=True, exist_ok=True)
    cache.write_text(json.dumps({"response": '{"claims": []}'}), encoding="utf-8")
    assert main(["claims", str(built.root), "1", "--backend", "gemini", "--plan"]) == 0
    assert "쓸 수 있는 캐시 0" in capsys.readouterr().out
    cache.write_text(json.dumps({"response": json.dumps({"claims": [CLAIM_OK]}, ensure_ascii=False)}), encoding="utf-8")
    assert main(["claims", str(built.root), "1", "--backend", "gemini", "--plan"]) == 0
    assert "쓸 수 있는 캐시 1 · 새 호출 0" in capsys.readouterr().out
    assert main(["claims", str(built.root), "1", "--backend", "groq", "--plan", "--chunk-chars", "10"]) == 0
    assert "LLM 호출 3회" in capsys.readouterr().out
    assert main(["correct", str(built.root), "1", "--backend", "gemini", "--plan"]) == 0
    assert "correct-01-1: 01-001–01-003 문단 3개" in capsys.readouterr().out
    assert main(["notes", str(built.root), "1", "--backend", "gemini", "--plan"]) == 0
    assert "LLM 호출 2회" in capsys.readouterr().out
    from test_verify import write_claims
    write_claims(built)
    assert main(["verify", str(built.root), "1", "--backend", "manual", "--plan"]) == 0
    out = capsys.readouterr().out
    assert "LLM 호출 최대 1회 (최악 2회 + 형식 오류 재요청)" in out and "claim 5개 중 다시 볼 것 5개" in out
    assert "붙여넣기" in out
    assert main(["report", str(built.root), "--backend", "gemini", "--plan"]) == 2     # 노트가 없으면 계획도 없다


def test_correct_writes_all_rounds_when_readme_appears_late(built, monkeypatch):
    ev = built
    readme = ev.transcripts_dir / "README.md"
    original = readme.read_text(encoding="utf-8")
    readme.unlink()
    install(monkeypatch, FakeBackend(lambda m: {"chapters": [], "replacements": []}))
    assert main(["correct", str(ev.root), "1"]) == 0          # README 없음: 1차 기록은 json 에만
    readme.write_text(original, encoding="utf-8")              # scaffold 가 나중에 만든 것과 같다
    assert main(["correct", str(ev.root), "1"]) == 0
    text = readme.read_text(encoding="utf-8")
    assert "`lecture-recon correct` 1차 교정" in text and "`lecture-recon correct` 2차 교정" in text
    assert text.count("`lecture-recon correct` 2차 교정") == 1 and f"- 교정: 01 2차 ({TODAY}), 02 0차 (초안)." in text


def test_all_skipped_response_is_cached_and_manual_does_not_loop(built, monkeypatch, capsys):
    skipped = {"claims": [], "skipped": [{"candidate": 1, "reason": "인사말, 검증할 사실 아님"}]}
    real_make_llm = llm.make_llm
    backend = FakeBackend(by_prompt(claims=skipped))
    install(monkeypatch, backend)
    assert main(["claims", str(built.root), "1", "--chunk-chars", "100000"]) == 0
    assert len(backend.calls) == 1 and len(list((built.work_dir / "llm-cache").glob("*.json"))) == 1
    data = yaml.safe_load(verify.claims_path(built, built.talks[0]).read_text(encoding="utf-8"))
    assert data["candidates"][0]["status"] == "skip" and data["candidates"][0]["reason"] == "인사말, 검증할 사실 아님"
    assert main(["claims", str(built.root), "1", "--chunk-chars", "100000"]) == 0 and len(backend.calls) == 1
    # manual: 모두 건너뛴 응답이면 exit 3 을 되풀이하지 않는다
    monkeypatch.setattr(llm, "make_llm", real_make_llm)
    assert main(["claims", str(built.root), "1", "--backend", "manual"]) == 3
    prompt = (built.work_dir / "prompts" / "claims-01-1.md").read_text(encoding="utf-8")
    rid = prompt.split("`request_id: ")[1].split("`")[0]
    (built.work_dir / "responses" / "claims-01-1.md").write_text(
        f"request_id: {rid}\n" + json.dumps(skipped, ensure_ascii=False), encoding="utf-8")
    assert main(["claims", str(built.root), "1", "--backend", "manual"]) == 0


def test_quote_normalization_keeps_number_punctuation_and_min_length():
    p = "성능이 3.5배 좋아졌고 지연은 -20% 줄었습니다 버전 2012345 입니다"
    assert not verify.quote_in("성능이 35배 좋아졌고", p)                 # 소수점은 지운 것으로 보지 않는다
    assert verify.quote_in("성능이 3.5 배 좋아졌고", p)
    assert not verify.quote_in("지연은 20% 줄었습니다", p)               # 부호
    assert not verify.quote_in("1 . 2 . 3 .", p) and not verify.quote_in("12345 입니다", p)   # 숫자 중간에서 시작
    rows = {"claims": [{**CLAIM_OK, "quote_from_transcript": "1 . 2 . 3 . 4 ."}]}
    _, dropped, _ = verify._validate_claims(json.dumps(rows, ensure_ascii=False), {"01-001": "1 2 3 4 버전"}, 1)
    assert "너무 짧다" in dropped[0]                                     # 길이는 공백·문장부호를 뺀 글자로 잰다


def test_claims_flag_correction_and_unclear_basis_and_verify_shows_it(built, monkeypatch):
    ev, talk = built, built.talks[0]
    paras = {"04-038": '인프랑스 스피드를 [추정: 70밀리초에서 40밀리초]에서 사업을 했습니다 [불명확: "RLDX 97.8 점수가 나왔다"]'}
    def claim(cid, quote):
        return {**CLAIM_OK, "id": cid, "candidate": None, "paragraph_ids": ["04-038"], "quote_from_transcript": quote}
    text = json.dumps({"claims": [claim("c1", "40밀리초에서 사업을 했습니다"), claim("c2", "RLDX 97.8 점수가 나왔다"),
                                  claim("c3", "인프랑스 스피드를")]}, ensure_ascii=False)
    keep, _, _ = verify._validate_claims(text, paras, 0)
    flags = [(c.get("from_correction", False), c.get("low_confidence", False)) for c in keep]
    assert flags == [(True, False), (False, True), (False, False)]
    rows = [{**{k: v for k, v in c.items()}, "id": f"01-C00{i}", "paragraph_ids": ["01-001"]}
            for i, c in enumerate(keep[:2], 1)]
    from test_verify import write_claims
    from fakes import FakeWeb, fake_searcher
    write_claims(ev, rows)
    verify.run_verify(ev, talk, llm.LLM(FakeBackend(lambda m: {}), ev.work_dir),
                      fake_searcher(ev.work_dir, FakeWeb({}), {}), progress=lambda m: None)
    md = verify.verify_md_path(ev, talk).read_text(encoding="utf-8")
    row1 = next(x for x in md.splitlines() if x.startswith("| 01-C001 |"))
    row2 = next(x for x in md.splitlines() if x.startswith("| 01-C002 |"))
    assert "교정된 구간 기반" in row1 and "불명확 구간 기반" in row2


def test_official_url_must_be_event_source_and_text_must_contain_change(trial_like):
    ev = trial_like
    from fakes import FakeWeb
    from lecture_recon import sources
    url_ok = rep("04-011", "3년", "4년", "공식 자료: https://example.org/aitechday/ 프로그램")
    # 본문을 못 가져오면 거부(캐시도 없을 때)
    offline = correct.official_refs(ev, ev.talks[0], http=FakeWeb({}, offline=True))
    res = correct.apply_patch(TRIAL_T, {"replacements": [url_ok]}, official=offline)
    assert res.applied == [] and "가져오지 못했다" in res.rejected[0]["why"]
    web = FakeWeb({"https://example.org/aitechday": "행사 소개. 신진우 교수는 4년 전부터 로봇을 연구했다. " * 30})
    refs = correct.official_refs(ev, ev.talks[0], http=web)
    ok = correct.apply_patch(TRIAL_T, {"replacements": [url_ok]}, official=refs)
    assert [a["level"] for a in ok.applied] == ["추정"] and "[추정: 4년]" in ok.text
    # 본문에 고친 표기가 없으면 거부
    miss = correct.apply_patch(TRIAL_T, {"replacements": [{**url_ok, "new": "5년"}]}, official=refs)
    assert miss.applied == [] and miss.rejected[0]["why"].startswith(correct.OFFICIAL_NOT_FOUND)
    assert web.requests == ["https://example.org/aitechday"]                # 받은 본문은 work/sources/ 에 캐시
    assert correct.apply_patch(TRIAL_T, {"replacements": [url_ok]},
                               official=correct.Official(urls={"https://example.org/aitechday"})).applied == []
    # 원칙 2: 등록된 URL 이 있어도 reason 이 보고서·원문이면 거부
    report = correct.apply_patch(TRIAL_T, {"replacements": [{**url_ok, "reason": "보고서 수치"}]}, official=refs)
    assert report.applied == [] and "원칙 2" in report.rejected[0]["why"]
    # work/sources/user(원문 대조 자료)는 교정 근거가 아니다
    d = sources.user_dir(ev.work_dir, "04")
    d.mkdir(parents=True)
    (d / "manifest.json").write_text(json.dumps([{"id": "U1", "target": "https://kaist.example/talk.pdf"}]),
                                     encoding="utf-8")
    refs = correct.official_refs(ev, ev.talks[0], http=web)
    assert "https://kaist.example/talk.pdf" not in refs.urls
    user = correct.apply_patch(TRIAL_T, {"replacements": [
        rep("04-011", "3년", "4년", "공식 자료: 발표 자료 https://kaist.example/talk.pdf p.3")]}, official=refs)
    assert user.applied == [] and user.rejected[0]["why"].startswith(correct.OFFICIAL_MISMATCH)
    # 자유 서술·출처 없음은 보호 항목 변경에서 거부, "슬라이드 NN" 규칙은 그대로
    free = correct.apply_patch(TRIAL_T, {"replacements": [rep("04-011", "3년", "4년", "공식 자료: AI 분야 연표")]},
                               official=refs)
    assert free.applied == [] and free.rejected[0]["why"].startswith(correct.OFFICIAL_MISMATCH)
    slide = correct.apply_patch(TRIAL_T, {"replacements": [rep("04-011", "3년", "4년", "슬라이드 02: 4년")]},
                                slides={2}, official=refs)
    assert [a["level"] for a in slide.applied] == ["추정"]


def test_official_string_path_needs_whole_word_and_never_unlocks_guarded_changes():
    t = """# t

<a id="segment-04-011"></a>
**[04-011] [00:05:00]** 그때 카이스트 연구팀이 3년 전에 에이아이 를 했고 안 했습니다
"""
    refs = correct.Official(strings=["2026 AI TECH DAY", "신진우", "KAIST", "로봇 파운데이션 모델의 현재와 미래, 그리고 우리의 기회"])
    def apply(old, new, ev):
        res = correct.apply_patch(t, {"replacements": [rep("04-011", old, new, ev)]}, official=refs)
        return [(a["new"], a["level"]) for a in res.applied], [r["why"] for r in res.rejected]
    assert apply("3년", "2", "공식 자료: 2026 AI TECH DAY")[0] == []                     # 숫자 → 문자열 경로 불가
    assert apply("3년", "20년", "공식 자료: 2026 AI TECH DAY")[0] == []
    assert apply("카이스트 연구팀이 3년", "KAIST 연구팀이 4년", "공식 자료: KAIST")[0] == []
    assert apply("안 했습니다", "했습니다", "공식 자료: 신진우")[0] == []                  # 부정어
    assert apply("카이스트", "KAIST", "공식 자료: 소속 KAIST")[0] == [("KAIST", "일반")]   # 단어가 같다
    assert apply("에이아이", "AI", "공식 자료: 2026 AI TECH DAY")[0] == [("[추정: AI]", "추정")]   # 3자 미만
    assert apply("카이스트", "KAIST 대학", "공식 자료: KAIST")[0] == [("[추정: KAIST 대학]", "추정")]  # 단어가 다르다


def test_other_recording_evidence_needs_two_recordings(trial_like):
    ev = trial_like
    one = correct.official_refs(ev, ev.talks[0])
    assert one.recordings == 1
    num = rep("04-011", "3년", "4년", "다른 녹음: 없음")
    assert correct.apply_patch(TRIAL_T, {"replacements": [num]}, official=one).applied == []
    word = correct.apply_patch(TRIAL_T, {"replacements": [rep("04-011", "엔터픽이", "Anthropic이", "다른 녹음: 02")]},
                               official=one)
    assert [a["level"] for a in word.applied] == ["추정"]
    two = correct.Official(recordings=2)
    assert [a["level"] for a in correct.apply_patch(TRIAL_T, {"replacements": [num]}, official=two).applied] == ["추정"]


def test_report_link_forms_with_parentheses_lists_and_angle_brackets(trial_like):
    ev = trial_like
    note = ev.note_path(ev.talks[0])
    note.write_text(TRIAL_NOTE.replace("## 3. 로봇 파운데이션 모델의 구조와 모듈", "## 3. 로봇 파운데이션 모델(RFM)의 구조"),
                    encoding="utf-8")
    cites = compose.Cites.build(ev, f"{ev.slug}-transcripts")
    base = "ai-tech-day-2026-notes/04_KAIST_신진우.md"
    cases = {
        "[신진우](04#3. 로봇 파운데이션 모델(RFM)의 구조)": f"[신진우]({base}#3-로봇-파운데이션-모델rfm의-구조)",
        "[신진우]([[04#3. 로봇 파운데이션 모델(RFM)의 구조]])": f"[신진우]({base}#3-로봇-파운데이션-모델rfm의-구조)",
        "[두 발표]([[04]], [[04#요약]])": f"[신진우]({base}), [신진우]({base}#요약)",
        "[x](<[[04#요약]]>)": f"[신진우]({base}#요약)",
        "[x]([[04#요약]] [04-001])": f"[신진우]({base}#요약) ",         # 전사가 없는 문단 인용은 CLI 가 뺀다
    }
    for form, want in cases.items():
        assert compose._report_tokens(f"- {form}: 주장.", ev, cites, set()) == f"- {want}: 주장.", form
    assert compose._report_tokens("[근거](https://example.org/a(b)) [04-001](x.md#segment-04-001)", ev, cites, set()) \
        .startswith("[근거](https://example.org/a(b))")                  # 일반 링크는 그대로
    ev.report_path.write_text("# r\n\n" + "\n".join(compose._report_tokens(f"- {f}", ev, cites, set()) for f in cases)
                              + "\n", encoding="utf-8")
    assert not [e for e in lint.run(ev).errors if "report" in e]


def test_next_reset_is_backend_specific():
    now = dt.datetime(2026, 11, 6, 5, 0, tzinfo=dt.timezone.utc)            # 14:00 KST, 전날 21:00 PST
    assert llm.next_reset(now).strftime("%Y-%m-%d %H:%M %Z") == "2026-11-06 17:00 KST"
    summer = dt.datetime(2026, 10, 20, 8, 0, tzinfo=dt.timezone.utc)        # PDT(일광 절약) → 16:00 KST
    assert llm.next_reset(summer).strftime("%Y-%m-%d %H:%M") == "2026-10-21 16:00"
    q = {"limit": 50, "metric": "", "model": "", "hint": None}
    assert "2026-11-06 17:00 KST (태평양 시각 자정)" in llm.quota_message("gemini", "m", q, now)
    assert "2026-11-07 09:00 KST (UTC 자정)" in llm.quota_message("openrouter", "m", q, now)
    assert "알 수 없다" in llm.quota_message("groq", "m", {**q, "hint": 7200.0}, now) and "7200초" in \
        llm.quota_message("groq", "m", {**q, "hint": 7200.0}, now)


def test_fill_keeps_hand_edited_lines(built, monkeypatch):
    ev = built
    install(monkeypatch, FakeBackend(lambda m: {"chapters": [], "replacements": []}))
    assert main(["correct", str(ev.root), "1"]) == 0
    # Quality note: 기계 줄에 사람이 덧붙이면 그대로 둔다
    tp = ev.transcript_path(ev.talks[0])
    ql = next(x for x in tp.read_text(encoding="utf-8").splitlines() if x.startswith(scaffold.QUALITY_PREFIX))
    tp.write_text(tp.read_text(encoding="utf-8").replace(ql, ql + " 후반 10분 에어컨 잡음."), encoding="utf-8")
    scaffold.fill(ev)
    assert ql + " 후반 10분 에어컨 잡음." in tp.read_text(encoding="utf-8")
    # 노트 README: 발표 목록의 제목·사진 칸을 손으로 고치면 둔다, 작업 기록 줄에 덧붙인 것도 둔다(중복 줄 없음)
    path = ev.notes_dir / "README.md"
    text = path.read_text(encoding="utf-8")
    text = text.replace("| From Data to Driving |", "| From Data to Driving (발표 순서 바뀜) |")
    text = text.replace("| 없음 |", "| 없음 (녹음만, 손으로 적음) |")
    path.write_text(text, encoding="utf-8")
    scaffold.fill(ev)
    text = path.read_text(encoding="utf-8")
    assert "(발표 순서 바뀜)" in text and "| 없음 (녹음만, 손으로 적음) |" in text
    wl = next(x for x in text.splitlines() if "`lecture-recon correct 01`" in x)
    path.write_text(text.replace(wl, wl + " 손으로 3곳 더 고침."), encoding="utf-8")
    scaffold.fill(ev)
    text = path.read_text(encoding="utf-8")
    assert wl + " 손으로 3곳 더 고침." in text and text.count("`lecture-recon correct 01`") == 1
    # 같은 날 다시 돌린 명령은 CLI 모양 그대로인 줄만 바꾼다
    claims = verify.claims_path(ev, ev.talks[0])
    claims.parent.mkdir(parents=True, exist_ok=True)
    for n in (0, 2):
        claims.write_text(yaml.safe_dump({"created": TODAY, "backend": "fake/x", "claims": [{}] * n}), encoding="utf-8")
        scaffold.fill(ev)
    log = path.read_text(encoding="utf-8").split("## 작업 기록", 1)[1]
    assert log.count("`lecture-recon claims 01`") == 1 and "claim 2개(fake/x)" in log


def test_fill_refreshes_unknown_facts_when_event_yaml_gains_them(built):
    ev = built
    path = ev.notes_dir / "README.md"
    text = path.read_text(encoding="utf-8").replace("- 장소: 테스트홀", "- 장소: 미기재").replace(
        "- 주관: DAIS", "- 주관: 서울 어딘가에서 손으로 씀")
    path.write_text(text, encoding="utf-8")
    scaffold.fill(ev)
    text = path.read_text(encoding="utf-8")
    assert "- 장소: 테스트홀" in text and "- 주관: 서울 어딘가에서 손으로 씀" in text


def test_round_line_is_per_talk_and_refreshed(built, monkeypatch):
    ev = built
    install(monkeypatch, FakeBackend(lambda m: {"chapters": [], "replacements": []}))
    readme = ev.transcripts_dir / "README.md"
    assert main(["correct", str(ev.root), "1"]) == 0
    assert f"- 교정: 01 1차 ({TODAY}), 02 0차 (초안)." in readme.read_text(encoding="utf-8")
    assert main(["correct", str(ev.root), "2"]) == 0 and main(["correct", str(ev.root), "2"]) == 0
    assert f"- 교정: 01 1차 ({TODAY}), 02 2차 ({TODAY})." in readme.read_text(encoding="utf-8")
    assert main(["correct", str(ev.root), "1"]) == 0
    assert f"- 교정: 2차 ({TODAY})." in readme.read_text(encoding="utf-8")       # 같아지면 한 번만
    text = readme.read_text(encoding="utf-8").replace(f"- 교정: 2차 ({TODAY}).", f"- 교정: 2차 ({TODAY}), 손으로 확인.")
    readme.write_text(text, encoding="utf-8")
    assert main(["correct", str(ev.root), "1"]) == 0
    assert "손으로 확인." in readme.read_text(encoding="utf-8")                  # 사람이 고친 줄은 둔다


def test_corrupt_user_manifest_is_a_clear_error(built, capsys):
    from lecture_recon import sources
    d = sources.user_dir(built.work_dir, "01")
    d.mkdir(parents=True)
    (d / "manifest.json").write_text("[{broken", encoding="utf-8")
    assert main(["sources", str(built.root), "--talk", "1", "list"]) == 2
    assert "manifest" in capsys.readouterr().err
    (d / "manifest.json").write_text('{"not": "a list"}', encoding="utf-8")
    assert main(["sources", str(built.root), "--talk", "1", "list"]) == 2
