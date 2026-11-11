import json

import pytest
import yaml

from fakes import FakeBackend, FakeWeb, by_prompt, fake_searcher, install, page, user_of
from lecture_recon import llm, sources, verify
from lecture_recon.__main__ import main

CLAIMS = [
    {"id": "01-C001", "paragraph_ids": ["01-001"], "quote_from_transcript": "42dot 이희석입니다", "kind": "수치",
     "claim": "RLDX-1 의 LIBERO 평균 성공률은 97.8 이다", "queries": ["RLDX-1 LIBERO"], "keywords": ["LIBERO", "97.8"]},
    {"id": "01-C002", "paragraph_ids": ["01-002"], "quote_from_transcript": "데이터 수집입니다", "kind": "수치",
     "claim": "추론 지연은 40ms 다", "queries": ["RLDX-1 latency"], "keywords": ["latency", "ms"]},
    {"id": "01-C003", "paragraph_ids": ["01-003"], "quote_from_transcript": "모델 학습입니다", "kind": "고유명사",
     "claim": "RoboCasa 에서 70% 를 넘긴 첫 모델이다", "queries": ["RoboCasa kitchen"], "keywords": ["RoboCasa"]},
    {"id": "01-C004", "paragraph_ids": ["01-003"], "quote_from_transcript": "모델 학습입니다", "kind": "주장",
     "claim": "아무 데도 없는 주장", "queries": ["nothing matches"], "keywords": ["zzz"]},
    {"id": "01-C005", "paragraph_ids": ["01-001"], "quote_from_transcript": "42dot 이희석입니다", "kind": "수치",
     "claim": "RLDX-1 의 SimplerEnv 점수는 81.5 다", "queries": ["RLDX-1 SimplerEnv"], "keywords": ["SimplerEnv"]},
]
PAGES = {
    "https://a.example/rldx": page("RLDX-1 achieves an average success rate of 97.8 on LIBERO, ahead of pi0.5."),
    "https://b.example/latency": page("With CUDA graphs the RLDX-1 latency dropped from 71.2ms to 43.7ms on an RTX 5090."),
    "https://c.example/robocasa": page("On RoboCasa Kitchen the model reaches 70.6 percent average success."),
    "https://d.example/simpler": page("SimplerEnv Google-VM results are reported for RLDX-1 in the main table."),
}
RESULTS = {"RLDX-1 LIBERO": ["https://a.example/rldx"], "RLDX-1 latency": ["https://b.example/latency"],
           "RoboCasa kitchen": ["https://c.example/robocasa"], "RLDX-1 SimplerEnv": ["https://d.example/simpler"]}
VERDICTS = {"verdicts": [
    {"claim_id": "01-C001", "verdict": "확인", "source_id": "S1",
     "quote": "RLDX-1 achieves an average success rate of 97.8 on LIBERO", "note": "보고서 수치와 같다",
     "confidence": "높음"},
    {"claim_id": "01-C002", "verdict": "다름", "source_id": "S1",
     "quote": "latency dropped from 71.2ms to 43.7ms", "note": "발언 40ms, 원문 43.7ms", "confidence": "높음"},
    {"claim_id": "01-C003", "verdict": "확인", "source_id": "S1",
     "quote": "RoboCasa is the first model above 70 percent", "note": "지어낸 인용", "confidence": "높음"},
    {"claim_id": "01-C005", "verdict": "확인", "source_id": "S1",
     "quote": "SimplerEnv Google-VM results are reported", "note": "수치 없음", "confidence": "중간"},
]}


def write_claims(ev, claims=CLAIMS):
    path = verify.claims_path(ev, ev.talks[0])
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump({"talk": "01", "claims": claims}, allow_unicode=True), encoding="utf-8")


def judge(messages):
    ids = [line.split()[1].rstrip("]") for line in user_of(messages).splitlines() if line.startswith("[주장 ")]
    return {"verdicts": [v for v in VERDICTS["verdicts"] if v["claim_id"] in ids]}


def test_status_category_mapping():
    assert verify.category("supported") == "확인"
    assert verify.category("conflicting") == "발언과 원문이 다른 것"
    assert verify.category("search_ok_zero") == "검색했으나 찾지 못한 것"
    for failure in ("search_failed", "fetch_failed", "extract_poor", "insufficient"):
        assert verify.category(failure) == "확인하지 않은 것"


