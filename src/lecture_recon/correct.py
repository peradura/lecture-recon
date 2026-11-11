"""`correct NN`: LLM 의 JSON 패치(장 제목·치환)를 검사해 전사에 적용한다.

문단 ID·타임스탬프·`<a id>` 줄은 코드로 불변을 검증하고, 근거 규칙을 어긴 치환은 버리고 사유를 남긴다.
적용 전 원본은 work/NN_draftN.md 로 백업, 적용·거부 목록은 work/correct/NN.json.
"""

from __future__ import annotations

import json
import re
import shutil
from collections import Counter
from dataclasses import dataclass, field
from typing import Callable

from . import context, llm as llm_mod, scaffold, sources, transcript
from .config import Event, LectureReconError, Talk

OFFICIAL_PREFIX = "공식 자료"
OTHER_RECORDING_PREFIX = "다른 녹음"
OFFICIAL_MISMATCH = "공식 자료 근거가 등록된 출처와 맞지 않는다"
OFFICIAL_NOT_FOUND = "공식 자료 본문에서 확인하지 못했다"
URL_RE = re.compile(r"https?://[^\s<>\"'()\[\]]+")
GUESS_RE = re.compile(r"^\[추정:\s*(.*?)\]$", re.S)
SOURCE_REASON_RE = re.compile(r"원문|논문|보고서")
# 문단 본문 전체에서 바뀌면 안 되는 것(근거 없이는): 숫자열, 한글 수사, 단위, 부정어, [불명확 표시
NUMBER_SEQ_RE = re.compile(r"\d+(?:[.,]\d+)*")
KOREAN_NUMBER_RE = re.compile(r"(?<![가-힣])(?:[일이삼사오육칠팔구]?[십백천만억])+[일이삼사오육칠팔구]?")
UNIT_RE = re.compile(r"(?:\d|[십백천만억])\s*(ms|Hz|GHz|GB|TB|MB|KB|km|kg|cm|mm|%|퍼센트|배|초|분|시간|개|명|대|달러|원|년|월|일)")
NEGATION_RE = re.compile(r"(?<![가-힣])안(?=\s)|않|(?<![가-힣])못(?=\s|하|했|한)|없|아니")
SLIDE_EVIDENCE_RE = re.compile(r"^슬라이드\s*(\d+)")
EVENT_INFO_EVIDENCE_RE = re.compile(r"^\[?\s*행사\s*정보\s*\]?")          # "[행사 정보] 별칭 RLWRLD" = event.yaml 문자열
USER_EVIDENCE_RE = re.compile(r"^\[?\s*사용자\s*자료\s*\]?\s*:?\s*(U\d+)\b", re.I)  # "[사용자 자료] U1"
USER_GUARDED = ("사용자 자료 근거는 숫자·부정어·단위·`[불명확` 변경에 쓰지 않는다(숫자가 그 자료 용어 안에만 있으면 예외) — "
                "그런 변경은 '슬라이드 NN'·'공식 자료'(event.yaml sources URL)·'다른 녹음'만 인정한다")
NAME_DIGITS_NOTE = "숫자는 별칭/자료 용어 안"
# 사용자 자료 용어: 숫자가 있거나 둘째 글자부터 대문자가 있는 영문 표기(RLDX-1, RLWRLD, RoboCasa, Qwen3, pi0.5, H200)
TERM_RE = re.compile(r"(?<![A-Za-z0-9])[A-Za-z][A-Za-z0-9]*(?:-[A-Za-z0-9]+)*(?:(?<=\d)\.\d+)?")
TERM_STOPWORDS = frozenset("HTML HTTP HTTPS PDF URL URI API GPU CPU AI USA JSON XML CSS DOI ISBN ISSN PNG JPG JPEG SVG "
                           "FAQ CEO CTO CFO PHD USD KRW EUR LLC INC LTD WWW COM ORG NET TBD ETC FIG TABLE".split())
USER_TERMS_MAX = 60
PARA_LINE_RE = re.compile(r"^(\*\*\[(\d{2,}-\d{3,})\] \[\d{2,}:\d{2}:\d{2}\]\*\*\s?)(.*)$")
ROUND_RE = re.compile(r"교정: (\d+)차 \([^)\n]*\)")
DRAFT_NOTICE_RE = re.compile(r"^> 기계 전사 초안이다.*$", re.M)
QUALITY_PLACEHOLDER = transcript.QUALITY_PLACEHOLDER
CORRECTED_NOTICE = ("> 기계 전사 후 교정본이다. 고유명사·용어는 슬라이드·원문·공식 정보로 뒷받침될 때만 고쳤다. "
                    "불명확한 발화는 `[불명확]`로 두고 지어내지 않았다.")
