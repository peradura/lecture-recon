"""설계 검토(2026-11-02 코드 리뷰) 지적 사항의 회귀 테스트: C1, H1–H5, M1–M6, LOW."""

import json

import pytest
import yaml

from fakes import FakeBackend, FakeWeb, fake_searcher, page
from lecture_recon import context, correct, llm, sources, verify
from lecture_recon.__main__ import main

FILL = "Unrelated filler sentence about other topics. " * 15


def evidence(sentence: str, sid: str = "S1") -> verify.Evidence:
    text = FILL + "\n\n" + sentence + "\n\n" + FILL
    return verify.Evidence(sid, "https://x.example", "t", sources.doc_hash(text), sentence, text, "", "html")


CLAIM = {"id": "04-C001", "claim": "RoboCasa 성공률이 70.5%다", "kind": "수치", "paragraph_ids": ["04-001"],
         "quote_from_transcript": "칠십 점 오 퍼센트", "queries": ["RoboCasa"], "keywords": ["RoboCasa"]}
SOURCE = "On the RoboCasa benchmark the policy reaches a success rate of 63.5% across all kitchen tasks."


# C1 -------------------------------------------------------------------------

@pytest.mark.parametrize("verdict, quote", [
    ("확인", SOURCE.replace("63.5", "70.5")),            # 숫자만 바꾼 근사 인용 → 통과하면 안 된다
    ("다름", SOURCE.replace("63.5", "80.5")),            # 지어낸 상충
    ("확인", SOURCE.replace("reaches", "does not reach")),  # 부정 표현 뒤집기(비율은 0.9 이상)
])
def test_c1_fuzzy_quote_with_altered_facts_is_downgraded(verdict, quote):
    r = verify.assess(CLAIM, {"verdict": verdict, "source_id": "S1", "quote": quote}, [evidence(SOURCE)], None,
                      attribution=[])
    assert r["status"] == "insufficient" and "인용문 불일치" in r["reason"]
    assert verify.category(r["status"]) == "확인하지 않은 것"


def test_c1_stored_quote_is_the_source_window_and_numbers_checked_on_it():
    claim = {**CLAIM, "claim": "RoboCasa 성공률이 63.5%다"}
    typo = SOURCE.replace("benchmark", "benchmrak")
    r = verify.assess(claim, {"verdict": "확인", "source_id": "S1", "quote": typo}, [evidence(SOURCE)], None,
                      attribution=[])
    assert r["status"] == "supported"
    assert r["quote"] == SOURCE and r["llm_quote"] == typo          # 저장·표시는 원문 그대로
    # 수치 검사는 LLM 인용이 아니라 원문 구간으로 한다
    claim70 = {**CLAIM, "claim": "RoboCasa 성공률이 70.5%다"}
    r = verify.assess(claim70, {"verdict": "확인", "source_id": "S1", "quote": SOURCE}, [evidence(SOURCE)], None,
                      attribution=[])
    assert r["status"] == "insufficient" and "70.5" in r["reason"]


# H1 -------------------------------------------------------------------------

T = """# t

- ASR: small, cpu, x · 교정: 0차 (초안)

<a id="segment-04-001"></a>
**[04-001] [00:00:05]** 지연은 3.5ms 이고 [불명확: "로보 카사"] 에서 안 쓴다 그리고 칠십 퍼센트다
"""


@pytest.mark.parametrize("old, new, what", [
    ('불명확: "로보 카사"', "RoboCasa", "`[불명확]` 표시"),   # '[' 를 old 밖에 두고 표시를 지우기
    (".5ms", "5ms", "숫자"),                               # old/new 숫자 목록은 같아도 본문 숫자가 바뀜
    ("3.", "3", "숫자"),
    ("안 쓴다", "안정적으로 쓴다", "부정어"),                 # '안정' 의 '안' 으로 부정어 제거를 가리기
    ("칠십", "사십", "한글 수사"),
    ("3.5ms", "3.5초", "단위"),
])
def test_h1_guards_compare_whole_paragraph(old, new, what):
    r = correct.apply_patch(T, {"replacements": [{"paragraph_id": "04-001", "old": old, "new": new,
                                                  "reason": "오인식"}]})
    assert r.applied == [] and what in r.rejected[0]["why"]
    assert r.text == T