def test_verify_pipeline_with_fake_search(built):
    ev, talk = built, built.talks[0]
    write_claims(ev)
    backend = FakeBackend(by_prompt(verify=judge))
    client = llm.LLM(backend, ev.work_dir)
    searcher = fake_searcher(ev.work_dir, FakeWeb(PAGES), RESULTS)
    data = verify.run_verify(ev, talk, client, searcher, progress=lambda m: None)
    by = {c["id"]: c for c in data["claims"]}
    assert by["01-C001"]["status"] == "supported" and by["01-C001"]["category"] == "확인"
    assert by["01-C001"]["source"]["url"] == "https://a.example/rldx"
    assert by["01-C002"]["status"] == "conflicting"
    assert by["01-C003"]["status"] == "insufficient" and "인용문 불일치" in by["01-C003"]["reason"]
    assert by["01-C003"]["category"] == "확인하지 않은 것"
    assert by["01-C004"]["status"] == "search_ok_zero" and "결과 0건" in by["01-C004"]["reason"]
    assert by["01-C005"]["status"] == "insufficient" and "81.5" in by["01-C005"]["reason"]
    assert data["counts"] == {"확인": 1, "발언과 원문이 다른 것": 1, "검색했으나 찾지 못한 것": 1, "확인하지 않은 것": 2}
    assert len(backend.calls) == 1                      # claim 4개를 한 번에(5개씩) 판정, 0건은 LLM 없이
    prompt = user_of(backend.calls[0])
    assert '<source id="S1" url="https://a.example/rldx"' in prompt and "</source>" in prompt
    assert "발표자: 이희석 (42dot)" in prompt and "대상: external(공개된 외부 사실)" in prompt
    assert "데이터" in llm.prompt("verify") and "따르지 않는다" in llm.prompt("verify")
    md = verify.verify_md_path(ev, talk).read_text(encoding="utf-8")
    for cat in verify.CATEGORIES:
        assert f"## {cat}" in md
    assert "| 01-C003 |" in md and "인용문 불일치" in md
    saved = json.loads(verify.verify_json_path(ev, talk).read_text(encoding="utf-8"))
    assert saved["claims"][0]["claim_hash"] and saved["claims"][2]["status"] == "insufficient"


def test_offline_everything_is_unverified_with_reason(built):
    ev, talk = built, built.talks[0]
    write_claims(ev)
    backend = FakeBackend(by_prompt(verify=judge))
    web = FakeWeb(PAGES, offline=True)
    data = verify.run_verify(ev, talk, llm.LLM(backend, ev.work_dir), fake_searcher(ev.work_dir, web, RESULTS),
                             progress=lambda m: None)
    assert backend.calls == []
    assert {c["status"] for c in data["claims"]} == {"search_failed"}
    assert all(c["category"] == "확인하지 않은 것" and "검색 실패" in c["reason"] for c in data["claims"])
    assert data["counts"]["검색했으나 찾지 못한 것"] == 0       # 실패가 "못찾음"으로 보이지 않는다
    # 네트워크가 돌아오면 실패한 것만 다시 한다. 확정 판정은 다시 검색하지 않는다
    web.offline = False
    s2 = fake_searcher(ev.work_dir, web, RESULTS)
    data = verify.run_verify(ev, talk, llm.LLM(backend, ev.work_dir), s2, progress=lambda m: None)
    assert data["counts"]["확인"] == 1
    s3 = fake_searcher(ev.work_dir, web, RESULTS)
    verify.run_verify(ev, talk, llm.LLM(backend, ev.work_dir), s3, progress=lambda m: None)
    assert "RLDX-1 LIBERO" not in s3.calls and "RoboCasa kitchen" in s3.calls


def test_fetch_and_extract_failures_are_not_not_found(built):
    ev, talk = built, built.talks[0]
    write_claims(ev, CLAIMS[:2])
    pages = {"https://a.example/rldx": "짧다"}             # 500자 미만 → extract_poor
    data = verify.run_verify(ev, talk, llm.LLM(FakeBackend(judge), ev.work_dir),
                             fake_searcher(ev.work_dir, FakeWeb(pages), RESULTS), progress=lambda m: None)
    by = {c["id"]: c for c in data["claims"]}
    assert by["01-C001"]["status"] == "extract_poor" and "500" in by["01-C001"]["reason"]
    assert by["01-C002"]["status"] == "fetch_failed" and "HTTP 404" in by["01-C002"]["reason"]
    assert data["counts"]["확인하지 않은 것"] == 2