CORRECTED_NOTICE_GUESS = ("> 기계 전사 후 교정본이다. 고유명사·용어는 슬라이드·원문·공식 정보로 뒷받침될 때만 고쳤다. "
                          "근거가 약한 교정은 `[추정: …]`로 표시했다. 불명확한 발화는 `[불명확]`로 두고 지어내지 않았다.")
STRUCTURE_TOKENS = ("\n", "**[", "<a ", "## ")


@dataclass
class Block:
    pid: str
    line: int          # 문단 줄(0부터)
    end: int           # 이어지는 줄 끝(미포함)
    prefix: str        # `**[NN-MMM] [HH:MM:SS]** `


def blocks(lines: list[str]) -> dict[str, Block]:
    out: dict[str, Block] = {}
    i = 0
    while i < len(lines):
        m = PARA_LINE_RE.match(lines[i])
        if not m:
            i += 1
            continue
        j = i + 1
        while j < len(lines) and lines[j].strip() and not lines[j].startswith("#") \
                and not transcript.ANCHOR_RE.search(lines[j]) and not PARA_LINE_RE.match(lines[j]):
            j += 1
        out.setdefault(m.group(2), Block(m.group(2), i, j, m.group(1)))
        i = j
    return out


def body_of(lines: list[str], b: Block) -> str:
    return "\n".join([lines[b.line][len(b.prefix):], *lines[b.line + 1:b.end]])


def invariants(text: str) -> tuple:
    pt = transcript.parse(text)
    anchor_lines = [line for line in text.splitlines() if transcript.ANCHOR_RE.search(line)]
    prefixes = [m.group(1) for line in text.splitlines() if (m := PARA_LINE_RE.match(line))]
    return [(p.id, p.start) for p in pt.paragraphs], sorted(pt.anchors), anchor_lines, prefixes


@dataclass
class Official:
    """근거로 인정하는 출처. "공식 자료": event.yaml `sources` 의 URL(보호 항목 변경은 그 URL 본문에 고친 표기가 있어야
    한다)과 event.yaml 의 문자열(행사명, 발표 제목·발표자·소속·별칭; 보호 항목이 아닌 변경만). "다른 녹음": 녹음이 둘
    이상일 때만. "[사용자 자료] U1": work/sources/user/NN 의 자료(사용자가 등록) — 고친 표기가 그 본문에 있고 보호 항목이
    아닌 변경만."""
    urls: set[str] = field(default_factory=set)
    strings: list[str] = field(default_factory=list)
    user_texts: dict[str, str] = field(default_factory=dict)   # 사용자 자료 id → 캐시된 본문
    aliases: list[str] = field(default_factory=list)           # event.yaml 별칭(숫자 든 이름을 인정하는 근거)
    recordings: int = 1
    fetch: Callable[[str], str | None] | None = None   # URL → 본문(캐시 work/sources/). 없거나 실패면 None
    originals: dict[str, str] = field(default_factory=dict)
    _texts: dict[str, str | None] = field(default_factory=dict, repr=False)

    def text(self, key: str) -> str | None:
        if key not in self._texts:
            self._texts[key] = self.fetch(self.originals.get(key, key)) if self.fetch else None
        return self._texts[key]


def _url_key(url: str) -> str:
    return url.strip().rstrip(".,;:)]}'\"").rstrip("/").lower()


def registered_urls(event: Event) -> list[str]:
    """교정 근거로 인정하는 URL: event.yaml `sources` 만."""
    return list(dict.fromkeys(u.strip() for u in event.sources if re.match(r"^https?://", u.strip(), re.I)))


def official_refs(event: Event, talk: Talk, http: Callable | None = None) -> Official:
    urls = registered_urls(event)
    searcher = sources.Sources(event.work_dir, providers=[], http=http or sources.http_get, env={})

    def fetch(url: str) -> str | None:
        doc = searcher.fetch_url(url)
        return doc.text if doc.ok else None

    strings = [event.name, *(x for t in event.talks for x in (t.title, t.speaker, t.org, *t.aliases))]
    users = {sid: text for sid, (_, text) in sources.user_texts(event.work_dir, talk.nn).items()}
    aliases = list(dict.fromkeys(a for t in event.talks for a in t.aliases))
    return Official({_url_key(u) for u in urls}, [x for x in dict.fromkeys(str(v or "").strip() for v in strings) if x],
                    users, aliases, recordings=len(talk.recordings), fetch=fetch,
                    originals={_url_key(u): u for u in urls})