def test_h1_safe_word_change_still_applies():
    r = correct.apply_patch(T, {"replacements": [{"paragraph_id": "04-001", "old": "그리고", "new": "그래서",
                                                  "reason": "오인식"}]})
    assert [a["level"] for a in r.applied] == ["일반"]


# M1 -------------------------------------------------------------------------

def test_m1_slide_evidence_must_exist_and_guess_is_marked_inline():
    rep = {"paragraph_id": "04-001", "old": "3.5ms", "new": "7.5ms", "reason": "숫자", "evidence": "슬라이드 99"}
    assert correct.apply_patch(T, {"replacements": [rep]}, slides={1, 2}).applied == []
    ok = correct.apply_patch(T, {"replacements": [{**rep, "evidence": "슬라이드 02: 7.5ms"}]}, slides={1, 2})
    assert ok.applied[0]["level"] == "추정" and "[추정: 7.5ms]" in ok.text
    refs = correct.Official(urls={"https://example.org/report"}, fetch=lambda url: "Table 3: latency 7.5ms on RTX")
    official = correct.apply_patch(T, {"replacements": [{**rep, "evidence": "공식 자료: https://example.org/report 표 3"}]},
                                   official=refs)
    assert "[추정: 7.5ms]" in official.text


# M5 / M3 (correct) ------------------------------------------------------------

@pytest.mark.parametrize("patch", [
    {"replacements": [{"paragraph_id": "04-001", "old": "그리고 ", "new": None, "reason": "x"}]},
    {"replacements": [{"paragraph_id": "04-001", "old": 3, "new": 4, "reason": "x"}]},
    {"replacements": ["문자열 항목"]},
    {"chapters": [{"paragraph_id": "04-001", "title": 5}]},
    {"note": "키가 없다"},
])
def test_m5_malformed_patch_is_a_format_error(patch):
    with pytest.raises(llm.ResponseError):
        correct.validate_patch(json.dumps(patch, ensure_ascii=False))


def test_m3_validate_patch_skips_footnote_lists():
    text = '참고 [1] 를 보라.\n{"chapters": [], "replacements": []}'
    assert correct.validate_patch(text) == {"chapters": [], "replacements": []}
    with pytest.raises(llm.ResponseError):
        correct.validate_patch('{"chapters": [{"paragraph_id": "04-001", "ti')       # 잘린 JSON


# M3 / M5 (verify·claims·slides) ----------------------------------------------

def test_m3_extract_json_prefers_object_with_expected_key():
    resp = 'Note [1]: see below.\n{"claims": [{"id": "c1"}]}'
    assert llm.extract_json(resp) == {"claims": [{"id": "c1"}]}
    assert llm.json_object(resp, "claims") == [{"id": "c1"}]
    assert llm.json_object('설명 {"other": 1} 그리고 [{"id": "c1"}]', "claims") == [{"id": "c1"}]


def test_m3_truncated_json_is_re_requested_and_never_cached(tmp_path):
    answers = iter(['{"claims": [{"id": "c1", "claim": "잘', '{"claims": []}'])
    backend = FakeBackend(lambda m: next(answers))
    client = llm.LLM(backend, tmp_path)
    assert client.ask("c", "s", "u", validate=lambda t: llm.json_object(t, "claims")) == []
    cached = list((tmp_path / "llm-cache").glob("*.json"))
    assert len(cached) == 1 and json.loads(cached[0].read_text())["response"] == '{"claims": []}'


def test_m5_non_dict_rows_are_format_errors():
    with pytest.raises(llm.ResponseError, match="객체가 아니다"):
        verify.validate_verdicts(json.dumps({"verdicts": [{"claim_id": "a", "verdict": "확인"}, "oops"]}), ["a"])
    with pytest.raises(llm.ResponseError, match="객체가 아니다"):
        verify._validate_claims(json.dumps({"claims": [{"id": "c1"}, 3]}), {"01-001": "x"})
    from lecture_recon import compose
    with pytest.raises(llm.ResponseError):
        compose.validate_slides(json.dumps({"slides": [{"photo": None}]}), ["IMG_1"])


