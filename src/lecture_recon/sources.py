"""키 없는 검색(arXiv · Semantic Scholar · DuckDuckGo) + 선택(Tavily · Brave), 본문 취득·추출·캐시, 관련 단락 추출.

네트워크 실패는 예외로 새지 않는다: SearchResult.ok=False / Doc.status="fetch_failed" 로 돌려주고,
verify 가 그 claim 을 "확인하지 않은 것"(사유 포함)으로 분류한다.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import html
import http.client
import importlib.util
import json
import os
import re
import shutil
import socket
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Callable

from .config import LectureReconError

USER_AGENT = "lecture-recon/0.1 (lecture notes fact-check)"
MAX_BYTES = 25 * 1024 * 1024
MIN_TEXT_CHARS = 500
MAX_NONPRINTABLE = 0.2
PASSAGE_CHARS = 2000
WIDE_PASSAGE_CHARS = 6000
INTERVAL = {"arxiv": 3.0, "semanticscholar": 1.1, "ddg": 1.0, "tavily": 0.5, "brave": 1.1}
KEY_ENVS = {"tavily": "TAVILY_API_KEY", "brave": "BRAVE_API_KEY"}
SEARCH_VERSION = "2"  # 질의 구성 규칙이 바뀌면 올린다(검색 캐시 키에 들어감)
S2_KEY_ENVS = ("SEMANTIC_SCHOLAR_API_KEY", "S2_API_KEY")
ARXIV_ID_RE = re.compile(r"arxiv\.org/(?:abs|pdf|html)/([0-9]{4}\.[0-9]{4,5}|[a-z\-]+/[0-9]{7})(v\d+)?", re.I)


class NetworkError(Exception):
    """연결 실패·타임아웃(HTTP 상태 코드가 없는 실패)."""


class AmbiguousEmpty(LectureReconError):
    """결과 0건인지 차단·일시 오류인지 구분할 수 없다(ddg). 성공한 0건으로 세지 않는다."""


@dataclass
class HttpResponse:
    status: int
    content_type: str
    body: bytes
    url: str
    headers: dict = field(default_factory=dict)


def safe_url(url: str) -> str:
    """한글 등 비 ASCII 경로·질의를 퍼센트 인코딩하고 호스트는 IDNA 로. 이미 인코딩된 %XX 는 둔다."""
    try:
        parts = urllib.parse.urlsplit(url.strip())
        host = parts.hostname.encode("idna").decode("ascii") if parts.hostname else ""
    except (ValueError, UnicodeError) as e:
        raise NetworkError(f"잘못된 URL: {e}") from None
    netloc = host + (f":{parts.port}" if parts.port else "")
    if parts.username:
        netloc = f"{parts.username}@{netloc}"
    path = urllib.parse.quote(parts.path, safe="/%:@!$&'()*+,;=-._~")
    query = urllib.parse.quote(parts.query, safe="=&%+/?:@,;!$'()*-._~")
    return urllib.parse.urlunsplit((parts.scheme, netloc, path, query, ""))


def http_get(url: str, headers: dict | None = None, timeout: float = 30.0, data: bytes | None = None) -> HttpResponse:
    """GET(또는 data 가 있으면 POST). HTTP 오류는 상태 코드로 돌려주고, 나머지 실패는 모두 NetworkError."""
    hdrs = {"User-Agent": USER_AGENT, "Accept": "*/*"}
    hdrs.update(headers or {})
    try:
        req = urllib.request.Request(safe_url(url), headers=hdrs, data=data, method="POST" if data is not None else "GET")
        with urllib.request.urlopen(req, timeout=timeout) as r:
            body = r.read(MAX_BYTES + 1)
            return HttpResponse(r.status, r.headers.get("Content-Type", ""), body[:MAX_BYTES], r.geturl(), dict(r.headers))
    except urllib.error.HTTPError as e:
        try:
            body = e.read()[:20000] if e.fp else b""
        except (OSError, http.client.HTTPException):
            body = b""
        return HttpResponse(e.code, e.headers.get("Content-Type", "") if e.headers else "", body, url,
                            dict(e.headers) if e.headers else {})
    except (urllib.error.URLError, socket.timeout, TimeoutError, ConnectionError, OSError,
            http.client.HTTPException, ValueError, UnicodeError) as e:
        raise NetworkError(f"{type(e).__name__}: {getattr(e, 'reason', e)}") from None


def _json_body(resp: HttpResponse, provider: str) -> dict:
    try:
        data = json.loads(resp.body.decode("utf-8", "replace"))
    except ValueError:
        raise LectureReconError(f"{provider}: 응답이 JSON 이 아니다(HTTP {resp.status})") from None
    if not isinstance(data, dict):
        raise LectureReconError(f"{provider}: 응답 JSON 이 객체가 아니다")
    return data


# 데이터 ----------------------------------------------------------------------

@dataclass
class Hit:
    provider: str
    title: str
    url: str
    snippet: str = ""
    arxiv_id: str = ""
    pdf_url: str = ""
    abstract: str = ""


@dataclass
class SearchResult:
    provider: str
    query: str
    ok: bool
    hits: list[Hit] = field(default_factory=list)
    error: str = ""

    def log(self) -> dict:
        return {"provider": self.provider, "query": self.query, "ok": self.ok, "n": len(self.hits), "error": self.error}


@dataclass
class Doc:
    url: str
    title: str
    kind: str              # html | pdf | text | abstract | user
    status: str            # ok | fetch_failed | extract_poor
    reason: str = ""
    text: str = ""
    doc_hash: str = ""
    path: str = ""         # 캐시된 본문 파일(행사 디렉터리 기준)

    @property
    def ok(self) -> bool:
        return self.status == "ok"


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def doc_hash(text: str) -> str:
    return _sha(text)[:12]


# 추출 ------------------------------------------------------------------------

TAG_DROP_RE = re.compile(r"<(script|style|noscript|svg|head|nav|footer)\b.*?</\1\s*>", re.S | re.I)
TAG_BREAK_RE = re.compile(r"<\s*(br|/p|/div|/li|/h[1-6]|/tr|/section|/article)\b[^>]*>", re.I)
TAG_RE = re.compile(r"<[^>]+>")


def strip_tags(markup: str) -> str:
    text = TAG_DROP_RE.sub(" ", markup)
    text = TAG_BREAK_RE.sub("\n", text)
    text = html.unescape(TAG_RE.sub(" ", text))
    text = re.sub(r"[ \t\r\f\v]+", " ", text)
    return re.sub(r"\n\s*\n+", "\n\n", text).strip()


def html_text(markup: str) -> str:
    if importlib.util.find_spec("trafilatura") is not None:
        import trafilatura

        try:
            out = trafilatura.extract(markup, include_tables=True, include_comments=False, favor_recall=True)
        except Exception:  # 깨진 HTML 은 단순 태그 제거로
            out = None
        if out and len(out) >= MIN_TEXT_CHARS:
            return out
    return strip_tags(markup)


def pdf_text(data: bytes) -> str | None:
    """PDF 텍스트 층. pymupdf 가 없으면 None."""
    if importlib.util.find_spec("pymupdf") is None:
        return None
    import pymupdf

    with pymupdf.open(stream=data, filetype="pdf") as doc:
        return "\n\n".join(page.get_text() for page in doc).strip()


def quality(text: str) -> str:
    """추출 품질 문제(없으면 빈 문자열)."""
    if len(text) < MIN_TEXT_CHARS:
        return f"추출 텍스트 {len(text)}자(<{MIN_TEXT_CHARS})"
    bad = sum(1 for ch in text if ch == "�" or (not ch.isprintable() and ch not in "\n\t\r"))
    if bad / len(text) > MAX_NONPRINTABLE:
        return f"깨진 문자 비율 {bad / len(text):.0%}(>{MAX_NONPRINTABLE:.0%})"
    return ""


def _decode(body: bytes, content_type: str) -> str:
    m = re.search(r"charset=([\w-]+)", content_type or "", re.I)
    try:
        return body.decode(m.group(1) if m else "utf-8", errors="replace")
    except LookupError:
        return body.decode("utf-8", errors="replace")


def extract(body: bytes, content_type: str, url: str) -> tuple[str, str, str]:
    """(kind, text, 문제). 문제가 있으면 extract_poor 다."""
    ctype = (content_type or "").lower()
    if body[:5] == b"%PDF-" or "pdf" in ctype or url.lower().split("?")[0].endswith(".pdf"):
        try:
            text = pdf_text(body)
        except Exception as e:  # 잘린·깨진 PDF
            return "pdf", "", f"PDF 해석 실패: {type(e).__name__}"
        if text is None:
            return "pdf", "", 'pymupdf 없음 — uv pip install -e ".[search]"'
        if not text.strip():
            return "pdf", "", "PDF 텍스트 층 없음(스캔본)"
        return "pdf", text, quality(text)
    raw = _decode(body, ctype)
    if "html" in ctype or re.search(r"<html|<body|<div|<p[ >]", raw[:5000], re.I):
        text = html_text(raw)
        return "html", text, quality(text)
    return "text", raw.strip(), quality(raw.strip())


# 관련 단락 -------------------------------------------------------------------

STOPWORDS = {
    "the", "and", "for", "with", "from", "that", "this", "are", "was", "were", "has", "have", "its", "into", "than",
    "about", "over", "under", "using", "based", "model", "models", "paper", "report", "new", "via", "our", "their",
    "발표자", "발표", "주장", "있다", "한다", "했다", "이다", "것이", "것을", "그리고", "하지만", "정도", "우리",
}
JOSA_RE = re.compile(r"(은|는|이|가|을|를|의|에|에서|에게|로|으로|와|과|도|만|이다|였다|이며|라는|라고)$")
TOKEN_RE = re.compile(r"[A-Za-z][A-Za-z0-9.+\-]*[A-Za-z0-9]|\d+(?:[.,]\d+)*%?|[가-힣]{2,}")
NUMBER_RE = re.compile(r"\d+(?:\.\d+)?")


def keywords(*texts: str, extra: list[str] | None = None) -> list[str]:
    out: list[str] = []
    for kw in extra or []:
        kw = str(kw).strip()
        if kw and kw.lower() not in (k.lower() for k in out):
            out.append(kw)
    for text in texts:
        for tok in TOKEN_RE.findall(text or ""):
            if re.match(r"[가-힣]", tok):
                tok = JOSA_RE.sub("", tok)
                if len(tok) < 2:
                    continue
            elif not any(c.isdigit() for c in tok) and len(tok) < 3:
                continue
            tok = tok.rstrip(".,")
            if tok.lower() in STOPWORDS or tok.lower() in (k.lower() for k in out):
                continue
            out.append(tok)
    return out[:24]


def _units(text: str) -> list[str]:
    units = []
    for block in re.split(r"\n\s*\n", text):
        block = re.sub(r"\s+", " ", block).strip()
        if not block:
            continue
        while len(block) > 600:
            cut = max(block.rfind(". ", 0, 500), block.rfind("다. ", 0, 500))
            cut = cut + 2 if cut > 100 else 500
            units.append(block[:cut].strip())
            block = block[cut:].strip()
        if block:
            units.append(block)
    return units


def _score(unit: str, kws: list[str]) -> float:
    low = unit.lower()
    score = 0.0
    for kw in kws:
        if kw.lower() in low:
            score += 2.0 if any(c.isdigit() for c in kw) else 1.0
    return score


def relevant_passage(text: str, kws: list[str], window: int = PASSAGE_CHARS) -> tuple[str, float]:
    """키워드 겹침이 큰 단락을 window 자 이내로 문서 순서대로 모은다. wide(window>2000)면 이웃 단락도 붙인다."""
    units = _units(text)
    scored = [(_score(u, kws), i) for i, u in enumerate(units)]
    ranked = sorted((s for s in scored if s[0] > 0), key=lambda s: (-s[0], s[1]))
    if not ranked:
        return "", 0.0
    chosen: set[int] = set()
    total = 0
    order = [i for _, i in ranked]
    if window > PASSAGE_CHARS:
        order = [j for i in order for j in (i, i - 1, i + 1) if 0 <= j < len(units)]
    for i in order:
        if i in chosen:
            continue
        if total + len(units[i]) > window:
            if not chosen:
                chosen.add(i)
                total = window
            continue
        chosen.add(i)
        total += len(units[i]) + 3
    parts, prev = [], None
    for i in sorted(chosen):
        if prev is not None and i != prev + 1:
            parts.append("…")
        parts.append(units[i])
        prev = i
    passage = "\n".join(parts)
    return passage[:window], ranked[0][0]


HYPHEN_BREAK_RE = re.compile(r"-[ \t]*\n\s*(?=\w)")
QUOTE_MARKS = str.maketrans({"“": '"', "”": '"', "‘": "'", "’": "'", "π": "pi"})


def normalize_map(text: str) -> tuple[str, list[int]]:
    """대조용 정규화(NFKC·소문자·공백 하나·PDF 줄끝 하이픈 제거)와, 정규화 글자마다 원문 위치."""
    text = text or ""
    out: list[str] = []
    idx: list[int] = []
    space = False
    i = 0
    while i < len(text):
        ch = text[i]
        if ch == "-" and out and out[-1].isalnum():
            m = HYPHEN_BREAK_RE.match(text, i)
            if m:
                i = m.end()
                continue
        piece = unicodedata.normalize("NFKC", ch).translate(QUOTE_MARKS).lower()
        if not piece.strip():
            space = True
            i += 1
            continue
        if space and out:
            out.append(" ")
            idx.append(i - 1)
        space = False
        for c in piece:
            out.append(c)
            idx.append(i)
        i += 1
    return "".join(out), idx


def normalize(text: str) -> str:
    return normalize_map(text)[0]


# 검색 ------------------------------------------------------------------------

HANGUL_RE = re.compile(r"[가-힣]")


def is_name(kw: str) -> bool:
    """영문 고유명사·모델명 모양(질의어 고르기용): RLDX-1, RoboCasa, LIBERO, Qwen3. 한글 단어는 아니다."""
    kw = kw.strip()
    if HANGUL_RE.search(kw):
        return False
    has_letter = any(c.isalpha() for c in kw)
    has_digit = any(c.isdigit() for c in kw)
    inner_upper = any(c.isupper() for c in kw[1:])
    return has_letter and (has_digit or (len(kw) >= 4 and inner_upper))


def entity_names(keywords: list[str]) -> list[str]:
    """claims 단계가 준 keywords 중 문서 관련성을 가를 이름: 영문은 대문자·숫자가 섞인 것, 한글은 2자 이상."""
    out = []
    for kw in keywords or []:
        kw = str(kw).strip()
        if HANGUL_RE.search(kw):
            if len(kw) >= 2:
                out.append(kw)
        elif any(c.isalpha() for c in kw) and (any(c.isupper() for c in kw) or any(c.isdigit() for c in kw)) \
                and len(kw) >= 3:
            out.append(kw)
    return out


def is_korean(text: str) -> bool:
    sample = text[:20000]
    letters = sum(c.isalpha() for c in sample)
    return letters > 0 and len(HANGUL_RE.findall(sample)) / letters > 0.3


def relevant(text: str, names: list[str]) -> bool:
    """이름(entity_names)이 있으면 그중 하나는 문서에 있어야 한다. 한글 이름은 한국어 문서만 가른다."""
    korean = is_korean(text)
    gate = [n for n in names if korean or not HANGUL_RE.search(n)]
    if not gate:
        return True
    low = normalize(text)
    return any(normalize(n) in low for n in gate)


def _arxiv_terms(query: str) -> list[str]:
    words = [w for w in re.findall(r"[A-Za-z0-9][A-Za-z0-9.\-]*[A-Za-z0-9]|[A-Za-z0-9]", query)
             if w.lower() not in STOPWORDS]
    names = [w for w in words if is_name(w)]
    return (names or words)[:3] or [query]


class Sources:
    """검색·취득기. 결과는 work/sources/ 에 캐시한다(성공만). 실패는 사유와 함께 돌려준다."""

    def __init__(self, work_dir: Path, providers: list[str] | None = None, http: Callable = http_get,
                 sleep: Callable[[float], None] = time.sleep, env: dict | None = None,
                 custom: dict[str, Callable[[str, int], list[Hit]]] | None = None):
        self.work_dir = Path(work_dir)
        self.dir = self.work_dir / "sources"
        self.http = http
        self.sleep = sleep
        self.env = os.environ if env is None else env
        self.custom = custom or {}
        self.notes: list[str] = []
        self.providers: list[str] = []
        self._last: dict[str, float] = {}
        self._fails: dict[str, int] = {}
        self.disabled: dict[str, str] = {}
        self.n_requests = 0
        wanted = list(providers if providers is not None else ["arxiv", "semanticscholar", "ddg"])
        wanted += [p for p in ("tavily", "brave") if self.env.get(KEY_ENVS[p]) and p not in wanted]
        for p in wanted:
            if p in self.custom:
                self.providers.append(p)
            elif p in KEY_ENVS and not self.env.get(KEY_ENVS[p]):
                self.notes.append(f"{p}: {KEY_ENVS[p]} 없음 — 건너뜀")
            elif p == "ddg" and importlib.util.find_spec("ddgs") is None:
                self.notes.append('ddg: ddgs 미설치 — 건너뜀 (uv pip install -e ".[search]")')
            elif p in ("arxiv", "semanticscholar", "ddg", "tavily", "brave"):
                self.providers.append(p)
            else:
                self.notes.append(f"{p}: 알 수 없는 검색 소스 — 건너뜀")

    # 공통 --------------------------------------------------------------------
    def _wait(self, provider: str) -> None:
        gap = INTERVAL.get(provider, 0.0)
        last = self._last.get(provider)
        if last is not None and gap:
            wait = last + gap - time.monotonic()
            if wait > 0:
                self.sleep(wait)
        self._last[provider] = time.monotonic()

    def _get(self, provider: str, url: str, headers: dict | None = None, data: bytes | None = None,
             retries: int = 0) -> HttpResponse:
        for attempt in range(retries + 1):
            self._wait(provider)
            self.n_requests += 1
            resp = self.http(url, headers=headers, data=data) if data is not None else self.http(url, headers=headers)
            if resp.status in (429, 500, 502, 503) and attempt < retries:
                self.sleep(min(30.0, 2.0 ** (attempt + 1)))
                continue
            return resp
        return resp

    def search(self, query: str, provider: str, n: int = 3) -> SearchResult:
        if provider in self.disabled:
            return SearchResult(provider, query, False, error=self.disabled[provider])
        cache = self.dir / "search" / f"{_sha(SEARCH_VERSION + '|' + provider + '|' + str(n) + '|' + query)[:20]}.json"
        if cache.exists():
            try:
                hits = [Hit(**h) for h in json.loads(cache.read_text(encoding="utf-8"))["hits"]]
                return SearchResult(provider, query, True, hits)
            except (KeyError, TypeError, json.JSONDecodeError):
                pass
        try:
            if provider in self.custom:
                hits, cacheable = self.custom[provider](query, n), False
            else:
                hits, cacheable = getattr(self, f"_search_{provider}")(query, n)
        except NetworkError as e:
            return self._failed(provider, query, f"네트워크 오류: {e}", network=True)
        except AmbiguousEmpty as e:
            return SearchResult(provider, query, False, error=str(e))
        except LectureReconError as e:
            return self._failed(provider, query, str(e))
        self._fails[provider] = 0
        if cacheable:
            cache.parent.mkdir(parents=True, exist_ok=True)
            cache.write_text(json.dumps({"provider": provider, "query": query, "created": dt.date.today().isoformat(),
                                         "hits": [asdict(h) for h in hits]}, ensure_ascii=False, indent=1),
                             encoding="utf-8")
        return SearchResult(provider, query, True, hits)

    def _failed(self, provider: str, query: str, error: str, network: bool = False) -> SearchResult:
        self._fails[provider] = self._fails.get(provider, 0) + 1
        limit = 2 if network or provider == "semanticscholar" else 3
        if self._fails[provider] >= limit:
            self.disabled[provider] = f"연속 실패 {self._fails[provider]}회로 이번 실행에서 중단 (마지막: {error})"
        return SearchResult(provider, query, False, error=error)

    @staticmethod
    def _check(resp: HttpResponse, provider: str) -> None:
        if resp.status != 200:
            detail = resp.body[:300].decode("utf-8", "replace")
            try:
                detail = json.loads(detail).get("message") or detail
            except (json.JSONDecodeError, AttributeError):
                pass
            detail = re.sub(r"\s+", " ", str(detail))[:80]
            raise LectureReconError(f"HTTP {resp.status} {detail}".strip())

    # 소스별 ------------------------------------------------------------------
    def _search_arxiv(self, query: str, n: int):
        terms = _arxiv_terms(query)
        hits = self._arxiv(" AND ".join(f"all:{w}" for w in terms), n)
        if not hits and len(terms) > 1:  # 이름 여럿의 AND 가 너무 좁으면 첫 이름만으로 한 번 더
            hits = self._arxiv(f"all:{terms[0]}", n)
        return hits, True

    def _arxiv(self, search_query: str, n: int) -> list[Hit]:
        params = urllib.parse.urlencode({"search_query": search_query, "start": 0, "max_results": n})
        resp = self._get("arxiv", f"https://export.arxiv.org/api/query?{params}", retries=2)
        self._check(resp, "arxiv")
        ns = {"a": "http://www.w3.org/2005/Atom"}
        try:
            root = ET.fromstring(resp.body)
        except ET.ParseError as e:
            raise LectureReconError(f"arxiv Atom 파싱 실패: {e}") from None
        hits = []
        for entry in root.findall("a:entry", ns):
            ident = (entry.findtext("a:id", "", ns) or "").strip()
            m = ARXIV_ID_RE.search(ident)
            if not m:
                continue
            aid = m.group(1)
            title = re.sub(r"\s+", " ", entry.findtext("a:title", "", ns) or "").strip()
            summary = re.sub(r"\s+", " ", entry.findtext("a:summary", "", ns) or "").strip()
            hits.append(Hit("arxiv", title, f"https://arxiv.org/abs/{aid}", summary[:300], arxiv_id=aid,
                            pdf_url=f"https://arxiv.org/pdf/{aid}", abstract=summary))
        return hits

    def _search_semanticscholar(self, query: str, n: int):
        params = urllib.parse.urlencode({"query": query, "limit": n,
                                         "fields": "title,url,abstract,externalIds,openAccessPdf,year"})
        headers = {}
        for name in S2_KEY_ENVS:
            if self.env.get(name):
                headers["x-api-key"] = self.env[name]
                break
        resp = self._get("semanticscholar", f"https://api.semanticscholar.org/graph/v1/paper/search?{params}",
                         headers=headers, retries=2 if headers else 1)  # 키 없는 공용 풀은 429 가 잦다
        self._check(resp, "semanticscholar")
        hits = []
        for p in _json_body(resp, "semanticscholar").get("data") or []:
            ext = p.get("externalIds") or {}
            aid = ext.get("ArXiv") or ""
            pdf = (p.get("openAccessPdf") or {}).get("url") or ""
            url = f"https://arxiv.org/abs/{aid}" if aid else (p.get("url") or "")
            hits.append(Hit("semanticscholar", p.get("title") or "", url, (p.get("abstract") or "")[:300],
                            arxiv_id=aid, pdf_url=pdf, abstract=p.get("abstract") or ""))
        return hits, True

    def _search_ddg(self, query: str, n: int):
        from ddgs import DDGS

        last = ""
        for attempt in range(2):
            self._wait("ddg")
            self.n_requests += 1
            try:
                rows = DDGS(timeout=15).text(query, max_results=n)
            except Exception as e:  # ddgs 는 결과 없음·차단·네트워크를 모두 예외로 낸다
                last = f"{type(e).__name__}: {e}"
                if "no results" in str(e).lower() and attempt == 1:
                    raise AmbiguousEmpty("결과 0건(차단·일시 오류와 구분할 수 없음)") from None
                continue
            hits = [Hit("ddg", r.get("title") or "", r.get("href") or r.get("url") or "", r.get("body") or "")
                    for r in rows if r.get("href") or r.get("url")]
            for h in hits:
                m = ARXIV_ID_RE.search(h.url)
                if m:
                    h.arxiv_id = m.group(1)
            return hits, True
        raise LectureReconError(f"ddg 실패: {last}")

    def _search_tavily(self, query: str, n: int):
        body = json.dumps({"query": query, "max_results": n, "search_depth": "basic"}).encode("utf-8")
        resp = self._get("tavily", "https://api.tavily.com/search", data=body,
                         headers={"Content-Type": "application/json",
                                  "Authorization": f"Bearer {self.env.get('TAVILY_API_KEY', '')}"}, retries=2)
        self._check(resp, "tavily")
        rows = _json_body(resp, "tavily").get("results") or []
        return [Hit("tavily", r.get("title") or "", r.get("url") or "", (r.get("content") or "")[:300])
                for r in rows if r.get("url")], True

    def _search_brave(self, query: str, n: int):
        params = urllib.parse.urlencode({"q": query, "count": n})
        resp = self._get("brave", f"https://api.search.brave.com/res/v1/web/search?{params}",
                         headers={"Accept": "application/json",
                                  "X-Subscription-Token": self.env.get("BRAVE_API_KEY", "")}, retries=2)
        self._check(resp, "brave")
        rows = (_json_body(resp, "brave").get("web") or {}).get("results") or []
        return [Hit("brave", r.get("title") or "", r.get("url") or "", (r.get("description") or "")[:300])
                for r in rows if r.get("url")], True

    # 취득 --------------------------------------------------------------------
    def _cached(self, url: str) -> Doc | None:
        key = _sha(url)[:20]
        meta, txt = self.dir / f"{key}.json", self.dir / f"{key}.txt"
        if meta.exists() and txt.exists():
            try:
                m = json.loads(meta.read_text(encoding="utf-8"))
                return Doc(m["url"], m.get("title", ""), m.get("kind", ""), "ok", text=txt.read_text(encoding="utf-8"),
                           doc_hash=m.get("doc_hash", ""), path=str(txt.relative_to(self.work_dir.parent)))
            except (KeyError, json.JSONDecodeError):
                return None
        return None

    def _store(self, url: str, doc: Doc) -> Doc:
        key = _sha(url)[:20]
        self.dir.mkdir(parents=True, exist_ok=True)
        txt = self.dir / f"{key}.txt"
        txt.write_text(doc.text, encoding="utf-8")
        doc.doc_hash = doc_hash(doc.text)
        doc.path = str(txt.relative_to(self.work_dir.parent))
        (self.dir / f"{key}.json").write_text(json.dumps(
            {"url": url, "title": doc.title, "kind": doc.kind, "doc_hash": doc.doc_hash,
             "fetched": dt.datetime.now().isoformat(timespec="seconds"), "chars": len(doc.text)},
            ensure_ascii=False, indent=1), encoding="utf-8")
        return doc

    def fetch_url(self, url: str, title: str = "") -> Doc:
        cached = self._cached(url)
        if cached:
            return cached
        try:
            resp = self.http(url, headers={"Accept": "text/html,application/pdf,text/plain;q=0.9,*/*;q=0.5"})
            self.n_requests += 1
        except NetworkError as e:
            return Doc(url, title, "", "fetch_failed", f"네트워크 오류: {e}")
        if resp.status != 200:
            return Doc(url, title, "", "fetch_failed", f"HTTP {resp.status}")
        kind, text, problem = extract(resp.body, resp.content_type, resp.url or url)
        if problem:
            return Doc(url, title, kind, "extract_poor", problem)
        return self._store(url, Doc(url, title, kind, "ok", text=text))

    def fetch(self, hit: Hit) -> Doc:
        """arXiv 는 html → pdf → 초록 순, 그 밖은 URL → (S2 오픈 PDF) → 초록."""
        tries: list[str] = []
        if hit.arxiv_id:
            tries += [f"https://arxiv.org/html/{hit.arxiv_id}", f"https://arxiv.org/pdf/{hit.arxiv_id}"]
        else:
            tries += [u for u in (hit.url, hit.pdf_url) if u]
        reasons, statuses = [], []
        for url in dict.fromkeys(tries):
            doc = self.fetch_url(url, hit.title)
            if doc.ok:
                doc.url = hit.url or doc.url
                return doc
            reasons.append(f"{url}: {doc.reason}")
            statuses.append(doc.status)
        if hit.abstract and not quality(hit.abstract):
            return Doc(hit.url, f"{hit.title} (초록)", "abstract", "ok", text=hit.abstract, doc_hash=doc_hash(hit.abstract))
        status = "extract_poor" if "extract_poor" in statuses else "fetch_failed"
        return Doc(hit.url, hit.title, "", status, "; ".join(reasons) or "URL 없음")


# 사용자 자료 -------------------------------------------------------------------

def user_dir(work_dir: Path, nn: str) -> Path:
    return Path(work_dir) / "sources" / "user" / nn


def load_manifest(work_dir: Path, nn: str) -> list[dict]:
    path = user_dir(work_dir, nn) / "manifest.json"
    if not path.exists():
        return []
    try:
        items = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError) as e:
        raise LectureReconError(f"{path}: 사용자 자료 목록(manifest)이 깨졌다({e}) — 고치거나 지우고 `sources add` 로 "
                                "다시 넣는다") from None
    if not isinstance(items, list) or not all(isinstance(e, dict) and isinstance(e.get("target", ""), str)
                                              for e in items):
        raise LectureReconError(f"{path}: 사용자 자료 목록은 {{id, target, …}} 객체의 목록이어야 한다")
    return items


def _save_manifest(work_dir: Path, nn: str, items: list[dict]) -> None:
    path = user_dir(work_dir, nn) / "manifest.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(items, ensure_ascii=False, indent=1), encoding="utf-8")
    os.replace(tmp, path)


def add_user_source(work_dir: Path, nn: str, target: str, note: str = "", http: Callable = http_get) -> dict:
    """URL 이나 파일(pdf/html/txt/md)을 work/sources/user/NN/ 에 받아 둔다. 추출이 나쁘면 근거로 쓰지 않는다."""
    items = load_manifest(work_dir, nn)
    d = user_dir(work_dir, nn)
    d.mkdir(parents=True, exist_ok=True)
    sid = f"U{len(items) + 1}"
    entry = {"id": sid, "target": target, "note": note, "added": dt.date.today().isoformat(),
             "status": "", "reason": "", "kind": "", "text_file": "", "doc_hash": "", "title": ""}
    text_out = ""
    if re.match(r"^https?://", target, re.I):
        try:
            resp = http(target)
        except NetworkError as e:
            resp = None
            entry.update(status="fetch_failed", reason=f"네트워크 오류: {e}")
        if resp is not None and resp.status != 200:
            entry.update(status="fetch_failed", reason=f"HTTP {resp.status}")
        elif resp is not None:
            kind, text, problem = extract(resp.body, resp.content_type, target)
            entry.update(kind=kind)
            if problem:
                entry.update(status="extract_poor", reason=problem)
            else:
                entry.update(status="ok")
                text_out = text
        entry["title"] = target
    else:
        src = Path(target).expanduser()
        if not src.is_file():
            raise LectureReconError(f"파일이 없다: {target}")
        copy = d / f"{sid}_{src.name}"
        shutil.copyfile(src, copy)
        kind, text, problem = extract(src.read_bytes(), "", src.name)
        if src.suffix.lower() in (".md", ".txt"):
            kind, text = "text", src.read_text(encoding="utf-8", errors="replace").strip()
            problem = quality(text)
        entry.update(kind=kind, title=src.name, target=str(src.resolve()), copy=str(copy.relative_to(work_dir.parent)))
        if problem:
            entry.update(status="extract_poor", reason=problem)
        else:
            entry.update(status="ok")
            text_out = text
    if entry["status"] == "ok":
        txt = d / f"{sid}.txt"
        txt.write_text(text_out, encoding="utf-8")
        entry.update(text_file=str(txt.relative_to(work_dir.parent)), doc_hash=doc_hash(text_out), chars=len(text_out))
    items.append(entry)
    _save_manifest(work_dir, nn, items)
    return entry


def retry_user_sources(work_dir: Path, nn: str, http: Callable = http_get) -> None:
    """취득 실패했던 URL 자료를 다시 받아 본다(verify 시작 시)."""
    items = load_manifest(work_dir, nn)
    changed = False
    for e in items:
        if e.get("status") != "fetch_failed" or not re.match(r"^https?://", e["target"], re.I):
            continue
        try:
            resp = http(e["target"])
        except NetworkError:
            continue
        if resp.status != 200:
            continue
        kind, text, problem = extract(resp.body, resp.content_type, e["target"])
        e["kind"] = kind
        if problem:
            e.update(status="extract_poor", reason=problem)
        else:
            txt = user_dir(work_dir, nn) / f"{e['id']}.txt"
            txt.write_text(text, encoding="utf-8")
            e.update(status="ok", reason="", text_file=str(txt.relative_to(Path(work_dir).parent)),
                     doc_hash=doc_hash(text), chars=len(text))
        changed = True
    if changed:
        _save_manifest(work_dir, nn, items)


def user_texts(work_dir: Path, nn: str) -> dict[str, tuple[str, str]]:
    """사용자 자료 id(U1, …) → (제목, 캐시된 본문). status ok 인 것만(correct 의 용어 목록·근거 검사용)."""
    root = Path(work_dir).parent
    return {e["id"]: (e.get("note") or e.get("title") or e["target"],
                      (root / e["text_file"]).read_text(encoding="utf-8"))
            for e in load_manifest(work_dir, nn) if e.get("status") == "ok" and e.get("text_file") and e.get("id")}


def user_docs(work_dir: Path, nn: str) -> list[Doc]:
    """근거로 쓸 수 있는(status ok) 사용자 자료."""
    docs = []
    root = Path(work_dir).parent
    for e in load_manifest(work_dir, nn):
        if e.get("status") != "ok" or not e.get("text_file"):
            continue
        text = (root / e["text_file"]).read_text(encoding="utf-8")
        title = e.get("note") or e.get("title") or e["target"]
        url = e["target"] if re.match(r"^https?://", e["target"], re.I) else f"(사용자 자료 {e['id']}) {Path(e['target']).name}"
        docs.append(Doc(url, title, "user", "ok", text=text, doc_hash=e.get("doc_hash") or doc_hash(text),
                        path=e["text_file"]))
    return docs