def user_terms(text: str, limit: int | None = USER_TERMS_MAX) -> list[str]:
    """사용자 자료 본문의 고유명사 모양 영문 표기(3자 이상, 숫자가 있거나 둘째 글자부터 대문자). 일반 약어(HTML·PDF·GPU…)는
    뺀다. 대소문자만 다른 것은 하나로(가장 자주 쓴 꼴), 자주 나온 순(같으면 먼저 나온 순)으로 limit 개."""
    counts: dict[str, Counter] = {}
    for m in TERM_RE.finditer(text or ""):
        t = m.group(0)
        up = t.upper()
        if len(t) < 3 or up in TERM_STOPWORDS or (up.endswith("S") and up[:-1] in TERM_STOPWORDS):
            continue
        if any(c.isdigit() for c in t) or any(c.isupper() for c in t[1:]):
            counts.setdefault(t.lower(), Counter())[t] += 1
    ranked = sorted(counts.values(), key=lambda c: -sum(c.values()))
    return [c.most_common(1)[0][0] for c in ranked[:limit]]


def user_terms_block(event: Event, talk: Talk) -> str:
    """[사용자 자료 용어] 블록. 사용자 자료(용어가 하나라도 있는 것)가 없으면 빈 문자열."""
    rows = [f"- {sid} ({context.data_safe(title)}): {', '.join(terms)}"
            for sid, (title, text) in sources.user_texts(event.work_dir, talk.nn).items()
            if (terms := user_terms(text))]
    if not rows:
        return ""
    return "\n".join(["[사용자 자료 용어] 원문 대조용으로 등록된 자료에서 뽑은 고유명사. 전사의 음성 오인식이 이 용어로 보이면 "
                      "[추정: …] 또는 일반 교정으로 제안하고 evidence 에 '[사용자 자료] U1' 처럼 자료 ID 를 적는다. "
                      "숫자·부정어·단위·`[불명확` 이 바뀌는 교정에는 쓸 수 없다.", *rows])


EDGE_PUNCT = ".,;:!?·\"'()[]{}「」『』<>“”‘’"


def _strip_josa(token: str) -> str:
    m = sources.JOSA_RE.search(token)
    return token[:m.start()] if m and m.start() > 0 else token


def _tokens(text: str) -> list[str]:
    out = [t.strip(EDGE_PUNCT) for t in sources.normalize(text).split()]
    out = [t for t in out if t]
    if out and sources.JOSA_RE.fullmatch(out[-1]):  # "From Data to Driving 을" 의 떨어진 조사
        out = out[:-1]
    return [_strip_josa(t) for t in out]


def changed_part(old: str, new: str) -> str:
    """new 에서 old 와 다른 부분(공통 앞뒤를 뺀 것)을 단어 경계까지 넓힌 것. `[추정: …]` 은 벗긴다."""
    m = GUESS_RE.match(new.strip())
    new = m.group(1) if m else new
    p = 0
    while p < min(len(old), len(new)) and old[p] == new[p]:
        p += 1
    q = 0
    while q < min(len(old), len(new)) - p and old[-1 - q] == new[-1 - q]:
        q += 1
    a, b = p, len(new) - q
    while a < b and new[a].isspace():
        a += 1
    while b > a and new[b - 1].isspace():
        b -= 1
    if a >= b:
        return ""
    while a > 0 and not new[a - 1].isspace():
        a -= 1
    while b < len(new) and not new[b].isspace():
        b += 1
    return new[a:b].strip().strip(EDGE_PUNCT)


def _in_text(part: str, text: str) -> bool:
    np_, nt = sources.normalize(part), sources.normalize(text)
    return bool(np_) and (np_ in nt or np_.replace(" ", "") in nt.replace(" ", ""))