# M4 -------------------------------------------------------------------------

def test_m4_string_list_fields_are_wrapped():
    rows = {"claims": [{"id": "c1", "paragraph_ids": "01-001", "quote_from_transcript": "42dot 이희석입니다",
                        "claim": "발표자는 42dot 소속이다", "kind": "고유명사", "queries": "42dot Lee",
                        "keywords": "42dot"}]}
    keep, dropped, _ = verify._validate_claims(json.dumps(rows, ensure_ascii=False),
                                            {"01-001": "안녕하세요 42dot 이희석입니다."})
    assert dropped == [] and keep[0]["paragraph_ids"] == ["01-001"] and keep[0]["queries"] == ["42dot Lee"]
    assert keep[0]["keywords"] == ["42dot"]


# H2 -------------------------------------------------------------------------

def test_h2_unicode_url_is_percent_encoded():
    assert sources.safe_url("https://ko.wikipedia.org/wiki/로봇") == \
        "https://ko.wikipedia.org/wiki/%EB%A1%9C%EB%B4%87"
    assert sources.safe_url("https://e.org/a%20b?q=로봇&x=1") == "https://e.org/a%20b?q=%EB%A1%9C%EB%B4%87&x=1"
    assert sources.safe_url("https://한국.kr/").startswith("https://xn--")


def test_h2_http_get_maps_odd_failures_to_network_error(monkeypatch):
    import http.client
    import urllib.request

    for exc in (ValueError("bad"), http.client.IncompleteRead(b"x"), UnicodeEncodeError("ascii", "가", 0, 1, "x")):
        def boom(*a, exc=exc, **k):
            raise exc
        monkeypatch.setattr(urllib.request, "urlopen", boom)
        with pytest.raises(sources.NetworkError):
            sources.http_get("https://e.org/x")


def test_h2_fetch_exception_and_non_json_body_do_not_crash(built, monkeypatch):
    ev = built
    claim = {"id": "01-C001", "claim": "RLDX-1 LIBERO 97.8", "kind": "수치", "paragraph_ids": ["01-001"],
             "quote_from_transcript": "42dot 이희석입니다", "queries": ["RLDX-1"], "keywords": ["RLDX-1"]}
    s = fake_searcher(ev.work_dir, FakeWeb({}), {"RLDX-1": ["https://a.example/x"]})
    monkeypatch.setattr(s, "fetch", lambda hit: (_ for _ in ()).throw(RuntimeError("boom")))
    g = verify.gather(claim, s, [])
    assert g.status == "fetch_failed" and "내부 오류" in g.reason
    web = FakeWeb({})
    web.pages = {}

    def s2_html(url, headers=None, data=None):
        return sources.HttpResponse(200, "text/html", b"<html>rate limited</html>", url)
    s2 = sources.Sources(ev.work_dir, providers=["semanticscholar"], http=s2_html, sleep=lambda x: None, env={})
    r = s2.search("RLDX-1", "semanticscholar")
    assert not r.ok and "JSON" in r.error


def test_h2_unexpected_error_for_one_claim_is_recorded(built, monkeypatch):
    ev, talk = built, built.talks[0]
    path = verify.claims_path(ev, talk)
    path.parent.mkdir(parents=True, exist_ok=True)
    claims = [{"id": f"01-C00{i}", "paragraph_ids": ["01-001"], "quote_from_transcript": "42dot 이희석입니다",
               "kind": "주장", "claim": f"주장 {i}", "queries": [f"q{i}"], "keywords": []} for i in (1, 2)]
    path.write_text(yaml.safe_dump({"claims": claims}, allow_unicode=True), encoding="utf-8")
    real = verify.gather

    def flaky(claim, *a, **k):
        if claim["id"] == "01-C001":
            raise KeyError("고장")
        return real(claim, *a, **k)
    monkeypatch.setattr(verify, "gather", flaky)
    data = verify.run_verify(ev, talk, llm.LLM(FakeBackend(lambda m: {}), ev.work_dir),
                             fake_searcher(ev.work_dir, FakeWeb({}), {}), progress=lambda m: None)
    by = {c["id"]: c for c in data["claims"]}
    assert by["01-C001"]["status"] == "internal_error" and "내부 오류" in by["01-C001"]["reason"]
    assert by["01-C001"]["category"] == "확인하지 않은 것" and by["01-C002"]["status"] == "search_ok_zero"


