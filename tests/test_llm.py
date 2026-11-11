import io
import json
import re
import urllib.error
from pathlib import Path

import pytest

from fakes import FakeBackend
from lecture_recon import llm
from lecture_recon.__main__ import main

SKILL = Path(__file__).resolve().parents[1] / "skill" / "lecture-recon" / "SKILL.md"


def test_extract_json_variants():
    assert llm.extract_json('{"a": 1}') == {"a": 1}
    assert llm.extract_json('설명입니다.\n```json\n{"claims": [{"id": "c1"}]}\n```\n끝') == {"claims": [{"id": "c1"}]}
    assert llm.extract_json('앞말 {"t": "중괄호 } 포함"} 뒷말 {"x": 2}') == {"t": "중괄호 } 포함"}
    assert llm.extract_json("<think>{생각}</think>\n[1, 2]") == [1, 2]
    assert llm.extract_json('request_id 없이 {"verdicts": []}') == {"verdicts": []}
    with pytest.raises(llm.ResponseError, match="JSON"):
        llm.extract_json('{"claims": [{"id": "c1", "claim": "잘린')
    assert llm.json_object('[{"a": 1}]', "claims") == [{"a": 1}]
    with pytest.raises(llm.ResponseError):
        llm.json_object('{"other": []}', "claims")


def test_cache_keyed_by_backend_model_messages(tmp_path):
    backend = FakeBackend(lambda m: {"ok": m[1]["content"]})
    client = llm.LLM(backend, tmp_path)
    a = client.ask("x", "sys", "u1", validate=llm.extract_json)
    b = client.ask("x", "sys", "u1", validate=llm.extract_json)
    assert a == b == {"ok": "u1"} and len(backend.calls) == 1 and client.cache_hits == 1
    key = llm.cache_key("fake", "fake-1", [{"role": "system", "content": "sys"}, {"role": "user", "content": "u1"}])
    assert (tmp_path / "llm-cache" / f"{key}.json").exists()
    client.ask("y", "sys", "u2")                      # 입력이 다르면 새 호출
    assert len(backend.calls) == 2
    backend.model = "fake-2"                           # 모델이 다르면 새 키
    client.ask("x", "sys", "u1")
    assert len(backend.calls) == 3
    assert llm.cache_key("a", "m", []) != llm.cache_key("b", "m", [])
    nocache = llm.LLM(backend, tmp_path, cache=False)
    nocache.ask("x", "sys", "u1")
    assert len(backend.calls) == 4


def test_invalid_response_gets_one_repair_and_is_not_cached(tmp_path):
    answers = iter(["형식 없음", '{"claims": []}'])
    backend = FakeBackend(lambda m: next(answers))
    client = llm.LLM(backend, tmp_path)
    assert client.ask("c", "s", "u", validate=lambda t: llm.json_object(t, "claims")) == []
    assert len(backend.calls) == 2 and "응답 형식 오류" in backend.calls[1][-1]["content"]
    bad = llm.LLM(FakeBackend(lambda m: "계속 틀림"), tmp_path / "b")
    with pytest.raises(llm.ResponseError, match="재요청 후"):
        bad.ask("c", "s", "u", validate=llm.extract_json)
    assert not (tmp_path / "b" / "llm-cache").exists()


def test_manual_round_trip_and_request_id_mismatch(tmp_path):
    client = llm.LLM(llm.ManualBackend(), tmp_path)
    with pytest.raises(llm.ManualPending) as exc:
        client.ask("claims-01-1", "지시문", "입력 데이터", validate=llm.extract_json)
    assert exc.value.exit_code == 3 and "work/responses/claims-01-1.md" in exc.value.render()
    prompt = (tmp_path / "prompts" / "claims-01-1.md").read_text(encoding="utf-8")
    rid = re.search(r"`request_id: ([0-9a-f]{12})`", prompt).group(1)
    assert "지시문" in prompt and "입력 데이터" in prompt
    resp = tmp_path / "responses" / "claims-01-1.md"
    resp.write_text(f"request_id: {rid}\n```json\n{{\"claims\": []}}\n```\n", encoding="utf-8")
    assert client.ask("claims-01-1", "지시문", "입력 데이터", validate=llm.extract_json) == {"claims": []}
    # JSON 안의 request_id 도 받는다
    resp.write_text(f'{{"request_id": "{rid}", "claims": []}}', encoding="utf-8")
    assert client.ask("claims-01-1", "지시문", "입력 데이터", validate=llm.extract_json)["claims"] == []
    # 입력이 바뀌면 같은 이름이라도 request_id 가 달라진다 → 다시 붙여넣으라고 한다
    with pytest.raises(llm.ManualPending) as exc:
        client.ask("claims-01-1", "지시문", "바뀐 입력", validate=llm.extract_json)
    assert "불일치" in exc.value.items[0][1]
    resp.write_text('{"claims": []}', encoding="utf-8")
    with pytest.raises(llm.ManualPending, match="request_id 가 없다"):
        client.ask("claims-01-1", "지시문", "입력 데이터", validate=llm.extract_json)
    resp.write_text(f"request_id: {rid}\n{{\"claims\": [", encoding="utf-8")
    with pytest.raises(llm.ManualPending, match="형식 오류"):
        client.ask("claims-01-1", "지시문", "입력 데이터", validate=llm.extract_json)