def _string_attested(part: str, evidence: str, strings: list[str]) -> bool:
    """고친 부분이 evidence 에 그대로 인용된 event.yaml 문자열의 단어(들)와 같다(3자 이상, 글자 포함)."""
    pt = _tokens(part)
    joined = "".join(pt)
    if len(joined) < 3 or not any(c.isalpha() for c in joined):
        return False
    ev = sources.normalize(evidence)
    for x in strings:
        if sources.normalize(x) not in ev:
            continue
        xt = _tokens(x)
        if any(xt[i:i + len(pt)] == pt for i in range(len(xt) - len(pt) + 1)):
            return True
    return False


def evidence_verdict(evidence: str, old: str, new: str, guarded: bool, slides: set[int] | None = None,
                     official: Official | None = None) -> tuple[bool, str, str]:
    """(근거 인정, 거부 사유, 메모). 거부 사유가 있으면 버린다. 인정 안 됨 + 메모면 추정으로만 적용한다."""
    evidence = evidence.strip()
    m = SLIDE_EVIDENCE_RE.match(evidence)
    if m:
        return int(m.group(1)) in (slides or set()), "", ""
    if evidence.startswith(OTHER_RECORDING_PREFIX):
        if official is not None and official.recordings > 1:
            return True, "", ""
        if guarded:
            return False, "'다른 녹음' 근거는 녹음이 둘 이상인 발표에만 인정한다", ""
        return False, "", "'다른 녹음' 근거는 녹음이 둘 이상인 발표에만 인정한다 — 추정으로 적용"
    if (u := USER_EVIDENCE_RE.match(evidence)) is not None:
        if guarded:
            return False, USER_GUARDED, ""
        sid = u.group(1).upper()
        text = official.user_texts.get(sid) if official is not None else None
        part = changed_part(old, new)
        if text is not None and part and _in_text(part, text):
            return True, "", ""
        why = (f"사용자 자료 {sid} 가 없다(`sources add` 로 등록·추출된 자료만)" if text is None
               else f"사용자 자료 {sid} 본문에 {part or '고친 표기'!r} 가 없다")
        return False, "", f"{why} — 추정으로 적용"
    if not (evidence.startswith(OFFICIAL_PREFIX) or EVENT_INFO_EVIDENCE_RE.match(evidence)):
        return False, "", ""
    part = changed_part(old, new)
    keys = [k for k in (_url_key(u) for u in URL_RE.findall(evidence)) if official and k in official.urls]
    if keys:
        text = official.text(keys[0])
        if text is not None and part and _in_text(part, text):
            return True, "", ""
        why = f"{OFFICIAL_NOT_FOUND}({official.originals.get(keys[0], keys[0])}" + (
            ": 본문을 가져오지 못했다)" if text is None else f" 에 {part or '고친 표기'!r} 가 없다)")
        return (False, why, "") if guarded else (False, "", f"{why} — 추정으로 적용")
    if guarded:
        return False, (f"{OFFICIAL_MISMATCH} — 숫자·부정어·단위·`[불명확` 변경의 '공식 자료' 근거는 event.yaml "
                       "sources 의 URL 이어야 하고, 고친 표기가 그 본문에 있어야 한다"), ""
    if official is not None and _string_attested(part, evidence, official.strings):
        return True, "", ""
    return False, "", f"{OFFICIAL_MISMATCH} — 근거 없음으로 보고 추정으로 적용"


def name_digits_only(evidence: str, old: str, new: str, official: Official | None) -> bool:
    """숫자 변경이 이름 안의 숫자뿐인가(RLDX-1, Qwen3, H200): evidence 가 `[행사 정보]` 별칭 또는 `[사용자 자료] U<n>` 이고
    고친 부분이 그 근거에 있으며, old 에 숫자가 없고, new 에서 그 근거가 보증하는 표기(event.yaml 별칭 / 그 자료의 용어,
    낱말 경계에서 글자 그대로)를 지우면 숫자가 남지 않는다. 그 밖의 숫자 변경은 지금처럼 보호 항목이다."""
    if official is None or re.search(r"\d", old):
        return False
    part = changed_part(old, new)
    if (u := USER_EVIDENCE_RE.match(evidence.strip())) is not None:
        text = official.user_texts.get(u.group(1).upper())
        if text is None or not part or not _in_text(part, text):
            return False
        names = user_terms(text, limit=None)
    elif EVENT_INFO_EVIDENCE_RE.match(evidence.strip()):
        if not _string_attested(part, evidence, official.aliases):
            return False
        names = official.aliases
    else:
        return False
    rest = new
    for name in sorted(names, key=len, reverse=True):
        rest = re.sub(rf"(?<![A-Za-z0-9]){re.escape(name)}(?![A-Za-z0-9])", " ", rest)
    return not re.search(r"\d", rest)