def test_user_sources_come_first(built, tmp_path):
    ev, talk = built, built.talks[0]
    write_claims(ev, CLAIMS[:1])
    doc = tmp_path / "official.txt"
    doc.write_text(page("Official slide: RLDX-1 reaches 97.8 average success on LIBERO."), encoding="utf-8")
    assert main(["sources", str(ev.root), "--talk", "1", "add", str(doc), "--note", "발표자 공식 슬라이드"]) == 0
    short = tmp_path / "short.txt"
    short.write_text("너무 짧은 자료", encoding="utf-8")
    assert main(["sources", str(ev.root), "--talk", "1", "add", str(short)]) == 1
    assert [e["status"] for e in sources.load_manifest(ev.work_dir, "01")] == ["ok", "extract_poor"]
    seen = []

    def judge_user(messages):
        seen.append(user_of(messages))
        return {"verdicts": [{"claim_id": "01-C001", "verdict": "확인", "source_id": "S1",
                              "quote": "RLDX-1 reaches 97.8 average success on LIBERO", "note": "공식 슬라이드",
                              "confidence": "높음"}]}

    data = verify.run_verify(ev, talk, llm.LLM(FakeBackend(judge_user), ev.work_dir),
                             fake_searcher(ev.work_dir, FakeWeb(PAGES), RESULTS), progress=lambda m: None)
    first_source = seen[0].split("<source ", 1)[1]
    assert first_source.startswith('id="S1" url="(사용자 자료 U1) official.txt"')
    assert '<source id="S2" url="https://a.example/rldx"' in seen[0]
    assert data["claims"][0]["status"] == "supported" and data["claims"][0]["source"]["user"] is True
    assert data["user_sources"] == 1


def test_quote_fuzzy_match_returns_source_window():
    text = page("The latency dropped from 71.2ms to 43.7ms on an RTX 5090 GPU.")
    e = verify.Evidence("S1", "u", "t", sources.doc_hash(text), text, text, "", "text")
    assert verify.locate_quote("the latency dropped from 71.2ms to 43.7ms", e) == \
        "The latency dropped from 71.2ms to 43.7ms"
    # 한 글자 오타는 비슷한 원문 구간을 돌려준다(저장·표시는 원문 그대로)
    assert verify.locate_quote("The latency droped from 71.2ms to 43.7ms on an RTX 5090", e).startswith(
        "The latency dropped from 71.2ms")
    assert verify.locate_quote("The latency dropped from 90ms to 10ms on an H100 cluster", e) is None
    assert verify.missing_facts("2026년 5월 출시, 97.8", "released on May 7, 2026 with 97.8") == []
    assert verify.missing_facts("1,220대", "1220 units") == []
    assert verify.missing_facts("70% 를 넘겼다", "reaches 70.6 percent") == ["70"]


def test_verdict_id_set_must_match(built):
    ev, talk = built, built.talks[0]
    write_claims(ev, CLAIMS[:2])
    with pytest.raises(llm.ResponseError, match="빠짐 01-C002"):
        verify.validate_verdicts(json.dumps({"verdicts": [VERDICTS["verdicts"][0]]}), ["01-C001", "01-C002"])
    # manual: 응답의 claim id 집합이 다르면 다시 받으라고 한다(exit 3)
    web_pages = PAGES
    s = fake_searcher(ev.work_dir, FakeWeb(web_pages), RESULTS)
    client = llm.LLM(llm.ManualBackend(), ev.work_dir)
    with pytest.raises(llm.ManualPending):
        verify.run_verify(ev, talk, client, s, progress=lambda m: None)
    prompt = (ev.work_dir / "prompts" / "verify-01-1.md").read_text(encoding="utf-8")
    assert "request_id" in prompt and "<source id=\"S1\"" in prompt and "97.8" in prompt
    rid = prompt.split("`request_id: ")[1].split("`")[0]
    (ev.work_dir / "responses" / "verify-01-1.md").write_text(
        f"request_id: {rid}\n" + json.dumps({"verdicts": [VERDICTS["verdicts"][0]]}), encoding="utf-8")
    with pytest.raises(llm.ManualPending) as exc:
        verify.run_verify(ev, talk, client, fake_searcher(ev.work_dir, FakeWeb(web_pages), RESULTS),
                          progress=lambda m: None)
    assert "claim id 집합" in exc.value.items[0][1]
    (ev.work_dir / "responses" / "verify-01-1.md").write_text(
        f"request_id: {rid}\n" + json.dumps({"verdicts": VERDICTS["verdicts"][:2]}), encoding="utf-8")
    data = verify.run_verify(ev, talk, client, fake_searcher(ev.work_dir, FakeWeb(web_pages), RESULTS),
                             progress=lambda m: None)
    assert data["counts"]["확인"] == 1 and data["counts"]["발언과 원문이 다른 것"] == 1


