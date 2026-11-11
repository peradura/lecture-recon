"""LLM·검색 가짜: 네트워크 없이 파이프라인을 끝까지 돌린다."""

from __future__ import annotations

import json

from lecture_recon import llm, sources


class FakeBackend:
    """메모리 안의 정해진 응답. responder(messages) → str | dict | list."""

    name = "fake"
    model = "fake-1"

    def __init__(self, responder):
        self.responder = responder
        self.calls: list[list[dict]] = []

    def complete(self, messages, json_mode=False):
        self.calls.append(messages)
        out = self.responder(messages)
        return out if isinstance(out, str) else json.dumps(out, ensure_ascii=False)


def system_of(messages) -> str:
    return messages[0]["content"]


def user_of(messages) -> str:
    return messages[1]["content"]


def by_prompt(**routes):
    """프롬프트 제목(`# lecture-recon: … (name)`)으로 응답을 고른다."""
    def responder(messages):
        head = system_of(messages).splitlines()[0]
        for name, value in routes.items():
            if f"({name})" in head:
                return value(messages) if callable(value) else value
        raise AssertionError(f"예상하지 않은 프롬프트: {head}")
    return responder


def install(monkeypatch, backend: FakeBackend):
    """CLI 가 만드는 LLM 을 가짜 백엔드로 바꾼다."""
    monkeypatch.setattr(llm, "make_llm", lambda settings, work_dir: llm.LLM(backend, work_dir, cache=settings.cache))


PAGE_FILLER = "This paragraph is unrelated filler about other topics and benchmarks. " * 12


def page(*sentences: str) -> str:
    return PAGE_FILLER + "\n\n" + "\n\n".join(sentences) + "\n\n" + PAGE_FILLER


class FakeWeb:
    """http_get 대용. pages: url → 본문. offline 이면 모든 요청이 NetworkError."""

    def __init__(self, pages: dict[str, str] | None = None, offline: bool = False):
        self.pages = pages or {}
        self.offline = offline
        self.requests: list[str] = []

    def __call__(self, url, headers=None, data=None):
        self.requests.append(url)
        if self.offline:
            raise sources.NetworkError("URLError: [Errno -3] Temporary failure in name resolution")
        if url in self.pages:
            return sources.HttpResponse(200, "text/plain; charset=utf-8", self.pages[url].encode("utf-8"), url)
        return sources.HttpResponse(404, "text/html", b"not found", url)


def fake_searcher(work_dir, web: FakeWeb, results: dict[str, list[str]]):
    """질의 → URL 목록. 목록에 없는 질의는 결과 0건."""
    calls: list[str] = []

    def provider(query, n):
        calls.append(query)
        if web.offline:
            raise sources.NetworkError("URLError: offline")
        return [sources.Hit("fake", f"Doc {u}", u, "") for u in results.get(query, [])][:n]

    s = sources.Sources(work_dir, providers=["fake"], http=web, sleep=lambda s: None, env={},
                        custom={"fake": provider})
    s.calls = calls
    return s