def guarded_changes(before: str, after: str) -> list[str]:
    """문단 본문 전체를 비교해 바뀐 보호 항목."""
    out = []
    if NUMBER_SEQ_RE.findall(before) != NUMBER_SEQ_RE.findall(after):
        out.append("숫자")
    if KOREAN_NUMBER_RE.findall(before) != KOREAN_NUMBER_RE.findall(after):
        out.append("한글 수사")
    if UNIT_RE.findall(before) != UNIT_RE.findall(after):
        out.append("단위")
    if NEGATION_RE.findall(before) != NEGATION_RE.findall(after):
        out.append("부정어")
    if before.count("[불명확") != after.count("[불명확"):
        out.append("`[불명확]` 표시")
    return out


def check_replacement(r: dict, body: str | None, slides: set[int] | None = None,
                      official: Official | None = None) -> tuple[str, str, str]:
    """('', level, note) 이면 통과(level = 일반|추정), 아니면 (거부 사유, '', '').
    "공식 자료" 근거가 등록된 출처와 맞지 않으면 근거 없음으로 본다: 보호 항목 변경이면 거부, 아니면 추정으로만."""
    old, new = r.get("old"), r.get("new")
    if not isinstance(old, str) or not isinstance(new, str):
        return "old/new 가 문자열이 아니다", "", ""
    reason, evidence = str(r.get("reason") or "").strip(), str(r.get("evidence") or "").strip()
    if body is None:
        return "문단 ID 가 전사(또는 이 묶음)에 없다", "", ""
    if not old:
        return "old 가 비어 있다", "", ""
    if old == new:
        return "old 와 new 가 같다", "", ""
    if not reason:
        return "reason 이 없다", "", ""
    if any(tok in new for tok in STRUCTURE_TOKENS) or "\n" in old:
        return "new/old 에 줄바꿈·`**[`·`<a `·`## ` 가 있다(구조 변경)", "", ""
    count = body.count(old)
    if count == 0:
        return "old 가 문단 본문에 없다", "", ""
    if count > 1:
        return f"old 가 문단 본문에 {count}번 나온다(모호)", "", ""
    if (old.count("[") - old.count("]")) != (new.count("[") - new.count("]")):
        return "대괄호 짝이 바뀐다(표시 구조 변경)", "", ""
    if SOURCE_REASON_RE.search(reason):  # 원칙 2: 근거가 무엇이든 "원문과 다르다"는 전사를 고칠 이유가 아니다
        return ("원문·논문·보고서와 다르다는 이유로는 전사를 고치지 않는다(원칙 2: 노트 '발언과 원문이 다른 것'에 적는다). "
                "슬라이드·발표자 공식 자료로 표기를 바로잡는 것이면 reason 에 그렇게 적는다"), "", ""
    guarded = guarded_changes(body, body.replace(old, new, 1))
    if guarded == ["숫자"] and name_digits_only(evidence, old, new, official):  # 다른 보호 항목은 그대로 검사한다
        return "", "추정" if "[추정" in new else "일반", NAME_DIGITS_NOTE
    ok, reject, note = evidence_verdict(evidence, old, new, bool(guarded), slides, official)
    if reject:
        return reject, "", ""
    if guarded and not ok:
        return (f"{'·'.join(guarded)} 변경은 evidence 가 '슬라이드 NN'(노트 슬라이드 표에 있는 번호)·'공식 자료'"
                "(event.yaml sources URL, 본문에 고친 표기)·'다른 녹음'(녹음이 둘 이상) 이어야 한다"), "", ""
    return "", "추정" if guarded or note or "[추정" in new else "일반", note


def clean_title(title) -> str:
    t = re.sub(r"\s+", " ", str(title or "")).strip().lstrip("#").strip()
    t = re.sub(r"^\[\d{2,}:\d{2}:\d{2}\]\s*", "", t)
    return t[:80]


@dataclass
class PatchResult:
    text: str
    applied: list[dict] = field(default_factory=list)
    rejected: list[dict] = field(default_factory=list)
    chapters: list[dict] = field(default_factory=list)
    chapters_rejected: list[dict] = field(default_factory=list)

    @property
    def unclear_resolved(self) -> int:
        return sum(a["old"].count("[불명확") - a["new"].count("[불명확") for a in self.applied)


