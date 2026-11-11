"""`claims NN` → work/claims/NN.yaml, `verify NN` → work/verify/NN.json + work/verify-NN.md.

verify 는 claim 마다 내부 상태를 남긴다:
  supported → 확인, conflicting → 발언과 원문이 다른 것, search_ok_zero → 검색했으나 찾지 못한 것,
  search_failed | fetch_failed | extract_poor | insufficient → 확인하지 않은 것(사유 포함).
실패가 "못찾음"으로 보이지 않게 한다. 확인·다름은 인용문이 캐시된 원문에 있어야만 남는다.
"""

from __future__ import annotations

import difflib
import json
import re
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from . import context, llm as llm_mod, sources as src, transcript
from .config import Event, LectureReconError, Talk

KINDS = ("수치", "고유명사", "날짜", "주장")
CATEGORIES = ("확인", "발언과 원문이 다른 것", "검색했으나 찾지 못한 것", "확인하지 않은 것")
STATUS_CATEGORY = {"supported": CATEGORIES[0], "conflicting": CATEGORIES[1], "search_ok_zero": CATEGORIES[2]}
FINAL_STATUSES = ("supported", "conflicting", "search_ok_zero")  # 재실행 때 다시 하지 않는 상태
VERDICTS = ("확인", "다름", "불충분", "없음")
JUDGE_BATCH = 5
MAX_DOCS = 4          # claim 당 근거 발췌 수(사용자 자료 포함)
MAX_FETCH = 6         # claim 당 취득 시도 수
MIN_QUOTE = 12
MONTHS = {m: i for i, names in enumerate([
    ("january", "jan"), ("february", "feb"), ("march", "mar"), ("april", "apr"), ("may",), ("june", "jun"),
    ("july", "jul"), ("august", "aug"), ("september", "sep", "sept"), ("october", "oct"),
    ("november", "nov"), ("december", "dec")], 1) for m in names}


def category(status: str) -> str:
    return STATUS_CATEGORY.get(status, CATEGORIES[3])


def claims_path(event: Event, talk: Talk) -> Path:
    return event.work_dir / "claims" / f"{talk.nn}.yaml"


def verify_json_path(event: Event, talk: Talk) -> Path:
    return event.work_dir / "verify" / f"{talk.nn}.json"


def verify_md_path(event: Event, talk: Talk) -> Path:
    return event.work_dir / f"verify-{talk.nn}.md"


def load_verify(event: Event, talk: Talk) -> dict | None:
    path = verify_json_path(event, talk)
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else None


# claims ----------------------------------------------------------------------

MIN_TRANSCRIPT_QUOTE = 8   # 후보 문장 최소 길이(원문 글자)
MIN_QUOTE_CHARS = 6        # 전사 인용 최소 길이: 공백·문장부호를 뺀 글자·숫자 수
CANDIDATE_CHARS = 240
# 후보 문장: 숫자·%·단위·영문(2자 이상)·연도. 한글 수사는 단위가 붙을 때만(이번·만들다 같은 말은 아니다)
CANDIDATE_RE = re.compile(r"\d|%|[A-Za-z]{2,}|퍼센트|밀리초|밀리세컨|헤르츠"
                          r"|(?<![가-힣])(?:[일이삼사오육칠팔구]?[십백천만억]|[일이삼사오육칠팔구])+\s?"
                          r"(?:퍼센트|프로|배|년|개월|명|개|대|위)(?![가-힣])")
SENTENCE_SPLIT_RE = re.compile(r"(?:(?<=[.?!죠])|(?<=니다|어요|아요|에요|예요|해요|고요|든요|네요|군요))\s+")
WRAPPER_RE = re.compile(r'\[(?:추정|불명확)(?:\s*:\s*"?([^\]\n]*?)"?)?\]')
EMPTY_DROP = "모델이 빈 목록을 반환(후보 {n}개, 1회 재요청 후)"
SUBJECTS = ("own", "external")
SUBJECT_LABEL = {"own": "own(발표자 측 결과)", "external": "external(공개된 외부 사실)"}
# subject 가 없거나 틀렸을 때: 1인칭(저희·우리·자사)이나 "발표자 연구진/팀/회사/모델/시스템"만 own.
# "발표자는 …라고 말했다", "발표자의 해석이다", "OpenAI 연구진은 …" 은 external ("우리나라"도 아니다)
OWN_RE = re.compile(r"저희|우리(?!나라)|자사|발표자(?:의)?\s*(?:연구진|팀|회사|모델|시스템)")


def claim_subject(claim: dict) -> str:
    """own = 발표자·발표자 팀/소속의 결과·활동·수치, external = 공개된 외부 사실(타 기관·논문·제품·시장 수치).
    claim 의 `subject` 가 없거나 둘 중 하나가 아니면 claim 문장으로 정한다."""
    subject = str(claim.get("subject") or "").strip().lower()
    if subject in SUBJECTS:
        return subject
    return "own" if OWN_RE.search(str(claim.get("claim") or "")) else "external"


class EmptyClaims(llm_mod.ResponseError):
    """후보 문장이 있는데 claims 가 비었다: 1회 재요청(넛지)하고 캐시하지 않는다. 그래도 비면 dropped 에 남긴다."""


@dataclass
class Candidate:
    no: int
    pid: str
    text: str