# H3 / H4 --------------------------------------------------------------------

class Stub:
    notes: list = []
    disabled: dict = {}
    http = None

    def __init__(self, ok_by_provider, hits=()):
        self.providers = list(ok_by_provider)
        self.ok = ok_by_provider
        self.hits = list(hits)

    def search(self, q, p):
        return sources.SearchResult(p, q, self.ok[p], list(self.hits) if self.ok[p] else [],
                                    "" if self.ok[p] else "HTTP 503")

    def fetch(self, h):
        raise AssertionError("fetch 하지 않는다")


def test_h3_zero_hits_with_partial_failure_is_not_not_found():
    g = verify.gather(CLAIM, Stub({"arxiv": False, "semanticscholar": True}), [])
    assert g.status == "search_failed" and "일부 실패" in g.reason
    assert g.status not in verify.FINAL_STATUSES
    g = verify.gather(CLAIM, Stub({"arxiv": True, "semanticscholar": True}), [])
    assert g.status == "search_ok_zero"


def test_h3_ambiguous_ddg_and_circuit_breaker_count_as_failures(built):
    s = sources.Sources(built.work_dir, providers=["semanticscholar"], http=FakeWeb(), sleep=lambda x: None, env={})
    s.disabled["semanticscholar"] = "연속 실패로 중단"
    g = verify.gather(CLAIM, s, [])
    assert g.status == "search_failed"


def test_h4_user_source_only_with_failed_search_and_none_verdict():
    udoc = sources.Doc("(사용자 자료 U1) a.txt", "a", "user", "ok", text=FILL + " RoboCasa robots " + FILL,
                       doc_hash="u1")
    g = verify.gather(CLAIM, Stub({"arxiv": False}), [udoc])
    assert len(g.evidence) == 1 and g.unclean[0] == "search_failed"
    r = verify.assess(CLAIM, {"verdict": "없음"}, g.evidence, g.unclean, attribution=[])
    assert r["status"] == "search_failed" and r["status"] not in verify.FINAL_STATUSES
    # gather 상태를 모르면(기본값) "없음"을 확정하지 않는다
    assert verify.assess(CLAIM, {"verdict": "없음"}, g.evidence, attribution=[])["status"] == "insufficient"
    assert verify.assess(CLAIM, {"verdict": "없음"}, g.evidence, None, attribution=[])["status"] == "search_ok_zero"


# H5 -------------------------------------------------------------------------

def test_h5_korean_common_words_do_not_gate_english_documents():
    claim = {"claim": "휴머노이드 로봇 시장은 2030년까지 크게 성장한다", "queries": ["humanoid robot market growth 2030"],
             "keywords": ["humanoid", "2030", "휴머노이드"]}
    doc = FILL + "\n\nThe humanoid robot market is projected to grow strongly through 2030.\n\n" + FILL
    assert not sources.is_name("성장한다") and not sources.is_name("휴머노이드")
    assert sources.relevant(doc, verify.claim_names(claim))
    # 한글 이름은 한국어 문서만 가른다
    ko_doc = "로봇 시장 전망 기사다. " * 30 + "알엘월드가 새 모델을 냈다."
    assert sources.relevant(ko_doc, ["알엘월드"]) and not sources.relevant("로봇 기사다. " * 50, ["알엘월드"])
    assert sources.relevant(doc, ["알엘월드"])          # 영어 문서는 한글 이름으로 거르지 않는다
    assert not sources.relevant(doc, ["RLDX-1"])        # 영문 이름은 가른다


# M2 -------------------------------------------------------------------------