def test_settings_precedence(tmp_path):
    cfg = tmp_path / "config.yaml"
    cfg.write_text("backend: gemini\nmodel: gemini-x\nrpm: 10\nsearch: [arxiv, ddg]\n", encoding="utf-8")
    s = llm.load_settings(env={"GEMINI_API_KEY": "k1"}, path=cfg)
    assert (s.backend, s.model, s.rpm, s.api_key, s.search, s.origin) == ("gemini", "gemini-x", 10, "k1",
                                                                          ["arxiv", "ddg"], "설정 파일")
    assert s.base_url == llm.PRESETS["gemini"].base_url
    s = llm.load_settings(env={"LECTURE_RECON_BACKEND": "groq", "GROQ_API_KEY": "g"}, path=cfg)
    assert (s.backend, s.model, s.api_key, s.origin) == ("groq", "llama-3.3-70b-versatile", "g", "환경변수")
    s = llm.load_settings(backend="ollama", model="qwen3:4b", rpm=0, no_cache=True,
                          env={"LECTURE_RECON_BACKEND": "groq", "LECTURE_RECON_BASE_URL": "http://h:1/v1/"}, path=cfg)
    assert (s.backend, s.model, s.base_url, s.rpm, s.cache) == ("ollama", "qwen3:4b", "http://h:1/v1", 0, False)
    assert llm.load_settings(env={}, path=tmp_path / "none.yaml").backend == "claude-code"
    with pytest.raises(llm.LectureReconError, match="backend"):
        llm.load_settings(backend="gpt", env={}, path=cfg)


def test_claude_code_default_refuses_with_free_alternatives(event_dir, capsys):
    assert main(["claims", str(event_dir), "1"]) == 2
    err = capsys.readouterr().err
    for needle in ("claude-code", "--backend manual", "--backend gemini", "GEMINI_API_KEY", "--backend ollama",
                   str(llm.config_path())):
        assert needle in err


def test_missing_api_key_is_clear(event_dir, capsys):
    assert main(["claims", str(event_dir), "1", "--backend", "gemini"]) == 2
    assert "GEMINI_API_KEY" in capsys.readouterr().err


class Resp:
    def __init__(self, payload):
        self.payload = payload

    def read(self):
        return json.dumps(self.payload).encode("utf-8")

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def http_error(code, retry_after=None):
    headers = {"Retry-After": retry_after} if retry_after else {}
    return urllib.error.HTTPError("http://x", code, "err", headers, io.BytesIO(b'{"error": "quota"}'))


def test_http_backend_retries_429_and_drops_json_mode_on_400():
    script = [http_error(429, "3"), http_error(503), http_error(400),
              Resp({"choices": [{"message": {"content": "<think>음</think>{\"ok\": true}"}}]})]
    sent, slept = [], []

    def opener(req, timeout):
        sent.append(json.loads(req.data))
        item = script.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    s = llm.load_settings(backend="ollama", env={}, path=Path("/nonexistent"))
    backend = llm.HTTPBackend(s, sleep=slept.append, opener=opener)
    assert backend.complete([{"role": "user", "content": "hi"}], json_mode=True) == '{"ok": true}'
    assert slept == [3.0, 4.0]                      # Retry-After, 그다음 지수 백오프
    assert "response_format" in sent[2] and "response_format" not in sent[3]
    assert sent[0]["model"] == "qwen3:8b"


def test_http_backend_gives_up_after_five_retries():
    slept = []

    def opener(req, timeout):
        raise http_error(429)

    s = llm.load_settings(backend="ollama", env={}, path=Path("/nonexistent"))
    with pytest.raises(llm.LLMError, match="HTTP 429"):
        llm.HTTPBackend(s, sleep=slept.append, opener=opener).complete([{"role": "user", "content": "x"}])
    assert slept == [2.0, 4.0, 8.0, 16.0, 32.0]


def test_rpm_throttle_spaces_calls():
    slept = []
    s = llm.load_settings(backend="ollama", rpm=30, env={}, path=Path("/nonexistent"))
    llm.HTTPBackend._last_call.clear()
    b = llm.HTTPBackend(s, sleep=slept.append,
                        opener=lambda req, timeout: Resp({"choices": [{"message": {"content": "a"}}]}))
    b.complete([{"role": "user", "content": "1"}])
    b.complete([{"role": "user", "content": "2"}])
    assert len(slept) == 1 and 1.9 < slept[0] <= 2.0


def test_prompts_embed_skill_principles_verbatim():
    principles = [line for line in SKILL.read_text(encoding="utf-8").splitlines() if re.match(r"^[1-7]\. ", line)]
    assert len(principles) == 7
    for name in ("correct", "claims", "verify", "notes", "report", "slides"):
        text = llm.prompt(name)
        for line in principles:
            assert line in text, (name, line)