def test_claims_command_checks_quotes_and_assigns_ids(built, monkeypatch, capsys):
    response = {"claims": [
        {"id": "c1", "paragraph_ids": ["01-001"], "quote_from_transcript": "42dot 이희석입니다", "claim": "발표자는 42dot 소속이다",
         "kind": "고유명사", "queries": ["42dot Lee Heeseok"], "keywords": ["42dot"]},
        {"id": "c2", "paragraph_ids": ["01-001"], "quote_from_transcript": "전사에 없는 지어낸 문장", "claim": "x",
         "kind": "수치", "queries": "q 하나만 문자열로"},
        {"id": "c5", "paragraph_ids": "01-001", "quote_from_transcript": "42dot", "claim": "짧은 인용", "kind": "수치",
         "queries": ["q"]},
        {"id": "c3", "paragraph_ids": ["01-777"], "quote_from_transcript": "42dot", "claim": "y", "kind": "수치",
         "queries": ["q"]},
        {"id": "c4", "paragraph_ids": ["01-003"], "quote_from_transcript": "모델 학습입니다", "claim": "z", "kind": "의견",
         "queries": ["q"]},
    ]}
    install(monkeypatch, FakeBackend(by_prompt(claims=response)))
    assert main(["claims", str(built.root), "1", "--chunk-chars", "100000"]) == 0
    out = capsys.readouterr().out
    assert "claim 1개" in out and "그대로 없다" in out and "묶음에 없다" in out and "kind" in out
    assert "너무 짧다" in out                                     # 8자 미만 전사 인용은 버린다
    data = yaml.safe_load(verify.claims_path(built, built.talks[0]).read_text(encoding="utf-8"))
    assert [c["id"] for c in data["claims"]] == ["01-C001"]
    assert data["claims"][0]["quote_from_transcript"] == "42dot 이희석입니다"
    assert data["claims"][0]["subject"] == "external"               # subject 가 없으면 claim 문장으로("발표자는 …"는 아님)


def test_claims_duplicate_ids_are_a_format_error():
    rows = {"claims": [{"id": "c1"}, {"id": "c1"}]}
    with pytest.raises(llm.ResponseError, match="중복"):
        verify._validate_claims(json.dumps(rows), {"01-001": "x"})


def test_ddg_empty_is_ambiguous_not_not_found(built, monkeypatch):
    ddgs = pytest.importorskip("ddgs")

    class Blocked:
        def __init__(self, timeout=None):
            pass

        def text(self, query, max_results=3):
            raise RuntimeError("No results found.")

    monkeypatch.setattr(ddgs, "DDGS", Blocked)
    s = sources.Sources(built.work_dir, providers=["ddg"], http=FakeWeb(), sleep=lambda x: None, env={})
    r = s.search("RLDX-1", "ddg")
    assert not r.ok and "구분" in r.error and "ddg" not in s.disabled
    g = verify.gather(CLAIMS[0], s, [])
    assert g.status == "search_failed" and verify.category(g.status) == "확인하지 않은 것"


def test_extract_handles_broken_pdf_and_html():
    kind, text, problem = sources.extract(b"%PDF-1.7 truncated garbage", "application/pdf", "x.pdf")
    assert kind == "pdf" and text == "" and problem
    kind, text, problem = sources.extract(b"<html><body><p>" + b"hello world. " * 60 + b"</p><script>x()</script>",
                                          "text/html", "https://e.org")
    assert kind == "html" and "hello world" in text and "x()" not in text and problem == ""


# 발표자 측 결과의 귀속 관문 (2026-11-06 flash 실측 2차: 발표자 연구진의 RoboCasa 70% 를 다른 팀 논문 23.6% 와 견줘 "다름") ---