def test_m2_month_names_need_a_day_or_year_and_identifiers_must_appear():
    assert verify.missing_facts("2026년 5월 출시", "May 7, 2026: released") == []
    assert verify.missing_facts("5월 출시", "you may see the release notes") == ["5"]
    assert verify.missing_facts("GPT-5 는 22.9% 다", "the model reaches 22.9% zero-shot") == ["GPT-5"]
    assert verify.missing_facts("RTX 5090 에서 43.7ms", "43.7 ms on an RTX 5090") == []
    assert verify.missing_facts("pi0.5 는 96.9", "π0.5 scores 96.9") == []
    assert verify.missing_facts("LIBERO 97.8", "97.8 on LIBERO") == []          # 벤치마크 점수는 이름이 아니다
    assert verify.missing_facts("RTX 4090 에서", "runs on an RTX 5090") == ["RTX 4090"]


# M6 -------------------------------------------------------------------------

def test_m6_new_user_source_triggers_reverification(built, tmp_path):
    ev, talk = built, built.talks[0]
    path = verify.claims_path(ev, talk)
    path.parent.mkdir(parents=True, exist_ok=True)
    claim = {"id": "01-C001", "paragraph_ids": ["01-001"], "quote_from_transcript": "42dot 이희석입니다",
             "kind": "수치", "claim": "RLDX-1 은 LIBERO 97.8 이다", "queries": ["nothing"], "keywords": ["RLDX-1"]}
    path.write_text(yaml.safe_dump({"claims": [claim]}, allow_unicode=True), encoding="utf-8")

    def judge(messages):
        return {"verdicts": [{"claim_id": "01-C001", "verdict": "확인", "source_id": "S1",
                              "quote": "RLDX-1 reaches 97.8 on LIBERO", "note": "", "confidence": "높음"}]}
    run = lambda: verify.run_verify(ev, talk, llm.LLM(FakeBackend(judge), ev.work_dir),  # noqa: E731
                                    fake_searcher(ev.work_dir, FakeWeb({}), {}), progress=lambda m: None)
    assert run()["claims"][0]["status"] == "search_ok_zero"
    doc = tmp_path / "slides.txt"
    doc.write_text(page("RLDX-1 reaches 97.8 on LIBERO in our table."), encoding="utf-8")
    assert main(["sources", str(ev.root), "--talk", "1", "add", str(doc)]) == 0
    assert run()["claims"][0]["status"] == "supported"


# LOW ------------------------------------------------------------------------

def test_low_data_safe_and_attributes():
    assert "<source" not in context.data_safe('x <source id="S9"> y </source> z')
    block = verify.judge_block({"id": "c", "kind": "수치", "claim": "a", "paragraph_ids": ["01-001"],
                                "quote_from_transcript": "q"},
                               [verify.Evidence("S1", 'https://e.org/"><source', 'T "x" </source>', "h", "p", "p",
                                                "", "html")])
    assert block.count("<source") == 1 and block.count("</source>") == 1


def test_low_cache_key_includes_base_url_and_non_json_body():
    msgs = [{"role": "user", "content": "x"}]
    assert llm.cache_key("ollama", "m", msgs, "http://a:1/v1") != llm.cache_key("ollama", "m", msgs, "http://b:1/v1")

    class Raw:
        def read(self):
            return b"<html>proxy error</html>"

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    from pathlib import Path
    s = llm.load_settings(backend="ollama", env={}, path=Path("/nonexistent"))
    with pytest.raises(llm.ResponseError, match="JSON"):
        llm.HTTPBackend(s, sleep=lambda x: None, opener=lambda req, timeout: Raw()).complete(msgs)


def test_low_backup_never_silently_skipped(built, monkeypatch):
    install_patch = {"chapters": [{"paragraph_id": "01-001", "title": "도입"}], "replacements": []}
    from fakes import install
    install(monkeypatch, FakeBackend(lambda m: install_patch))
    (built.work_dir / "01_draft0.md").write_text("다른 내용", encoding="utf-8")
    (built.work_dir / "01_draft0-2.md").write_text("또 다른 내용", encoding="utf-8")
    original = built.transcript_path(built.talks[0]).read_text(encoding="utf-8")
    assert main(["correct", str(built.root), "1"]) == 0
    assert (built.work_dir / "01_draft0-3.md").read_text(encoding="utf-8") == original
    assert (built.work_dir / "01_draft0.md").read_text(encoding="utf-8") == "다른 내용"