def apply_patch(text: str, patch: dict, allowed: set[str] | None = None, slides: set[int] | None = None,
                official: Official | None = None) -> PatchResult:
    """패치를 적용한 새 전사와 적용·거부 목록. allowed = 이 패치가 다룰 수 있는 문단 ID(묶음),
    slides = 노트 슬라이드 표의 번호(evidence "슬라이드 NN" 검사용), official = "공식 자료" 로 인정할 출처
    (None 이면 "공식 자료" 근거를 인정하지 않는다)."""
    lines = text.splitlines()
    trailing_newline = text.endswith("\n")
    res = PatchResult(text)
    blks = blocks(lines)
    base = invariants(text)

    # 치환: 원래 본문 기준으로 검사·겹침 판정, 문단마다 뒤에서부터 적용
    accepted: dict[str, list[tuple[int, dict]]] = {}
    for r in patch.get("replacements") or []:
        if not isinstance(r, dict):
            res.rejected.append({"item": r, "why": "항목이 객체가 아니다"})
            continue
        pid = str(r.get("paragraph_id") or "")
        item = {k: r.get(k, "") for k in ("paragraph_id", "old", "new", "reason", "evidence")}
        in_scope = pid in blks and (allowed is None or pid in allowed)
        body = body_of(lines, blks[pid]) if in_scope else None
        why, level, note = check_replacement(r, body, slides, official)
        if not why:
            start = body.index(item["old"])
            span = (start, start + len(item["old"]))
            if any(not (span[1] <= s or span[0] >= s + len(o["old"])) for s, o in accepted.get(pid, [])):
                why = "같은 문단의 다른 치환과 겹친다"
        if why:
            res.rejected.append({**item, "why": why})
            continue
        if level == "추정" and "[추정" not in item["new"]:
            item["new"] = f"[추정: {item['new']}]"  # 근거가 약한 교정은 본문에 표시한다
        accepted.setdefault(pid, []).append((span[0], {**item, "level": level, **({"note": note} if note else {})}))

    for pid, items in accepted.items():
        b = blks[pid]
        body = body_of(lines, b)
        for start, it in sorted(items, key=lambda x: -x[0]):
            body = body[:start] + it["new"] + body[start + len(it["old"]):]
        new_lines = body.split("\n")
        candidate = lines[:b.line] + [b.prefix + new_lines[0]] + new_lines[1:] + lines[b.end:]
        if invariants("\n".join(candidate)) != base:
            res.rejected += [{**it, "why": "적용하면 문단 ID·타임스탬프·<a id> 가 바뀐다"} for _, it in items]
            continue
        lines = candidate
        res.applied += [it for _, it in sorted(items, key=lambda x: x[0])]

    # 장 제목: 기존 문단 블록 바로 앞(앵커 줄 앞)에만 넣는다
    blks = blocks(lines)
    paras = {p.id: p for p in transcript.parse("\n".join(lines)).paragraphs}
    inserts: list[tuple[int, str, dict]] = []
    seen: set[str] = set()
    for ch in patch.get("chapters") or []:
        if not isinstance(ch, dict):
            res.chapters_rejected.append({"item": ch, "why": "항목이 객체가 아니다"})
            continue
        pid = str(ch.get("paragraph_id") or "")
        title = clean_title(ch.get("title"))
        item = {"paragraph_id": pid, "title": title}
        why = ""
        if pid not in blks or (allowed is not None and pid not in allowed):
            why = "문단 ID 가 전사(또는 이 묶음)에 없다"
        elif not title or "<" in title or ">" in title:
            why = "제목이 비었거나 꺾쇠가 있다"
        elif pid in seen:
            why = "같은 문단에 장 제목이 둘이다"
        if not why:
            at = blks[pid].line
            if at > 0 and transcript.ANCHOR_RE.search(lines[at - 1]):
                at -= 1
            k = at - 1
            while k >= 0 and not lines[k].strip():
                k -= 1
            if k >= 0 and transcript.CHAPTER_RE.match(lines[k]):
                why = "이미 장 제목이 있다"
        if why:
            res.chapters_rejected.append({**item, "why": why})
            continue
        seen.add(pid)
        ts = transcript.fmt_ts(paras[pid].start)
        inserts.append((at, f"## [{ts}] {title}", {**item, "time": ts}))
    for at, heading, item in sorted(inserts, key=lambda x: -x[0]):
        block = [heading, ""]
        if at > 0 and lines[at - 1].strip():
            block.insert(0, "")
        lines[at:at] = block
    res.chapters = [item for _, _, item in sorted(inserts, key=lambda x: x[0])]
    out = "\n".join(lines) + ("\n" if trailing_newline else "")
    if invariants(out) != base:  # 장 제목 삽입은 문단·앵커를 건드리지 않는다. 바뀌면 버그다
        raise LectureReconError("내부 오류: 장 제목 삽입 후 문단 ID·앵커가 바뀌었다 — 전사를 쓰지 않는다")
    res.text = out
    return res