OWN = {"id": "04-C006", "kind": "수치", "subject": "own", "paragraph_ids": ["04-047"],
       "claim": "발표자 연구진은 RoboCasa 시뮬레이션 벤치마크에서 70%의 성공률을 달성했다고 주장했다",
       "quote_from_transcript": "로보카사에서 칠십 퍼센트", "queries": ["RoboCasa"], "keywords": ["RoboCasa"]}
OTHER_TEAM = "LY-GWM reaches an average success rate of 23.6% on the RoboCasa benchmark across kitchen tasks."
OWN_TEAM = "Our RoboCasa policy reaches an average success rate of 70% on the benchmark across kitchen tasks."
WHO = ["신진우", "KAIST"]


def ev(sentence, title="LY-GWM: a generalist world model", user=False, text=None):
    text = text if text is not None else page(sentence)
    return verify.Evidence("S1", "https://arxiv.org/abs/2610.23274", title, sources.doc_hash(text), sentence, text,
                           "", "user" if user else "html", user=user)


def ruling(verdict, quote, evidence, claim=OWN, attribution=WHO, aliases=()):
    return verify.assess(claim, {"verdict": verdict, "source_id": "S1", "quote": quote}, [evidence], None,
                         attribution=attribution, aliases=aliases)


@pytest.mark.parametrize("verdict, sentence, key", [("다름", OTHER_TEAM, "23.6%"), ("확인", OWN_TEAM, "70%")])
def test_own_claim_against_unattributed_source_is_not_confirmed_or_conflicting(verdict, sentence, key):
    r = ruling(verdict, sentence, ev(sentence))
    assert r["status"] == "insufficient" and verify.category(r["status"]) == "확인하지 않은 것"
    assert r["reason"].startswith("원문이 발표자 측(신진우·KAIST) 자료로 확인되지 않는다 — 다른 팀의 결과일 수 있어")
    assert r["gate"] == "attribution" and r["quote"] == sentence          # 찾은 원문 구간은 남긴다
    shown = r["reason"].split("(원문값: ", 1)[1]
    assert key in shown and shown.endswith("…)")                          # 핵심 수치를 가운데 둔 발췌


def test_own_claim_with_speaker_side_source_keeps_verdict():
    assert ruling("확인", OWN_TEAM, ev("KAIST " + OWN_TEAM))["status"] == "supported"
    assert ruling("다름", OTHER_TEAM, ev(OTHER_TEAM + " Shin Jinwoo, kaist"))["status"] == "conflicting"
    assert ruling("다름", OTHER_TEAM, ev(OTHER_TEAM, title="KAIST robot lab"))["status"] == "conflicting"
    assert ruling("다름", OTHER_TEAM, ev(OTHER_TEAM), aliases=["LY-GWM"])["status"] == "conflicting"      # 별칭
    r = ruling("다름", OTHER_TEAM, ev(OTHER_TEAM, user=True))                                           # 사용자 자료
    assert r["status"] == "conflicting" and "gate" not in r


def test_attribution_ignores_references_and_late_comparisons():
    head = "LY-GWM: a generalist world model\nA. Kim, Seoul National University\n\n" + OTHER_TEAM
    refs = head + "\n\nReferences\n[1] J. Shin et al. KAIST robot policy. 2025.\n"
    assert ruling("다름", OTHER_TEAM, ev(OTHER_TEAM, text=refs))["status"] == "insufficient"
    assert ruling("다름", OTHER_TEAM, ev(OTHER_TEAM, text=refs.replace("References", "Notes")))["status"] == "conflicting"
    late = (head + "\n\n" + "Unrelated discussion of kitchen tasks and policies. " * 70
            + "We compare against the KAIST baseline.")
    assert len(late) > verify.ATTR_HEAD_CHARS
    assert ruling("다름", OTHER_TEAM, ev(OTHER_TEAM, text=late))["status"] == "insufficient"