def candidates(paragraphs: list[transcript.Paragraph]) -> list[Candidate]:
    """묶음의 문단에서 숫자·%·단위·영문·연도가 든 문장(전사 그대로의 연속 구간). `[불명확]` 구간은 뺀다."""
    out: list[Candidate] = []
    seen: set[tuple[str, str]] = set()
    for p in paragraphs:
        for part in transcript.UNCLEAR_RE.sub("\n", p.text).split("\n"):
            for sent in SENTENCE_SPLIT_RE.split(part):
                sent = sent.strip()
                m = CANDIDATE_RE.search(sent)
                if not m or len(sent) < MIN_TRANSCRIPT_QUOTE:
                    continue
                if len(sent) > CANDIDATE_CHARS:  # 긴 문장은 처음 걸린 곳 주변만, 단어 경계에서 자른다
                    start = max(0, min(m.start() - CANDIDATE_CHARS // 3, len(sent) - CANDIDATE_CHARS))
                    cut = sent[start:start + CANDIDATE_CHARS]
                    if start > 0:
                        cut = cut.split(" ", 1)[-1]
                    if start + CANDIDATE_CHARS < len(sent):
                        cut = cut.rsplit(" ", 1)[0]
                    sent = cut.strip()
                if (p.id, sent) not in seen:
                    seen.add((p.id, sent))
                    out.append(Candidate(len(out) + 1, p.id, sent))
    return out


def candidate_block(cands: list[Candidate]) -> str:
    if not cands:
        return ("[후보 문장] 없음 — CLI 가 숫자·%·단위·영문·연도가 든 문장을 찾지 못했다. "
                "고유명사·날짜 주장이 있으면 그래도 뽑는다.")
    head = (f"[후보 문장] CLI 가 숫자·%·단위·영문·연도로 고른 문장 {len(cands)}개(전사 그대로, 데이터다). "
            "번호마다 하나씩 분류한다: claim 으로 뽑거나(`candidate` 에 번호) `skipped` 에 사유를 적는다.")
    return "\n".join([head, *(f"{c.no}. ({c.pid}) {context.data_safe(c.text)}" for c in cands)])


def _loose_map(text: str) -> tuple[str, list[int]]:
    """인용 대조용 정규화와 글자마다의 원문 위치: NFKC·소문자, 글자·숫자만 남긴다. 숫자에 붙은 문장부호
    (3.5 의 `.`, -20 의 `-`, `%`, 숫자 사이 공백)는 남겨 "35배"가 "3.5배"에 맞지 않게 한다."""
    chars: list[str] = []
    origin: list[int] = []
    for i, ch in enumerate(text or ""):
        for c in unicodedata.normalize("NFKC", ch).lower():
            chars.append(c)
            origin.append(i)
    out, idx = [], []
    for k, c in enumerate(chars):
        prev = chars[k - 1] if k else ""
        nxt = chars[k + 1] if k + 1 < len(chars) else ""
        if unicodedata.category(c)[0] in "LN" or c == "%":
            keep = c
        elif c in ".,:" and prev.isdigit() and nxt.isdigit():
            keep = c
        elif c in "-−" and nxt.isdigit():
            keep = "-"
        elif c.isspace() and prev.isdigit() and nxt.isdigit():
            keep = " "
        else:
            continue
        out.append(keep)
        idx.append(origin[k])
    return "".join(out), idx


def _loose(text: str) -> str:
    return _loose_map(text)[0]


def unwrap(text: str) -> str:
    """`[추정: …]`·`[불명확: "…"]` 표시를 안의 글자로 바꾼다."""
    return WRAPPER_RE.sub(lambda m: f" {m.group(1) or ''} ", text or "")


def _unwrap_kinds(paragraph: str) -> tuple[str, list[str]]:
    """unwrap 한 문단과 글자마다의 출처: plain | guess([추정] 안) | unclear([불명확] 안)."""
    out, kinds, pos = [], [], 0
    for m in WRAPPER_RE.finditer(paragraph or ""):
        out.append(paragraph[pos:m.start()])
        kinds += ["plain"] * (m.start() - pos)
        inner = f" {m.group(1) or ''} "
        kind = "guess" if m.group(0).startswith("[추정") else "unclear"
        out.append(inner)
        kinds += ["plain"] + [kind] * (len(inner) - 2) + ["plain"]
        pos = m.end()
    out.append((paragraph or "")[pos:])
    kinds += ["plain"] * (len(paragraph or "") - pos)
    return "".join(out), kinds


def locate_quote_in(quote: str, paragraph: str) -> dict | None:
    """전사 인용이 문단에 있으면 {from_correction, low_confidence}: 공백·문장부호를 무시하고 `[추정]`·`[불명확]`
    표시를 벗겨 찾는다(숫자 경계는 지킨다). from_correction = `[추정: …]`(교정된 구간)과 겹친다,
    low_confidence = 전부 `[불명확]` 구간 안이다. 없으면 None."""
    q = _loose(unwrap(quote))
    if len(q) < 4:
        return None
    hay_text, kinds = _unwrap_kinds(paragraph)
    hay, idx = _loose_map(hay_text)
    start = hay.find(q)
    while start >= 0:
        end = start + len(q)
        before = hay[start - 1] if start else ""
        after = hay[end] if end < len(hay) else ""
        cut_left = q[0].isdigit() and (before.isdigit() or (before and before in ".,-"))
        cut_right = q[-1].isdigit() and (after.isdigit() or (after in ".," and hay[end + 1:end + 2].isdigit()))
        if not cut_left and not cut_right:
            spans = {kinds[idx[k]] for k in range(start, end)}
            return {"from_correction": "guess" in spans or "[추정" in quote, "low_confidence": spans == {"unclear"}}
        start = hay.find(q, start + 1)
    return None


def quote_in(quote: str, paragraph: str) -> bool:
    """전사 인용이 문단에 있는가(locate_quote_in)."""
    return locate_quote_in(quote, paragraph) is not None


def as_list(value, field: str) -> list[str]:
    """문자열 하나로 온 목록 필드는 [값] 으로 감싼다. 객체·중첩 목록은 형식 오류."""
    if value is None:
        return []
    if isinstance(value, (str, int, float)) and not isinstance(value, bool):
        value = [value]
    if not isinstance(value, list) or any(isinstance(v, (dict, list)) for v in value):
        raise llm_mod.ResponseError(f"{field} 는 문자열 목록이어야 한다: {str(value)[:60]}")
    return [str(v).strip() for v in value if v is not None and str(v).strip()]


def _refs(value, n: int) -> list[int]:
    """candidate 번호(정수·정수 목록·"3")를 1..n 안의 것만. 이상한 값은 무시한다(분류 안 됨으로 센다)."""
    out = []
    for v in value if isinstance(value, list) else [value]:
        if isinstance(v, bool):
            continue
        if isinstance(v, (int, float)) or (isinstance(v, str) and v.strip().isdigit()):
            if 1 <= int(v) <= n:
                out.append(int(v))
    return out


def _validate_claims(text: str, paragraphs: dict[str, str],
                     n_candidates: int = 0) -> tuple[list[dict], list[str], dict[int, tuple[str, str]]]:
    """(통과한 claim, 버린 사유, 후보 번호 → (claim|dropped|skip, 사유)). 형식 자체가 틀리면 ResponseError(다시 받는다).
    후보 문장이 있는데 claims 도 skipped 도 비었으면 EmptyClaims(넛지와 함께 다시 받고, 캐시하지 않는다)."""
    data = llm_mod.extract_json(text, keys=("claims",))
    rows = data.get("claims") if isinstance(data, dict) else data
    if not isinstance(rows, list):
        raise llm_mod.ResponseError('`{"claims": [...]}` 형식이 아니다')
    bad = [i for i, r in enumerate(rows) if not isinstance(r, dict)]
    if bad:
        raise llm_mod.ResponseError(f"claims[{bad[0]}] 가 객체가 아니다: {str(rows[bad[0]])[:60]}")
    ids = [str(r.get("id") or "") for r in rows]
    dup = sorted({i for i in ids if i and ids.count(i) > 1})
    if dup:
        raise llm_mod.ResponseError(f"claim id 중복: {', '.join(dup)}")
    skipped_raw = (data.get("skipped") if isinstance(data, dict) else None) or []
    if not isinstance(skipped_raw, list):
        raise llm_mod.ResponseError("skipped 가 목록이 아니다")
    marks: dict[int, tuple[str, str]] = {}
    for r in skipped_raw:
        reason = str(r.get("reason") or "").strip() if isinstance(r, dict) else ""
        for i in _refs(r.get("candidate") if isinstance(r, dict) else r, n_candidates):
            marks[i] = ("skip", reason)
    if not rows and n_candidates and not marks:  # 아무 후보도 분류하지 않은 빈 응답만(모두 사유와 함께 건너뛴 것은 유효)
        raise EmptyClaims(f"후보 문장이 {n_candidates}개인데 claims 가 비었다. 후보마다 claim 으로 뽑거나 skipped 에 "
                          "{candidate, reason} 으로 사유를 적는다. 숫자·고유명사·날짜가 든 후보는 검증할 수 없는 "
                          "의견·감상·잡음이 아니면 claim 이다.")
    keep, dropped = [], []
    for r in rows:
        for key in ("quote_from_transcript", "claim", "kind"):
            if r.get(key) is not None and not isinstance(r.get(key), str):
                raise llm_mod.ResponseError(f"{r.get('id')}: {key} 가 문자열이 아니다")
        pids = as_list(r.get("paragraph_ids"), "paragraph_ids")
        quote = (r.get("quote_from_transcript") or "").strip()
        claim = (r.get("claim") or "").strip()
        kind = (r.get("kind") or "").strip()
        queries = as_list(r.get("queries"), "queries")[:3]
        keywords = as_list(r.get("keywords"), "keywords")[:6]
        refs = _refs(r.get("candidate"), n_candidates)
        label = r.get("id") or claim[:30]
        before = len(dropped)
        if not pids or any(p not in paragraphs for p in pids):
            dropped.append(f"{label}: 문단 ID 가 이 묶음에 없다 ({', '.join(pids) or '없음'})")
        elif not claim or not quote:
            dropped.append(f"{label}: claim 또는 quote_from_transcript 가 비었다")
        elif len(_loose(unwrap(quote))) < MIN_QUOTE_CHARS:
            dropped.append(f"{label}: quote_from_transcript 가 너무 짧다(글자·숫자 {len(_loose(unwrap(quote)))}자 "
                           f"< {MIN_QUOTE_CHARS})")
        elif kind not in KINDS:
            dropped.append(f"{label}: kind 가 {'|'.join(KINDS)} 가 아니다 ({kind!r})")
        elif not queries:
            dropped.append(f"{label}: queries 가 비었다")
        elif not (found := [f for p in pids if (f := locate_quote_in(quote, paragraphs[p]))]):
            dropped.append(f"{label}: quote_from_transcript 가 문단 {', '.join(pids)} 에 그대로 없다"
                           "(공백·문장부호·[추정]/[불명확] 표시를 무시해도)")
        else:
            flags = {k: True for k in ("from_correction", "low_confidence") if found[0][k]}
            keep.append({"paragraph_ids": pids, "quote_from_transcript": quote, "claim": claim, "kind": kind,
                         "subject": claim_subject(r), "queries": queries, "keywords": keywords, **flags})
        for i in refs:
            if len(dropped) > before:
                marks.setdefault(i, ("dropped", dropped[-1]))
            else:
                marks[i] = ("claim", "")
    return keep, dropped, marks


def claims_requests(event: Event, talk: Talk, chunk: int) -> list[context.Request]:
    """묶음마다 claims 요청(이름·지시·입력·문단·후보 문장). run_claims 와 --plan 이 같이 쓴다."""
    _, pt = context.load_transcript(event, talk)
    system = llm_mod.prompt("claims")
    table = context.slides_table(event, talk)
    groups = transcript.batches(pt.paragraphs, chunk)
    out = []
    for k, group in enumerate(groups, 1):
        cands = candidates(group)
        user = "\n\n".join(x for x in [
            context.event_info(event, talk),
            f"[슬라이드 표]\n{table}" if table else "",
            f"[전사 문단] 묶음 {k}/{len(groups)} ({group[0].id}–{group[-1].id})",
            context.transcript_block(group, pt.chapters),
            candidate_block(cands),
        ] if x)
        paras = {p.id: p.text for p in group}
        out.append(context.Request(f"claims-{talk.nn}-{k}", system, user, group, cands,
                                   validate=lambda t, paras=paras, n=len(cands): _validate_claims(t, paras, n)))
    return out


def run_claims(event: Event, talk: Talk, llm, chunk: int | None = None, progress=None) -> dict:
    reqs = claims_requests(event, talk, context.chunk_chars(llm, chunk))
    found, dropped, pending, stats, audit = [], [], [], [], []
    for k, rq in enumerate(reqs, 1):
        n = len(rq.candidates)
        span = f"{rq.paragraphs[0].id}–{rq.paragraphs[-1].id}"
        calls = getattr(llm, "calls", 0)
        try:
            got = llm_mod.collect_pending(lambda: llm.ask(rq.name, rq.system, rq.user, json_mode=True,
                                                          validate=rq.validate), pending)
        except EmptyClaims:
            got = ([], [EMPTY_DROP.format(n=n)], {})
        if got is None:
            if progress:
                progress(f"묶음 {k}/{len(reqs)} ({span}): 후보 {n} · 응답 대기")
            continue
        keep, drop, marks = got
        for cand in rq.candidates:
            st, why = marks.get(cand.no, ("none", ""))
            audit.append({"batch": k, "no": cand.no, "paragraph_id": cand.pid, "text": cand.text, "status": st,
                          **({"reason": why} if why else {})})
        unclassified = sum(cand.no not in marks for cand in rq.candidates)
        skipped = sum(st == "skip" for st, _ in marks.values())
        stats.append({"batch": k, "paragraphs": span, "candidates": n, "claims": len(keep), "dropped": len(drop),
                      "skipped": skipped, "unclassified": unclassified})
        retried = getattr(llm, "calls", 0) - calls > 1
        if progress:
            progress(f"묶음 {k}/{len(reqs)} ({span}): 후보 {n} · claim {len(keep)} · 버림 {len(drop)} · "
                     f"건너뜀 {skipped}" + (f" · 분류 안 됨 {unclassified}" if unclassified else "")
                     + (" (형식 오류·빈 응답으로 1회 재요청)" if retried else ""))
        found += keep
        dropped += [f"묶음 {k}: {d}" for d in drop]
    if pending:
        raise llm_mod.ManualPending(pending, event.root)
    seen, claims = set(), []
    for c in found:
        key = (src.normalize(c["claim"]), tuple(c["paragraph_ids"]))
        if key in seen:
            continue
        seen.add(key)
        claims.append({"id": f"{talk.nn}-C{len(claims) + 1:03d}", **c})
    data = {"talk": talk.nn, "created": context.today(), "backend": llm.label, "batches": len(reqs),
            "claims": claims, "dropped": dropped, "batch_stats": stats, "candidates": audit}
    context.write_atomic(claims_path(event, talk),
                         yaml.safe_dump(data, allow_unicode=True, sort_keys=False, width=1000))
    return data


def load_claims(event: Event, talk: Talk) -> list[dict]:
    path = claims_path(event, talk)
    if not path.exists():
        raise LectureReconError(f"{talk.nn}: {event.rel(path)} 없음 — 먼저 `lecture-recon claims {talk.nn}`")
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    claims = data.get("claims") or []
    ids = [c.get("id") for c in claims]
    if len(set(ids)) != len(ids) or not all(ids):
        raise LectureReconError(f"{event.rel(path)}: claim id 가 비었거나 중복된다")
    return claims


# 근거 모으기 ----------------------------------------------------------------------

@dataclass
class Evidence:
    sid: str
    url: str
    title: str
    doc_hash: str
    passage: str
    text: str
    path: str
    kind: str
    user: bool = False

    def meta(self) -> dict:
        return {"id": self.sid, "url": self.url, "title": self.title, "doc_hash": self.doc_hash, "kind": self.kind,
                "path": self.path, "user": self.user}


@dataclass
class Gathered:
    evidence: list[Evidence] = field(default_factory=list)
    status: str = ""          # 근거가 없을 때의 상태
    reason: str = ""
    searches: list[dict] = field(default_factory=list)
    fetches: list[dict] = field(default_factory=list)
    unclean: tuple[str, str] | None = None  # 검색·취득 일부 실패: 판정이 "없음"이어도 확정하지 않는다


def claim_keywords(claim: dict) -> list[str]:
    return src.keywords(claim.get("claim", ""), *claim.get("queries", []), extra=claim.get("keywords"))


def claim_names(claim: dict) -> list[str]:
    """문서 관련성을 가르는 이름: claims 단계가 준 keywords 에서만 고른다."""
    return src.entity_names(claim.get("keywords") or [])


def _fetch(searcher, hit) -> src.Doc:
    try:
        return searcher.fetch(hit)
    except Exception as e:  # 소스 하나의 예외가 실행 전체를 멈추지 않게
        return src.Doc(hit.url, hit.title, "", "fetch_failed", f"내부 오류: {type(e).__name__}: {str(e)[:120]}")


def gather(claim: dict, searcher: src.Sources, user: list[src.Doc], window: int = src.PASSAGE_CHARS) -> Gathered:
    g = Gathered()
    kws = claim_keywords(claim)
    names = claim_names(claim)
    for doc in user:
        passage, score = src.relevant_passage(doc.text, kws, window)
        if score > 0 and len(g.evidence) < MAX_DOCS:
            g.evidence.append(Evidence(f"S{len(g.evidence) + 1}", doc.url, doc.title, doc.doc_hash, passage, doc.text,
                                       doc.path, "user", user=True))
    results = [searcher.search(q, p) for q in claim.get("queries", [])[:3] for p in searcher.providers]
    g.searches = [r.log() for r in results]
    hits, seen = [], set()
    for rank in range(3):
        for r in results:
            if r.ok and rank < len(r.hits):
                h = r.hits[rank]
                key = h.arxiv_id or h.url
                if key and key not in seen:
                    seen.add(key)
                    hits.append(h)
    statuses = []
    tries = 0
    irrelevant = 0
    for h in hits:
        if len(g.evidence) >= MAX_DOCS or tries >= MAX_FETCH:
            break
        tries += 1
        doc = _fetch(searcher, h)
        g.fetches.append({"url": h.url, "provider": h.provider, "status": doc.status, "reason": doc.reason,
                          "kind": doc.kind})
        statuses.append(doc.status)
        if not doc.ok:
            continue
        passage, score = src.relevant_passage(doc.text, kws, window)
        if score <= 0 or not src.relevant(doc.text, names):
            irrelevant += 1
            continue
        if any(e.doc_hash == doc.doc_hash for e in g.evidence):
            continue
        g.evidence.append(Evidence(f"S{len(g.evidence) + 1}", doc.url, doc.title, doc.doc_hash, passage, doc.text,
                                   doc.path, doc.kind))

    failed = [r for r in results if not r.ok]
    bad_fetches = [f for f in g.fetches if f["status"] != "ok"]
    errors = "; ".join(dict.fromkeys(f"{r.provider}: {r.error}" for r in failed))
    fetch_errors = "; ".join(f"{f['url']}: {f['reason']}" for f in bad_fetches)[:600]
    if not results:
        g.unclean = ("search_failed", "쓸 수 있는 검색 소스가 없다(" + "; ".join(searcher.notes) + ")")
    elif len(failed) == len(results):
        g.unclean = ("search_failed", f"검색 실패({len(failed)}회 모두) — {errors}")
    elif failed:
        g.unclean = ("search_failed", f"검색 일부 실패({len(results) - len(failed)}/{len(results)}회만 성공) — {errors}")
    elif bad_fetches:
        poor = all(f["status"] == "extract_poor" for f in bad_fetches)
        g.unclean = ("extract_poor" if poor else "fetch_failed",
                     ("본문 추출 불량 — " if poor else "본문 취득 실패 — ") + fetch_errors)
    if g.evidence:
        return g
    # 근거가 없을 때: "검색했으나 찾지 못한 것"은 시도한 검색이 모두 성공하고 취득 실패도 없을 때만
    if g.unclean and (failed or not results):
        g.status, g.reason = g.unclean
    elif not hits:
        g.status, g.reason = "search_ok_zero", f"검색 {len(results)}회 모두 성공, 결과 0건"
    elif g.unclean:
        g.status, g.reason = g.unclean
        if irrelevant:
            g.reason += f" (읽은 {irrelevant}건은 주장과 겹치는 단락이 없다)"
    else:
        g.status, g.reason = "search_ok_zero", f"검색 결과 {irrelevant}건을 읽었으나 주장과 겹치는 단락이 없다"
    return g


# 판정 ------------------------------------------------------------------------

def _attr(text: str) -> str:
    return context.data_safe(str(text or "")).replace('"', "'").replace("\n", " ")


def judge_block(claim: dict, evidence: list[Evidence], speaker: str = "") -> str:
    """speaker: 발표자·소속·별칭 한 줄(run_verify 가 event.yaml 에서 만든다)."""
    lines = [f"[주장 {claim['id']}]", f"종류: {claim['kind']}", f"대상: {SUBJECT_LABEL[claim_subject(claim)]}",
             *([f"발표자: {context.data_safe(speaker)}"] if speaker else []),
             f"주장: {context.data_safe(claim['claim'])}",
             f"전사 인용 ({', '.join(claim['paragraph_ids'])}): \"{context.data_safe(claim['quote_from_transcript'])}\""]
    for e in evidence:
        lines += [f'<source id="{e.sid}" url="{_attr(e.url)}" hash="{e.doc_hash}" title="{_attr(e.title)}">',
                  context.data_safe(e.passage), "</source>"]
    return "\n".join(lines)


def validate_verdicts(text: str, expected: list[str]) -> dict[str, dict]:
    rows = llm_mod.json_object(text, "verdicts")
    bad = [i for i, r in enumerate(rows) if not isinstance(r, dict)]
    if bad:
        raise llm_mod.ResponseError(f"verdicts[{bad[0]}] 가 객체가 아니다: {str(rows[bad[0]])[:60]}")
    got = [str(r.get("claim_id") or "") for r in rows]
    missing = [i for i in expected if i not in got]
    extra = [i for i in got if i not in expected]
    dup = sorted({i for i in got if got.count(i) > 1})
    if missing or extra or dup:
        parts = [f"빠짐 {', '.join(missing)}" if missing else "", f"모르는 id {', '.join(extra)}" if extra else "",
                 f"중복 {', '.join(dup)}" if dup else ""]
        raise llm_mod.ResponseError("claim id 집합이 입력과 다르다: " + "; ".join(p for p in parts if p))
    out = {}
    for r in rows:
        v = str(r.get("verdict") or "").strip()
        if v not in VERDICTS:
            raise llm_mod.ResponseError(f"{r.get('claim_id')}: verdict 가 {'|'.join(VERDICTS)} 가 아니다 ({v!r})")
        for key in ("source_id", "quote", "note", "confidence"):
            if r.get(key) is not None and not isinstance(r.get(key), str):
                raise llm_mod.ResponseError(f"{r.get('claim_id')}: {key} 가 문자열이 아니다")
        out[str(r["claim_id"])] = r
    return out


def _numbers(text: str) -> list[str]:
    return [n.lstrip("0") or "0" for n in src.NUMBER_RE.findall((text or "").replace(",", ""))]


NEGATION_RE = re.compile(r"n't\b|\b(?:not|no|never|none|nor|without|cannot|fails?|failed)\b"
                         r"|(?<![가-힣])안(?=\s)|않|(?<![가-힣])못(?=\s|하|했|한)|없|아니")


def locate_quote(quote: str, e: Evidence) -> str | None:
    """인용문이 원문 어디에 있는지 찾아 **원문 그대로의** 구간을 돌려준다. 정확히 없으면 보여 준 발췌에서
    difflib 비율 0.9 이상의 가장 비슷한 창. 없으면 None."""
    q = src.normalize(quote)
    if not q:
        return None
    for hay_src in (e.text, e.passage):
        hay, idx = src.normalize_map(hay_src)
        pos = hay.find(q)
        if pos >= 0:
            return re.sub(r"\s+", " ", hay_src[idx[pos]: idx[pos + len(q) - 1] + 1]).strip()
    hay, idx = src.normalize_map(e.passage)
    if not hay or len(q) > len(hay) + 5:
        return None
    step = max(1, len(q) // 20)
    best, best_i = 0.0, -1
    for i in range(0, max(1, len(hay) - len(q) + 1), step):
        sm = difflib.SequenceMatcher(None, q, hay[i:i + len(q)])
        if sm.real_quick_ratio() >= 0.9 and sm.quick_ratio() >= 0.9:
            r = sm.ratio()
            if r > best:
                best, best_i = r, i
    if best < 0.9:
        return None
    end = min(len(hay), best_i + len(q)) - 1
    return re.sub(r"\s+", " ", e.passage[idx[best_i]: idx[end] + 1]).strip()


def fact_diff(quote: str, window: str) -> str:
    """LLM 인용문과 원문 구간의 수치 집합·부정 표현이 다르면 사유."""
    nq, nw = src.normalize(quote), src.normalize(window)
    if set(_numbers(nq)) != set(_numbers(nw)):
        return f"수치가 원문과 다르다(인용 {sorted(set(_numbers(nq)))} / 원문 {sorted(set(_numbers(nw)))})"
    if sorted(NEGATION_RE.findall(nq)) != sorted(NEGATION_RE.findall(nw)):
        return "부정 표현이 원문과 다르다"
    return ""


# 버전 붙은 이름: RLDX-1, GPT-5, Qwen3, pi0.5 / 대문자 약어 + 3~5자리 번호: RTX 5090 (LIBERO 97.8 같은 점수는 아님)
IDENT_RE = re.compile(r"[A-Za-z][A-Za-z0-9]*[-_]?\d[\w.\-]*|\b[A-Z]{2,4}\s\d{3,5}\b(?![.,%]?\d|%)")
MONTH_RE = re.compile(r"\b(" + "|".join(sorted(MONTHS, key=len, reverse=True)) + r")\b\.?")


def _month_numbers(window: str) -> list[str]:
    """날짜·연도에 붙은 월 이름만 숫자로 본다: "May 7", "7 May", "May 2026", "May, 2026"."""
    out = []
    for m in MONTH_RE.finditer(window):
        before, after = window[max(0, m.start() - 4):m.start()], window[m.end():m.end() + 7]
        if re.match(r"\s*,?\s*\d{1,4}\b", after) or re.search(r"\b\d{1,2}\s*$", before):
            out.append(str(MONTHS[m.group(1)]))
    return out


def _squash(text: str) -> str:
    return re.sub(r"[\s\-_]", "", src.normalize(text))


def missing_facts(claim_text: str, window: str) -> list[str]:
    """"확인"에 필요한 것: 주장의 버전 붙은 이름(GPT-5, Qwen3, RTX 5090)과 그 밖의 수치가 원문 구간에 모두 있다."""
    claim_text = claim_text or ""
    w = src.normalize(window).replace(",", "")
    squashed = _squash(window)
    missing = [ident for ident in dict.fromkeys(m.group(0) for m in IDENT_RE.finditer(claim_text))
               if _squash(ident) not in squashed]
    have = set(_numbers(w)) | set(_month_numbers(w))
    rest = IDENT_RE.sub(" ", claim_text)
    missing += [n for n in dict.fromkeys(_numbers(rest)) if n not in have]
    return missing


UNKNOWN = ("insufficient", "검색·취득이 모두 성공했는지 알 수 없다")


ATTR_HEAD_CHARS = 3000   # 저자·소속을 찾는 본문 앞부분
REFS_HEAD_RE = re.compile(r"^\s*(?:#+\s*)?(?:\d+\.?\s*)?(?:references|bibliography|참고\s*문헌)\s*:?\s*$", re.I | re.M)
PARTICLES = "은는이가의을를과와에서도로께"


def _is_hangul(ch: str) -> bool:
    return "가" <= ch <= "힣"


def _token_pattern(token: str) -> str:
    """낱말 경계: 영숫자 끝은 영숫자와 붙지 않게, 한글 끝은 앞에 한글 음절이 없고 뒤는 한글이 아니거나 조사."""
    def ascii_alnum(ch):
        return ch.isascii() and ch.isalnum()
    left = "(?<![a-z0-9])" if ascii_alnum(token[0]) else "(?<![가-힣])" if _is_hangul(token[0]) else ""
    right = ("(?![a-z0-9])" if ascii_alnum(token[-1])
             else f"(?=[^가-힣]|[{PARTICLES}]|$)" if _is_hangul(token[-1]) else "")
    return left + re.escape(token) + right


def attributable(e: Evidence, window: str, attribution: list[str], aliases=()) -> bool:
    """원문이 발표자 측 자료인가: 사용자 자료이거나, 제목·본문 앞 3,000자(참고문헌 제목 앞까지)·찾은 원문 구간에
    발표자 측 표기가 있다(정규화 후 대소문자 무시, 낱말 경계). 참고문헌·본문 뒤쪽의 비교 언급("KAIST baseline")은 세지 않는다.
    영문 표기는 3자 이상만(`aliases` 에 적은 것은 2자도), 그 밖은 2자 이상."""
    if e.user:
        return True
    head = e.text[:ATTR_HEAD_CHARS]
    if (m := REFS_HEAD_RE.search(head)) is not None:
        head = head[:m.start()]
    hay = src.normalize(f"{e.title}\n{head}\n{window}")
    tokens = [(src.normalize(a), True) for a in aliases] + [(src.normalize(a), False) for a in attribution]
    return any(re.search(_token_pattern(t), hay) for t, alias in tokens
               if len(t) >= 2 and (alias or not t.isascii() or len(t) >= 3))


def excerpt(window: str, claim_text: str, width: int = 80) -> str:
    """원문 구간에서 주장의 수치·버전 이름이 처음 나오는 곳(없으면 원문의 첫 수치)을 가운데 둔 약 width 자.
    잘린 쪽은 낱말 경계에서 자르고 "…" 를 붙인다."""
    if len(window) <= width:
        return window
    idents = [m.group(0) for m in IDENT_RE.finditer(claim_text or "")]
    nums = re.findall(r"\d+(?:[.,]\d+)*", IDENT_RE.sub(" ", claim_text or ""))
    center = None
    for t in idents + nums:
        if (m := re.search(rf"(?<![0-9A-Za-z.]){re.escape(t)}(?![0-9])", window, re.I)) is not None:
            center = (m.start() + m.end()) // 2
            break
    if center is None:
        m = re.search(r"\d", window)
        center = m.start() if m else 0
    start = max(0, min(center - width // 2, len(window) - width))
    end = start + width
    piece = window[start:end]
    if end < len(window) and " " in piece[len(piece) // 2:]:
        piece = piece.rsplit(" ", 1)[0]
    if start > 0 and " " in piece[:len(piece) // 2]:
        piece = piece.split(" ", 1)[1]
    return ("…" if start > 0 else "") + piece.strip() + ("…" if end < len(window) else "")


def assess(claim: dict, verdict: dict, evidence: list[Evidence], unclean: tuple[str, str] | None = UNKNOWN, *,
           attribution: list[str], aliases=()) -> dict:
    """판정 + 기계 검사 → 내부 상태·사유. quote 에는 LLM 인용이 아니라 원문 구간을 남긴다.
    unclean: gather 의 검색·취득 실패(None = 모두 성공). 모르면 "없음" 판정을 확정하지 않는다.
    attribution: 발표자 측 표기(발표자·소속), aliases: event.yaml 의 별칭. subject 가 own 인 주장은 원문이 발표자 측
    자료일 때만 확인·다름으로 둔다(다른 팀의 같은 벤치마크 수치와 견주지 않게). 빈 목록이면 사용자 자료만 통과한다."""
    v = verdict.get("verdict")
    sid = str(verdict.get("source_id") or "").strip()
    llm_quote = str(verdict.get("quote") or "").strip()
    note = str(verdict.get("note") or "").strip()
    conf = str(verdict.get("confidence") or "").strip()
    base = {"verdict": v, "source_id": sid, "quote": "", "llm_quote": llm_quote, "note": note, "confidence": conf}
    by_id = {e.sid: e for e in evidence}
    if v == "없음":
        if unclean:  # 검색·취득이 일부 실패했으면 "못찾음"으로 확정하지 않는다
            return {**base, "status": unclean[0],
                    "reason": f"읽은 소스 {len(evidence)}건은 주장을 다루지 않지만 {unclean[1]}"}
        return {**base, "status": "search_ok_zero",
                "reason": f"읽은 소스 {len(evidence)}건이 주장의 대상을 다루지 않는다(판정)"}
    if v == "불충분":
        return {**base, "status": "insufficient", "reason": f"근거 불충분(부분 일치 등): {note}"}
    e = by_id.get(sid)
    if e is None:
        return {**base, "status": "insufficient", "reason": f"source_id {sid or '(없음)'} 가 입력 소스에 없다"}
    if len(src.normalize(llm_quote)) < MIN_QUOTE:
        return {**base, "status": "insufficient", "reason": f"인용문이 너무 짧다({len(llm_quote)}자)"}
    window = locate_quote(llm_quote, e)
    if window is None:
        return {**base, "status": "insufficient", "reason": f"인용문 불일치: {sid} 원문에서 찾지 못했다"}
    diff = fact_diff(llm_quote, window)
    if diff:
        return {**base, "status": "insufficient", "reason": f"인용문 불일치: {diff}"}
    base["quote"] = window
    if v == "확인":
        miss = missing_facts(claim["claim"], window)
        if miss:
            return {**base, "status": "insufficient",
                    "reason": f"근거 불충분: 원문 구간에 주장의 수치·이름이 없다({', '.join(miss)})"}
    if claim_subject(claim) == "own" and not attributable(e, window, attribution, aliases):
        who = "·".join(dict.fromkeys(a.strip() for a in attribution if a.strip()))
        side = f"발표자 측({who})" if who else "발표자 측"
        return {**base, "status": "insufficient", "gate": "attribution",
                "reason": f"원문이 {side} 자료로 확인되지 않는다 — 다른 팀의 결과일 수 있어 '확인/다름'으로 두지 않는다 "
                          f"(원문값: {excerpt(window, claim['claim'])})"}
    return {**base, "status": "supported" if v == "확인" else "conflicting", "reason": ""}


# 실행 ------------------------------------------------------------------------

def claim_hash(claim: dict) -> str:
    return llm_mod.sha256(json.dumps({k: claim.get(k) for k in ("claim", "paragraph_ids", "queries", "keywords")},
                                     ensure_ascii=False, sort_keys=True))[:12]


def speaker_side(talk: Talk) -> tuple[list[str], list[str]]:
    """(발표자·소속, event.yaml 별칭): 판정 입력의 `발표자:` 줄과 own 주장의 귀속 검사에 쓴다."""
    return [talk.speaker, talk.org], list(talk.aliases)


def judge_key(claim: dict, tokens: list[str]) -> str:
    """이전 판정을 다시 쓸 조건(claim_hash 밖): subject 와, own 이면 발표자 측 표기(발표자·소속·별칭)의 해시.
    claims 의 subject 나 event.yaml 의 aliases 를 고치면 다시 판정한다."""
    subject = claim_subject(claim)
    side = sorted({t.strip() for t in tokens if t and t.strip()}) if subject == "own" else []
    return f"{subject}:{llm_mod.sha256(json.dumps(side, ensure_ascii=False))[:12]}"


def _judge(llm, name_prefix: str, items: list[tuple[dict, list[Evidence]]], pending: list,
           speaker: str = "") -> dict[str, dict]:
    system = llm_mod.prompt("verify")
    out: dict[str, dict] = {}
    for k in range(0, len(items), JUDGE_BATCH):
        batch = items[k:k + JUDGE_BATCH]
        ids = [c["id"] for c, _ in batch]
        user = "\n\n".join(["[판정할 주장과 원문 발췌]", *(judge_block(c, ev, speaker) for c, ev in batch)])
        got = llm_mod.collect_pending(
            lambda: llm.ask(f"{name_prefix}-{k // JUDGE_BATCH + 1}", system, user, json_mode=True,
                            validate=lambda t: validate_verdicts(t, ids)), pending)
        if got is not None:
            out.update(got)
    return out


def user_sources_key(work_dir: Path, nn: str) -> str:
    """사용자 자료 목록의 해시. 바뀌면(sources add) 확인이 아닌 판정을 다시 한다."""
    items = [{k: e.get(k) for k in ("id", "target", "status", "doc_hash")} for e in src.load_manifest(work_dir, nn)]
    return llm_mod.sha256(json.dumps(items, ensure_ascii=False, sort_keys=True))[:12]


def previous_results(event: Event, talk: Talk, redo_all: bool, ukey: str) -> dict[str, dict]:
    """재실행 때 그대로 둘 이전 판정(claim id → 행). 사용자 자료가 바뀌었으면 "확인"만 남긴다."""
    old = load_verify(event, talk)
    if not old or redo_all:
        return {}
    keep = FINAL_STATUSES if old.get("user_sources_key") == ukey else ("supported",)
    return {c["id"]: c for c in old.get("claims", []) if c.get("status") in keep}


def redo_claims(event: Event, talk: Talk, claims: list[dict], redo_all: bool = False) -> list[dict]:
    """이번 verify 에서 다시 검색·판정할 claim(--plan 용, 사용자 자료 재취득 전 기준)."""
    prev = previous_results(event, talk, redo_all, user_sources_key(event.work_dir, talk.nn))
    tokens = sum(speaker_side(talk), [])
    return [c for c in claims if not (prev.get(c["id"]) and prev[c["id"]].get("claim_hash") == claim_hash(c)
                                      and prev[c["id"]].get("judge_key") == judge_key(c, tokens))]


def _internal_error(e: Exception) -> dict:
    return {"status": "internal_error", "reason": f"내부 오류: {type(e).__name__}: {str(e)[:160]}", "verdict": "",
            "source_id": "", "quote": "", "note": "", "confidence": ""}


def run_verify(event: Event, talk: Talk, llm, searcher: src.Sources, redo_all: bool = False,
               progress=print) -> dict:
    claims = load_claims(event, talk)
    src.retry_user_sources(event.work_dir, talk.nn, http=searcher.http)
    user = src.user_docs(event.work_dir, talk.nn)
    ukey = user_sources_key(event.work_dir, talk.nn)
    # 사용자 자료가 바뀌었으면 "확인"만 남기고 나머지(다름·못찾음 포함)는 새 자료로 다시 본다
    previous = previous_results(event, talk, redo_all, ukey)
    # 발표자 측 표기: 판정 입력에 보여 주고, own 주장의 확인·다름은 원문에 이 표기가 있어야 남긴다
    attribution, aliases = speaker_side(talk)
    tokens = attribution + aliases
    speaker = talk.speaker + (f" ({talk.org})" if talk.org else "") + (
        f" · 별칭 {', '.join(aliases)}" if aliases else "")
    rows: dict[str, dict] = {}
    judged: list[tuple[dict, list[Evidence]]] = []
    gathered: dict[str, Gathered] = {}
    for c in claims:
        h = claim_hash(c)
        prev = previous.get(c["id"])
        if prev and prev.get("claim_hash") == h and prev.get("judge_key") == judge_key(c, tokens):
            rows[c["id"]] = prev
            continue
        try:
            g = gather(c, searcher, user)
        except Exception as e:  # claim 하나의 예외는 그 claim 만 "확인하지 않은 것"으로
            rows[c["id"]] = _internal_error(e)
            progress(f"{c['id']}: {rows[c['id']]['reason']}")
            continue
        gathered[c["id"]] = g
        failed = sorted({f"{s['provider']}({s['error'].split(' (')[0][:24]})" for s in g.searches if not s["ok"]})
        progress(f"{c['id']}: 검색 {sum(s['ok'] for s in g.searches)}/{len(g.searches)} 성공, "
                 f"발췌 {len(g.evidence)}건" + (f" → {g.status}" if not g.evidence else "")
                 + (f" · 실패 {', '.join(failed)}" if failed else ""))
        if g.evidence:
            judged.append((c, g.evidence))
        else:
            rows[c["id"]] = {"status": g.status, "reason": g.reason, "verdict": "", "source_id": "", "quote": "",
                             "note": "", "confidence": ""}

    pending: list = []
    first = _judge(llm, f"verify-{talk.nn}", judged, pending, speaker)
    second_items = []
    for c, ev in judged:
        if c["id"] not in first:
            continue
        try:
            res = assess(c, first[c["id"]], ev, gathered[c["id"]].unclean, attribution=attribution, aliases=aliases)
        except Exception as e:
            res = _internal_error(e)
        res["pass"] = 1
        rows[c["id"]] = res
        if first[c["id"]].get("verdict") == "불충분" and any(len(e.text) > src.PASSAGE_CHARS for e in ev):
            kws = claim_keywords(c)
            wide = [Evidence(e.sid, e.url, e.title, e.doc_hash, src.relevant_passage(e.text, kws,
                                                                                     src.WIDE_PASSAGE_CHARS)[0],
                             e.text, e.path, e.kind, e.user) for e in ev]
            second_items.append((c, wide))
    if not pending and second_items:
        second = _judge(llm, f"verify-{talk.nn}-r2", second_items, pending, speaker)
        for c, ev in second_items:
            if c["id"] in second:
                try:
                    res = assess(c, second[c["id"]], ev, gathered[c["id"]].unclean, attribution=attribution,
                                 aliases=aliases)
                except Exception as e:
                    res = _internal_error(e)
                res["pass"] = 2
                rows[c["id"]] = res
                gathered[c["id"]].evidence = ev
    if pending:
        raise llm_mod.ManualPending(pending, event.root)

    out_claims = []
    for c in claims:
        r = rows[c["id"]]
        if c["id"] in gathered:
            g = gathered[c["id"]]
            r = {**r, "sources": [e.meta() for e in g.evidence], "searches": g.searches, "fetches": g.fetches}
        src_meta = next((s for s in r.get("sources", []) if s["id"] == r.get("source_id")), None)
        out_claims.append({**c, **r, "subject": claim_subject(c), "claim_hash": claim_hash(c),
                           "judge_key": judge_key(c, tokens),
                           "category": category(r["status"]), "source": src_meta})
    data = {"talk": talk.nn, "date": context.today(), "backend": llm.label,
            "providers": searcher.providers, "provider_notes": searcher.notes,
            "disabled": searcher.disabled, "user_sources": len(user), "user_sources_key": ukey, "claims": out_claims,
            "counts": {cat: sum(c["category"] == cat for c in out_claims) for cat in CATEGORIES}}
    context.write_atomic(verify_json_path(event, talk), json.dumps(data, ensure_ascii=False, indent=1))
    md_path = verify_md_path(event, talk)
    if md_path.exists() and not md_path.read_text(encoding="utf-8").startswith(MD_MARKER):
        keep = md_path.with_name(f"verify-{talk.nn}.manual.md")
        if not keep.exists():
            md_path.rename(keep)
            data["kept_manual"] = event.rel(keep)
    context.write_atomic(md_path, render_md(event, talk, data))
    return data


# work/verify-NN.md -------------------------------------------------------------

MD_MARKER = "<!-- lecture-recon verify: 자동 생성. 손으로 고치면 다음 verify 가 덮어쓴다 -->"


def cell(text) -> str:
    return re.sub(r"\s+", " ", str(text or "")).replace("|", "\\|").strip() or "-"


def render_md(event: Event, talk: Talk, data: dict) -> str:
    counts = data["counts"]
    lines = [
        MD_MARKER,
        f"# 원문 대조: {talk.nn} {talk.speaker} ({event.name}, {event.date}) — 검증일 {data['date']}",
        "",
        f"방법: `lecture-recon verify` ({data['backend']}). 검색: {', '.join(data['providers']) or '없음'}"
        + (f" + 사용자 자료 {data['user_sources']}건" if data["user_sources"] else "") + ". "
        "CLI 가 인용문을 캐시된 원문에서 다시 찾은 것만 \"확인\"·\"다름\"으로 둔다. "
        "검색·취득·추출 실패, 근거 불충분, 인용문 불일치는 \"확인하지 않은 것\"에 사유와 함께 둔다.",
    ]
    if data.get("provider_notes") or data.get("disabled"):
        notes = list(data.get("provider_notes") or []) + [f"{k}: {v}" for k, v in (data.get("disabled") or {}).items()]
        lines.append("검색 소스 메모: " + "; ".join(notes))
    lines += ["", " · ".join(f"{cat} {counts[cat]}" for cat in CATEGORIES) + f" (총 {len(data['claims'])})", ""]
    for cat in CATEGORIES:
        rows = [c for c in data["claims"] if c["category"] == cat]
        lines += [f"## {cat}", ""]
        if not rows:
            lines += ["없음.", ""]
            continue
        lines += ["| claim | 문단 | 종류 | 주장 | 원문(인용) | 근거 | 신뢰도 | 사유·비고 |", "|---|---|---|---|---|---|---|---|"]
        for c in rows:
            s = c.get("source") or {}
            where = f"{s.get('title', '')} {s.get('url', '')}".strip() if s else ""
            proven = c["status"] in ("supported", "conflicting")
            if c.get("gate") == "attribution":     # 인용은 원문에서 찾았고 귀속만 확인되지 않았다
                unproven = f"원문 구간(귀속 미확인): {c.get('quote')}"
            else:
                unproven = f"제시된 인용(검증 안 됨): {c['llm_quote']}" if c.get("llm_quote") and not proven else ""
            basis = ("교정된 구간 기반([추정] 교정과 겹치는 전사 인용)" if c.get("from_correction") else "",
                     "불명확 구간 기반(신뢰도 낮음)" if c.get("low_confidence") else "")
            remark = " / ".join(x for x in (*basis, c.get("note"), c.get("reason"), unproven, c.get("status")) if x)
            lines.append(f"| {c['id']} | {', '.join(c['paragraph_ids'])} | {c['kind']} | {cell(c['claim'])} | "
                         f"{cell(c.get('quote') if proven else '')} | {cell(where if proven else '')} | "
                         f"{cell(c.get('confidence') if proven else '')} | {cell(remark)} |")
        lines.append("")
    return "\n".join(lines)