def bump_header(text: str, round_no: int, date: str, guessed: bool) -> str:
    if ROUND_RE.search(text):
        text = ROUND_RE.sub(f"교정: {round_no}차 ({date})", text, count=1)
    else:
        text = re.sub(r"^(- ASR: .*)$", rf"\1 · 교정: {round_no}차 ({date})", text, count=1, flags=re.M)
    notice = CORRECTED_NOTICE_GUESS if guessed else CORRECTED_NOTICE
    return DRAFT_NOTICE_RE.sub(lambda _m: notice, text, count=1)


def update_readme(event: Event, line: str, round_no: int, date: str) -> bool:
    path = event.transcripts_dir / "README.md"
    if not path.exists():
        return False
    text = path.read_text(encoding="utf-8")  # 교정 차수 줄은 scaffold.fill_transcripts_readme 가 발표별로 쓴다
    placeholder = scaffold.VERIFY_PLACEHOLDER_RE
    if line in text:  # 이미 적었다(같은 기록)
        pass
    elif placeholder.search(text):  # 자리가 남아 있으면 이전 차수(README 가 없을 때 한 교정)까지 모두 적는다
        lines = scaffold.correct_record_lines(event)
        text = placeholder.sub(lambda _m: "\n".join(lines if line in lines else [*lines, line]), text, count=1)
    elif re.search(r"^## 검증 기록\s*$", text, re.M):
        lines = text.rstrip("\n").split("\n")
        start = next(i for i, x in enumerate(lines) if re.match(r"^## 검증 기록\s*$", x))
        end = next((i for i in range(start + 1, len(lines)) if lines[i].startswith("## ")), len(lines))
        while end > start + 1 and not lines[end - 1].strip():
            end -= 1
        lines.insert(end, line)
        text = "\n".join(lines) + "\n"
    else:
        text = text.rstrip("\n") + f"\n\n## 검증 기록\n\n{line}\n"
    context.write_atomic(path, text)
    return True


def validate_patch(text: str) -> dict:
    """형식이 틀리면 ResponseError(다시 받는다). 내용 규칙은 apply_patch 가 항목별로 거부한다."""
    keys = ("chapters", "replacements")
    data = llm_mod.extract_json(text, keys=keys)
    if not isinstance(data, dict) or not any(k in data for k in keys):
        raise llm_mod.ResponseError('`{"chapters": [...], "replacements": [...]}` 객체가 아니다')
    for key in keys:
        if data.get(key) is None:
            data[key] = []
        if not isinstance(data[key], list):
            raise llm_mod.ResponseError(f"{key} 가 목록이 아니다")
    for i, r in enumerate(data["replacements"]):
        if not isinstance(r, dict):
            raise llm_mod.ResponseError(f"replacements[{i}] 가 객체가 아니다")
        for f in ("paragraph_id", "old", "new"):
            if not isinstance(r.get(f), str):
                raise llm_mod.ResponseError(f"replacements[{i}].{f} 가 문자열이 아니다({r.get(f)!r})")
        for f in ("reason", "evidence"):
            if r.get(f) is not None and not isinstance(r.get(f), str):
                raise llm_mod.ResponseError(f"replacements[{i}].{f} 가 문자열이 아니다")
    for i, ch in enumerate(data["chapters"]):
        if not isinstance(ch, dict) or not isinstance(ch.get("paragraph_id"), str) \
                or not isinstance(ch.get("title"), str):
            raise llm_mod.ResponseError(f"chapters[{i}] 는 {{paragraph_id, title}} 문자열 객체여야 한다")
    return data