def test_attribution_token_rules():
    # 영문 2자 표기("AI")는 aliases 에 적었을 때만 센다
    ai = ev(OTHER_TEAM, title="AI world model")
    assert ruling("다름", OTHER_TEAM, ai, attribution=["신진우", "AI"])["status"] == "insufficient"
    assert ruling("다름", OTHER_TEAM, ai, attribution=["신진우"], aliases=["AI"])["status"] == "conflicting"
    # 영문은 낱말 경계: "LG" 는 "algorithm" 에 맞지 않는다
    algorithm = ev(OTHER_TEAM + " The algorithm is new.")
    assert ruling("다름", OTHER_TEAM, algorithm, aliases=["LG"])["status"] == "insufficient"
    # 한글은 앞에 음절이 없고 뒤가 조사이거나 한글이 아닐 때만: 현대 ≠ 현대적, 신진우는 = 신진우
    for text, status in [("현대적 로봇 정책을 쓴다.", "insufficient"), ("현대는 로봇 정책을 쓴다.", "conflicting"),
                         ("현대 로보틱스가 만들었다.", "conflicting")]:
        assert ruling("다름", OTHER_TEAM, ev(OTHER_TEAM + " " + text), attribution=["현대"])["status"] == status, text
    assert ruling("다름", OTHER_TEAM, ev(OTHER_TEAM + " 신진우는 저자다."))["status"] == "conflicting"
    assert ruling("다름", OTHER_TEAM, ev(OTHER_TEAM + " 김신진우 저자."), attribution=["신진우"])["status"] == "insufficient"


def test_excerpt_centers_on_key_number():
    window = ("Several unrelated words come first in this long passage, " * 3 + "the policy reaches 23.6% on RoboCasa"
              + ", followed by more unrelated words that pad the passage out" * 3)
    shown = verify.excerpt(window, "발표자 연구진은 70% 를 달성했다")       # 주장의 수치가 없으면 원문의 첫 수치
    assert "23.6%" in shown and shown.startswith("…") and shown.endswith("…") and len(shown) <= 82
    assert "70%" in verify.excerpt(window.replace("23.6%", "70%"), "발표자 연구진은 70% 를 달성했다")
    assert verify.excerpt("짧은 원문 23.6%", "70%") == "짧은 원문 23.6%"


def test_external_claim_is_not_gated():
    claim = {**OWN, "subject": "external", "claim": "RoboCasa 벤치마크에서 LY-GWM 의 평균 성공률은 23.6% 다"}
    assert ruling("확인", OTHER_TEAM, ev(OTHER_TEAM), claim=claim)["status"] == "supported"
    assert ruling("다름", OTHER_TEAM, ev(OTHER_TEAM), claim=claim, attribution=[])["status"] == "conflicting"


def test_assess_requires_attribution_keyword():
    with pytest.raises(TypeError):
        verify.assess(OWN, {"verdict": "다름", "source_id": "S1", "quote": OTHER_TEAM}, [ev(OTHER_TEAM)], None)
    # 빈 목록이면 사용자 자료만 통과한다
    assert ruling("다름", OTHER_TEAM, ev(OTHER_TEAM), attribution=[])["status"] == "insufficient"


def test_subject_falls_back_to_claim_text():
    for text in ("발표자 연구진은 RoboCasa 에서 70% 를 달성했다", "저희 모델은 70% 다", "우리 팀은 3년 전에 시작했다",
                 "발표자의 회사는 로봇 1,000대를 팔았다"):
        assert verify.claim_subject({"claim": text}) == "own", text
    for text in ("OpenAI 연구진은 GPT-4 를 2023년에 공개했다", "발표자는 GPT-5 가 2025년 8월 출시됐다고 말했다",
                 "GPT-5 가 최고 모델이라는 것은 발표자의 해석이다", "GPT-5 는 2025년 8월 출시", "우리나라 로봇 시장은 3조 원이다"):
        assert verify.claim_subject({"claim": text}) == "external", text
    assert verify.claim_subject({"claim": "저희 모델은 지연이 40ms 다", "subject": "mine"}) == "own"
    assert verify.claim_subject({"claim": "저희 모델은 GPT-5 를 썼다", "subject": "external"}) == "external"
    rows = {"claims": [{"id": f"c{i}", "paragraph_ids": ["01-001"], "quote_from_transcript": "42dot 이희석입니다",
                        "claim": claim, "kind": "고유명사", "queries": ["q"], **extra}
                       for i, (claim, extra) in enumerate([("발표자 연구진은 42dot 소속이다", {}),
                                                           ("GPT-5 는 2025년 8월 출시", {}),
                                                           ("42dot 은 회사다", {"subject": "own"})])]}
    keep, dropped, _ = verify._validate_claims(json.dumps(rows, ensure_ascii=False), {"01-001": "안녕하세요 42dot 이희석입니다."})
    assert dropped == [] and [c["subject"] for c in keep] == ["own", "external", "own"]
    # subject 는 claim_hash 에 들어가지 않는다(판정 재사용은 judge_key 로 가른다)
    assert verify.claim_hash(keep[0]) == verify.claim_hash({k: v for k, v in keep[0].items() if k != "subject"})


OWN_CLAIM = {**CLAIMS[2], "claim": "발표자 연구진은 RoboCasa 에서 70% 를 넘긴 첫 모델을 만들었다"}
KITCHEN = "On RoboCasa Kitchen the model reaches 70.6 percent average success"


def judge_kitchen(seen):
    def judge_own(messages):
        seen.append(user_of(messages))
        return {"verdicts": [{"claim_id": "01-C003", "verdict": "다름", "source_id": "S1", "quote": KITCHEN,
                              "note": "발언 70%, 원문 70.6%", "confidence": "중간"}]}
    return judge_own


def test_verify_run_passes_speaker_and_aliases_to_gate(built):
    ev_, talk = built, built.talks[0]
    write_claims(ev_, [OWN_CLAIM])
    seen = []

    def run():
        return verify.run_verify(ev_, talk, llm.LLM(FakeBackend(judge_kitchen(seen)), ev_.work_dir),
                                 fake_searcher(ev_.work_dir, FakeWeb(PAGES), RESULTS), progress=lambda m: None)

    row = run()["claims"][0]
    assert "대상: own(발표자 측 결과)" in seen[0] and "발표자: 이희석 (42dot)" in seen[0]
    assert row["subject"] == "own" and row["status"] == "insufficient" and "발표자 측(이희석·42dot)" in row["reason"]
    assert row["gate"] == "attribution"
    md = verify.verify_md_path(ev_, talk).read_text(encoding="utf-8")
    assert f"원문 구간(귀속 미확인): {KITCHEN}" in md and "제시된 인용(검증 안 됨)" not in md
    talk.aliases = ["RoboCasa Kitchen"]                    # event.yaml 의 aliases 가 귀속 표기가 된다
    row = run()["claims"][0]
    assert "별칭 RoboCasa Kitchen" in seen[-1] and row["status"] == "conflicting" and "gate" not in row


def test_kept_verdict_is_redone_when_subject_or_aliases_change(built):
    ev_, talk = built, built.talks[0]
    talk.aliases = ["RoboCasa Kitchen"]
    write_claims(ev_, [OWN_CLAIM])

    def run():
        s = fake_searcher(ev_.work_dir, FakeWeb(PAGES), RESULTS)
        data = verify.run_verify(ev_, talk, llm.LLM(FakeBackend(judge_kitchen([])), ev_.work_dir), s,
                                 progress=lambda m: None)
        return data["claims"][0], s.calls

    row, calls = run()
    assert row["status"] == "conflicting" and calls                         # 확정 판정
    claims = verify.load_claims(ev_, talk)
    assert run()[1] == [] and verify.redo_claims(ev_, talk, claims) == []  # 그대로면 다시 하지 않는다
    talk.aliases = ["RoboCasa Kitchen", "Kitchen Lab"]                      # 별칭을 고치면 다시 판정
    assert verify.redo_claims(ev_, talk, claims) == claims
    row, calls = run()
    assert calls and row["status"] == "conflicting" and run()[1] == []
    write_claims(ev_, [{**OWN_CLAIM, "subject": "external"}])               # subject 를 고쳐도 다시 판정
    row, calls = run()
    assert calls and row["subject"] == "external"
    talk.aliases = []                                                       # external 은 별칭과 상관없다
    assert verify.redo_claims(ev_, talk, verify.load_claims(ev_, talk)) == []


def test_locate_quote_digit_at_paragraph_start():
    # 문단 첫머리에서 숫자로 시작하는 인용(before == "")은 잘린 수가 아니다
    assert verify.locate_quote_in("42dot 이희석입니다", "42dot 이희석입니다.") is not None
    assert verify.locate_quote_in("70%의 성공률", "70%의 성공률을 보였다") is not None
    # 앞에 숫자가 붙어 있으면 여전히 잘린 수다
    assert verify.locate_quote_in("70%의 성공률", "170%의 성공률을 보였다") is None