def correct_requests(event: Event, talk: Talk, chunk: int) -> list[context.Request]:
    """묶음마다 교정 요청. run 과 --plan 이 같이 쓴다."""
    _, pt = context.load_transcript(event, talk)
    system = llm_mod.prompt("correct")
    table = context.slides_table(event, talk)
    groups = transcript.batches(pt.paragraphs, chunk)
    urls = registered_urls(event)
    terms = user_terms_block(event, talk)
    out = []
    for k, group in enumerate(groups, 1):
        user = "\n\n".join(x for x in [
            context.event_info(event, talk),
            "[이미 붙은 장 제목]\n" + ("\n".join(f"- [{transcript.fmt_ts(s)}] {t}" for s, t in pt.chapters) or "없음"),
            f"[슬라이드 표]\n{table}" if table else "[슬라이드 표]\n없음(사진 없음 또는 판독 전)",
            "[공식 자료로 인정하는 출처] (event.yaml sources)\n"
            + ("\n".join(f"- {u}" for u in urls) or "URL 없음 — \"공식 자료\" 는 [행사 정보]의 문자열 인용만 인정한다"),
            terms,
            f"[전사 문단] 묶음 {k}/{len(groups)} ({group[0].id}–{group[-1].id})",
            context.transcript_block(group, pt.chapters),
        ] if x)
        out.append(context.Request(f"correct-{talk.nn}-{k}", system, user, group, validate=validate_patch))
    return out


def run(event: Event, talk: Talk, llm, chunk: int | None = None, date: str | None = None) -> dict:
    date = date or context.today()
    text, _ = context.load_transcript(event, talk)
    slides = context.slide_numbers(context.slides_table(event, talk))
    official = official_refs(event, talk)
    reqs = correct_requests(event, talk, context.chunk_chars(llm, chunk))
    patches, pending = [], []
    for rq in reqs:
        got = llm_mod.collect_pending(
            lambda: llm.ask(rq.name, rq.system, rq.user, json_mode=True, validate=validate_patch), pending)
        if got is not None:
            patches.append(({p.id for p in rq.paragraphs}, got))
    if pending:
        raise llm_mod.ManualPending(pending, event.root)

    round_no = context.correction_round(text) + 1
    result = PatchResult(text)
    current = text
    for allowed, patch in patches:
        r = apply_patch(current, patch, allowed, slides, official)
        current = r.text
        result.applied += r.applied
        result.rejected += r.rejected
        result.chapters += r.chapters
        result.chapters_rejected += r.chapters_rejected
    guessed = any(a["level"] == "추정" for a in result.applied) or "[추정" in current
    current = bump_header(current, round_no, date, guessed)
    current = scaffold.fill_quality(event, talk, current)
    if invariants(current) != invariants(text):
        raise LectureReconError("내부 오류: 머리말 갱신 후 문단 ID·앵커가 바뀌었다 — 전사를 쓰지 않는다")

    path = event.transcript_path(talk)
    backup = event.work_dir / f"{talk.nn}_draft{round_no - 1}.md"
    k = 1
    while backup.exists() and backup.read_text(encoding="utf-8") != text:  # 다른 내용의 백업은 덮지 않는다
        k += 1
        backup = event.work_dir / f"{talk.nn}_draft{round_no - 1}-{k}.md"
    if not backup.exists():
        backup.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(path, backup)
    context.write_atomic(path, current)

    record = {
        "talk": talk.nn, "round": round_no, "date": date, "backend": llm.label, "batches": len(reqs),
        "backup": event.rel(backup), "chapters": result.chapters, "chapters_rejected": result.chapters_rejected,
        "applied": result.applied, "rejected": result.rejected, "unclear_resolved": result.unclear_resolved,
    }
    rec_path = event.work_dir / "correct" / f"{talk.nn}.json"
    history = []
    if rec_path.exists():
        try:
            old = json.loads(rec_path.read_text(encoding="utf-8"))
            history = old.get("history", []) + [{k: v for k, v in old.items() if k != "history"}]
        except json.JSONDecodeError:
            history = []
    record["history"] = history
    context.write_atomic(rec_path, json.dumps(record, ensure_ascii=False, indent=1))
    readme = update_readme(event, scaffold.correct_record_line(record), round_no, date)
    scaffold.fill_transcripts_readme(event)  # 전처리·ASR 등 남은 자리
    return {**record, "readme": readme, "quality_placeholder": QUALITY_PLACEHOLDER in current,
            "quality_filled": QUALITY_PLACEHOLDER in text and QUALITY_PLACEHOLDER not in current}
