"""`notes NN`, `report`, `slides NN`: LLM 출력을 docs/format.md 형식으로 조립한다.

- 원문 대조 결과 절은 work/verify/NN.json 에서 CLI 가 만든다(LLM 이 판정을 지어내지 않는다).
- LLM 은 근거를 `[NN-MMM]`·`[[NN#절]]` 토큰으로 쓰고, CLI 가 링크로 바꾸며 없는 ID 가 든 줄·근거 없는 줄을 버린다.
- 연구 라인 소제목은 연구 라인 설정(research_lines.yaml)의 이름만 남긴다. 설정이 없으면 노트·리포트에 그 절을 두지 않는다.
- 골격(scaffold 그대로)은 첫 줄의 scaffold.SKELETON_MARK(예전 골격은 SKELETON_MARKERS 문구)로 가른다. 쓸 때는 마커를 지운다.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import re
import shutil
from dataclasses import dataclass, field
from pathlib import Path

from . import align, context, lint, llm as llm_mod, photos, scaffold, transcript, verify
from .config import Event, LectureReconError, Talk

# 문단 인용 ID: 자릿수가 모자란 `[04-02]`·`[4-02]` 도 받는다(Cites.resolve 가 전사에 있는 ID 로 맞춘다).
# 한쪽이라도 두 자리 이상이어야 한다(`[1-5]`·`[0-1]` 같은 본문 표기는 인용이 아니다)
CITE_ID = r"(?:\d{2,}-\d+|\d+-\d{2,})"
CANON_ID_RE = re.compile(r"\d{2,}-\d{3,}")
CITE_TOKEN = rf"\[{CITE_ID}(?:\s*,\s*{CITE_ID})*\]"
# 이미 전사 링크인 `[..](…#segment-…)` 는 건너뛴다(`[04-002](슬라이드 3)` 은 바꾼다). 인용에 붙은 짝 없는 백틱은 같이 지운다
CITE_RE = re.compile(rf"`*\[({CITE_ID}(?:\s*,\s*{CITE_ID})*)\](?!\([^)\s]*#segment-)`*")
# 코드 표시(`…`, ``…``): 안의 인용은 링크로 바꾸지도 세지도 않는다. 인용 토큰만 든 것(`[04-02]`)은 풀어서 바꾼다
CODE_SPAN_RE = re.compile(r"(?<!`)(`+)(?!`)(.+?)(?<!`)\1(?!`)")
CITE_ONLY_RE = re.compile(rf"\s*{CITE_TOKEN}(?:\s*,?\s*{CITE_TOKEN})*\s*")
LINKED_CITE_RE = re.compile(rf"\[({CITE_ID})\]\([^)]*#segment-\1\)")
NOTE_TOKEN_RE = re.compile(r"\[\[(\d{1,3})(?:#([^\]\n]+))?\]\]")
# LLM 이 토큰을 링크로 감싼 꼴: `[신진우]([[04#3. 절]])`, `[두 발표]([[04]], [[05]])`, `[x](<[[04#요약]]>)`,
# `[신진우](04#3. 모델(RFM)의 구조)` → CLI 가 만든 `[발표자](노트#앵커)` 로만 바꾼다(링크 안에 링크가 생기지 않게)
LINK_OPEN_RE = re.compile(r"\[([^\[\]\n]*)\]\(")
BARE_NOTE_TARGET_RE = re.compile(r"(\d{1,3})(?:#(.+))?", re.S)
CITE_TOKEN_RE = re.compile(CITE_TOKEN)


def rewrite_note_links(text: str, note_link) -> str:
    """`[t](target)`(괄호 짝 맞춤) 중 target 이 노트 토큰을 담거나 `NN#절` 꼴이면 note_link(nn, 절, t) 로 바꾼다.
    토큰이 여럿이면 링크를 `, ` 로 잇고, target 안의 문단 인용 `[NN-MMM]` 은 뒤에 남긴다."""
    out, i = [], 0
    while (m := LINK_OPEN_RE.search(text, i)) is not None:
        depth, j = 1, m.end()
        while j < len(text) and text[j] != "\n":
            depth += {"(": 1, ")": -1}.get(text[j], 0)
            if depth == 0:
                break
            j += 1
        if depth:
            out.append(text[i:m.end()])
            i = m.end()
            continue
        target = text[m.end():j].strip()
        if target.startswith("<") and target.endswith(">"):
            target = target[1:-1].strip()
        repl = None
        tokens = NOTE_TOKEN_RE.findall(target)
        if tokens:
            links = [x for x in (note_link(nn, title or None, "") for nn, title in tokens) if x]
            repl = ", ".join(links) or m.group(1)
            cites = CITE_TOKEN_RE.findall(NOTE_TOKEN_RE.sub("", target))
            repl += "".join(f" {c}" for c in cites)
        elif (b := BARE_NOTE_TARGET_RE.fullmatch(target)):
            repl = note_link(b.group(1), (b.group(2) or "").strip() or None, m.group(1))
        out.append(text[i:m.start()] + repl if repl is not None else text[i:j + 1])
        i = j + 1
    out.append(text[i:])
    return "".join(out)
SLIDE_RE = re.compile(r"슬라이드\s*(\d+)(?![\d장])")
NUMBERED_RE = re.compile(r"^\d+\.\s*(.+)$")
UNCOVERED_RE = re.compile(r"^\s*-\s*다루지 않은 발표\s*:")
# 예전 골격(마커 이전)의 패키지 템플릿 문구. 새 골격은 scaffold.SKELETON_MARK 로 가른다
SKELETON_MARKERS = ("<세 문장 이내.", "<목적 한 줄>", "<관통 주제 1>")
SKELETON_MARK_RE = re.compile(rf"^[ \t]*{re.escape(scaffold.SKELETON_MARK)}[ \t]*(?:\n|$)", re.M)
NOTES_CHUNK_CHARS = 18000
REPORT_SPLIT_CHARS = 40000
RESEARCH_NOTE = lint.RESEARCH_SECTION
RESEARCH_REPORT = "연구에 옮길 아이디어"


# 공통 ------------------------------------------------------------------------

research_lines = llm_mod.research_lines  # 호환용 alias(연구 라인 설정은 llm.research_lines 가 읽는다)


def sections(md: str, level: int = 2) -> list[tuple[str, str]]:
    """`#`*level 헤딩으로 나눈 (제목, 본문). 헤딩 앞 텍스트는 제목 ''."""
    out: list[tuple[str, list[str]]] = [("", [])]
    head = re.compile(rf"^{'#' * level}\s+(.+?)\s*$")
    fenced = False
    for line in md.splitlines():
        if line.strip().startswith("```"):
            fenced = not fenced
        m = None if fenced else head.match(line)
        if m and not line.startswith("#" * (level + 1)):
            out.append((m.group(1).strip(), []))
        else:
            out[-1][1].append(line)
    return [(t, "\n".join(b).strip("\n")) for t, b in out]


def sanitize(text: str) -> str:
    """lint 규칙 6에 걸리는 꺾쇠 placeholder 를 〈…〉 로 바꾼다(HTML 태그는 둔다)."""
    return lint.PLACEHOLDER_RE.sub(lambda m: m.group(0) if lint.HTML_TAG_RE.match(m.group(1)) else f"〈{m.group(1)}〉",
                                   text)


def inline(text) -> str:
    """CLI 가 끼워 넣는 외부 문자열(주장·인용): 한 줄, 링크·placeholder·슬라이드 번호 무력화."""
    t = re.sub(r"\s+", " ", str(text or "")).strip()
    t = t.replace("](", "] (")
    t = SLIDE_RE.sub(lambda m: f"슬라이드(원 번호 {m.group(1)})", t)
    return sanitize(t)


def fix_slides(text: str, valid: set[int]) -> str:
    """슬라이드 표에 없는 번호: 괄호 근거 `(슬라이드 NN)` 는 지우고, 본문 언급은 lint 에 걸리지 않게 바꾼다."""
    text = re.sub(r"\s?\(슬라이드\s*(\d+)\)", lambda m: m.group(0) if int(m.group(1)) in valid else "", text)
    return SLIDE_RE.sub(lambda m: m.group(0) if int(m.group(1)) in valid else f"슬라이드(원 번호 {m.group(1)}, 표 밖)",
                        text)


def sentence(text: str) -> str:
    text = text.strip()
    return text if not text or text.endswith((".", "!", "?")) else text + "."


@dataclass
class Cites:
    """문단 ID → 전사 링크. 없는 ID 는 버린다."""
    event: Event
    base: str                     # 노트: ../<slug>-transcripts, 리포트: <slug>-transcripts
    known: dict[str, Talk] = field(default_factory=dict)
    invalid: list[str] = field(default_factory=list)

    @classmethod
    def build(cls, event: Event, base: str) -> Cites:
        c = cls(event, base)
        for t in event.talks:
            path = event.transcript_path(t)
            if path.exists():
                for p in transcript.parse(path.read_text(encoding="utf-8")).paragraphs:
                    c.known[p.id] = t
        return c

    def link(self, pid: str) -> str:
        return f"[{pid}]({self.base}/{self.known[pid].stem}.md#segment-{pid})"

    def links(self, pids: list[str]) -> str:
        return ", ".join(self.link(p) for p in pids if p in self.known)

    def resolve(self, raw: str) -> str | None:
        """인용 ID → 전사에 있는 문단 ID. 그대로 없으면 발표 2자리·문단 3자리로 0을 채워 본다(`04-02` → `04-002`)."""
        if raw in self.known:
            return raw
        nn, _, mmm = raw.partition("-")
        pid = f"{nn.zfill(2)}-{mmm.zfill(3)}"
        return pid if pid in self.known else None

    def convert(self, line: str) -> tuple[str, int]:
        """(바꾼 줄, 유효 인용 수). 정규 꼴(`NN-MMM`)인데 전사에 없는 ID 는 지우고 invalid 에 남긴다.
        정규 꼴이 아니고 맞춰도 없는 토큰(`[10-20]`)은 본문 표기로 보고 그대로 둔다."""
        n = 0

        def repl(m):
            nonlocal n
            ids = [(x, self.resolve(x)) for x in (y.strip() for y in m.group(1).split(","))]
            good = [pid for _, pid in ids if pid]
            bad = [x for x, pid in ids if not pid and CANON_ID_RE.fullmatch(x)]
            if not good and not bad:
                return m.group(0)
            self.invalid += bad
            n += len(good)
            return ", ".join(self.link(pid) for pid in good)

        def code(m):
            inner = m.group(2).strip()
            if not CITE_ONLY_RE.fullmatch(inner):
                return m.group(0)
            new = CITE_RE.sub(repl, inner)
            return m.group(0) if new == inner else new

        parts, pos = [], 0
        for m in CODE_SPAN_RE.finditer(line):
            parts += [CITE_RE.sub(repl, line[pos:m.start()]), code(m)]
            pos = m.end()
        out = "".join(parts) + CITE_RE.sub(repl, line[pos:])
        out = re.sub(r"\(\s*(?:,\s*)*\)", "", out)
        out = re.sub(r"\(\s*,\s*", "(", re.sub(r",\s*,", ",", re.sub(r",\s*\)", ")", out)))
        return out, n


def ground_lines(body: str, cites: Cites, valid_slides: set[int], require: bool = True) -> tuple[str, int]:
    """본문 줄마다 인용을 링크로 바꾸고, require 면 근거(문단·슬라이드) 없는 줄을 버린다. (본문, 버린 줄 수)."""
    out, dropped = [], 0
    for line in body.splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            out.append(line)
            continue
        new, n = cites.convert(line)
        has_slide = any(int(m.group(1)) in valid_slides for m in SLIDE_RE.finditer(new))
        if require and n == 0 and not has_slide:
            dropped += 1
            continue
        out.append(sanitize(fix_slides(new, valid_slides)))
    text = "\n".join(out).strip("\n")
    return re.sub(r"\n{3,}", "\n\n", text), dropped


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def is_skeleton(text: str, event: Event | None = None, path: Path | None = None) -> bool:
    """scaffold 골격 그대로인가.
    골격 마커 줄이 있으면 골격이되, scaffold 가 남긴 sha256(work/scaffold/)이 있으면 내용이 그때와 같아야 한다
    (마커를 지우지 않고 손으로 쓴 노트를 덮어쓰지 않기 위해). 예전 골격은 패키지 템플릿 문구로 가른다."""
    if SKELETON_MARK_RE.search(text):
        if event is not None and path is not None:
            rec = scaffold.skeleton_record(event, path)
            if rec.is_file():
                return rec.read_text(encoding="utf-8").strip() == _sha(text)
        return True
    return any(m in text for m in SKELETON_MARKERS)


def _hand_written(path: Path, record: Path, event: Event | None = None) -> bool:
    """골격도 아니고 lecture-recon 이 마지막으로 쓴 그대로도 아닌(손으로 쓰거나 고친) 파일."""
    if not path.exists() or not path.read_text(encoding="utf-8").strip():
        return False
    text = path.read_text(encoding="utf-8")
    if is_skeleton(text, event, path):
        return False
    try:
        return json.loads(record.read_text(encoding="utf-8")).get("sha256") != _sha(text)
    except (OSError, json.JSONDecodeError):
        return True


def guard_overwrite(event: Event, path: Path, record: Path, force: bool) -> str | None:
    """덮어써도 되면 None, 아니면 사유."""
    if _hand_written(path, record, event) and not force:
        return (f"{event.rel(path)} 은 손으로 쓴(또는 고친) 내용이다 — 덮어쓰지 않는다. "
                "덮어쓰려면 --force (원본은 work/backup/ 에 남긴다)")
    return None


def write_output(event: Event, path: Path, record: Path, text: str, **extra) -> None:
    """손으로 쓴 파일이면 work/backup/ 에 남기고 쓴다. 골격 마커는 지운다. 쓴 내용의 해시를 record 에 둔다."""
    text = SKELETON_MARK_RE.sub("", text)
    if _hand_written(path, record, event):
        stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
        backup = event.work_dir / "backup" / f"{path.stem}.{stamp}{path.suffix}"
        backup.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(path, backup)
    context.write_atomic(path, text)
    write_record(record, text, **extra)


def written_note(event: Event, talk: Talk) -> bool:
    """골격(scaffold 그대로)이 아닌 노트가 있다."""
    path = event.note_path(talk)
    if not path.exists():
        return False
    text = path.read_text(encoding="utf-8")
    return bool(text.strip()) and not is_skeleton(text, event, path)


def write_record(record: Path, text: str, **extra) -> None:
    context.write_atomic(record, json.dumps({"sha256": _sha(text), "written": dt.datetime.now().isoformat(
        timespec="seconds"), **extra}, ensure_ascii=False, indent=1))


# 원문 대조 결과 절 ---------------------------------------------------------------

def verify_section(talk: Talk, data: dict | None, cites: Cites) -> str:
    lines = ["## 원문 대조 결과", ""]
    if data is None:
        lines += ["원문 대조를 하지 않았다(`lecture-recon verify` 결과 없음).", ""]
        for cat in verify.CATEGORIES[:3]:
            lines += [f"### {cat}", "", "- 없음.", ""]
        lines += [f"### {verify.CATEGORIES[3]}", "",
                  f"- 전체: `lecture-recon claims {talk.nn}` → `lecture-recon verify {talk.nn}` 를 하지 않았다.", ""]
        return "\n".join(lines).rstrip()
    lines += [f"대조일 {data['date']}. `lecture-recon verify`({data['backend']}) 결과다. 인용문이 캐시된 원문에 있는 것만 "
              "\"확인\"·\"발언과 원문이 다른 것\"으로 두었다. 검색·취득·추출 실패와 근거 불충분은 \"확인하지 않은 것\"에 사유와 "
              f"함께 두었다. 전체 표는 `work/verify-{talk.nn}.md`.", ""]
    for cat in verify.CATEGORIES:
        lines += [f"### {cat}", ""]
        rows = [c for c in data["claims"] if c["category"] == cat]
        if not rows:
            lines += ["- 없음.", ""]
            continue
        for c in rows:
            refs = cites.links(c["paragraph_ids"])
            head = f"- {inline(c['claim'])}" + (f"({refs})" if refs else "")
            if cat in verify.CATEGORIES[:2]:
                s = c.get("source") or {}
                url = s.get("url", "")
                title = inline(s.get("title") or url or "원문").replace("[", "(").replace("]", ")")
                where = f"[{title}]({url})" if url.startswith(("http://", "https://")) else f"사용자 자료: {title}"
                note = sentence(inline(c.get("note")))
                conf = f" 신뢰도 {inline(c['confidence'])}." if c.get("confidence") else ""
                lines.append(f"{head} → \"{inline(c.get('quote'))}\" ({where}). {note}{conf}".rstrip())
            elif cat == verify.CATEGORIES[2]:
                queries = "; ".join(inline(q) for q in c.get("queries", []))
                lines.append(f"{head}. 검색: {queries}. {sentence(inline(c.get('reason')))}".rstrip())
            else:
                lines.append(f"{head}. 사유: {sentence(inline(c.get('reason')) or c.get('status'))}")
        lines.append("")
    return "\n".join(lines).rstrip()


# notes ---------------------------------------------------------------------

def validate_content(text: str) -> list[tuple[str, str]]:
    out = [(m.group(1).strip(), body) for title, body in sections(text) if (m := NUMBERED_RE.match(title))]
    if not out:
        raise llm_mod.ResponseError("내용 절(`## 1. 절 제목`)이 없다")
    return out


def validate_final(text: str) -> dict[str, str]:
    got = {title: body for title, body in sections(text) if title}
    if "요약" not in got:
        raise llm_mod.ResponseError("`## 요약` 절이 없다")
    return got


def _verify_digest(data: dict | None) -> str:
    if data is None:
        return "[원문 대조 결과]\n없음(verify 전). 모든 수치는 확인하지 않은 것이다."
    rows = [f"- {c['id']} ({', '.join(c['paragraph_ids'])}) {c['category']}: {context.data_safe(c['claim'])}"
            + (f" — {context.data_safe(c['reason'])}" if c.get("reason") else "") for c in data["claims"]]
    return "[원문 대조 결과]\n" + ("\n".join(rows) or "claim 없음")


def _lines_block(lines: list[dict]) -> str:
    if not lines:
        return f"[연구 라인]\n없음 — `## {RESEARCH_NOTE}` 절을 쓰지 않는다."
    rows = [f"- {x['name']}: 키워드 {', '.join(map(str, x.get('keywords') or []))}"
            + (f" (메모: {x['note']})" if x.get("note") else "") for x in lines]
    return "[연구 라인] (소제목은 이름 그대로)\n" + "\n".join(rows)


def _research_section(body: str, names: set[str], cites: Cites, slides: set[int]) -> tuple[str, list[str]]:
    kept, dropped = [], []
    for title, sub in sections(body, level=3):
        if not title:
            continue
        if title not in names:
            dropped.append(f"연구 라인 이름이 설정에 없다: {title}")
            continue
        grounded, _ = ground_lines(sub, cites, slides)
        if grounded.strip():
            kept.append(f"### {title}\n\n{grounded}")
        else:
            dropped.append(f"근거 ID 없는 연결만 있어 뺐다: {title}")
    return ("\n\n".join(kept) if kept else "근거 있는 연결이 없다."), dropped


def _check_section(body: str, data: dict | None, cites: Cites, slides: set[int]) -> str:
    grounded, _ = ground_lines(body, cites, slides)
    cited = set(re.findall(r"#segment-(\d{2,}-\d{3,})", grounded))
    extra = []
    for c in (data or {}).get("claims", []):
        if c["category"] in verify.CATEGORIES[2:] and not cited.intersection(c["paragraph_ids"]):
            refs = cites.links(c["paragraph_ids"])
            if refs:
                extra.append(f"- {inline(c['claim'])}({refs}): 원문 대조 \"{c['category']}\".")
    body = "\n".join(x for x in [grounded, *extra] if x.strip())
    return body or "- 없음."


def note_header(event: Event, talk: Talk, n_paras: int, n_photos: int, n_slides: int, date: str) -> str:
    t = talk
    source = f"전사 [{t.stem}.md](../{event.slug}-transcripts/{t.stem}.md) (문단 {n_paras}개) · "
    source += f"사진 {n_photos}장(슬라이드 {n_slides}장)" if n_photos else "사진 없음(녹음만)"
    return f"# {t.nn}. {t.speaker_org} — *{t.title}*\n\n> 원자료: {source}\n> 대조일: {date}"


def current_slides_section(event: Event, talk: Talk, n_paras: list[transcript.Paragraph]) -> str:
    path = event.note_path(talk)
    if path.exists():
        body = context.section(path.read_text(encoding="utf-8"), "슬라이드")
        if body is not None:
            return f"## 슬라이드\n\n{body.strip()}"
    st = scaffold.TalkState(talk, n_paras, photos.count(event, talk)[0], align.load(event, talk))
    return scaffold.slides_section(event, st)


def run_notes(event: Event, talk: Talk, llm, force: bool = False) -> dict:
    _, pt = context.load_transcript(event, talk)
    note_path = event.note_path(talk)
    record = event.work_dir / "notes" / f"{talk.nn}.json"
    why = guard_overwrite(event, note_path, record, force)
    if why:
        raise LectureReconError(why)
    data = verify.load_verify(event, talk)
    lines_cfg = llm_mod.research_lines()
    system = llm_mod.prompt("notes")
    table = context.slides_table(event, talk)
    valid_slides = context.slide_numbers(table)
    info = context.event_info(event, talk)
    slide_block = f"[슬라이드 표]\n{table}" if table else "[슬라이드 표]\n없음(사진 없음 또는 판독 전). 슬라이드 번호를 쓰지 않는다."

    groups = transcript.batches(pt.paragraphs, NOTES_CHUNK_CHARS)
    pending: list = []
    parts: list[list[tuple[str, str]]] = []
    for k, group in enumerate(groups, 1):
        user = "\n\n".join([
            "[작업] 내용 절", info, slide_block,
            f"[전사 범위] {k}/{len(groups)} ({group[0].id}–{group[-1].id}). 발표 전체 문단 {len(pt.paragraphs)}개.",
            context.transcript_block(group, pt.chapters),
        ])
        got = llm_mod.collect_pending(
            lambda: llm.ask(f"notes-{talk.nn}-{k}", system, user, validate=validate_content), pending)
        if got is not None:
            parts.append(got)
    if pending:
        raise llm_mod.ManualPending(pending, event.root)

    cites = Cites.build(event, f"../{event.slug}-transcripts")
    content, n_dropped, raw_sections = [], 0, []
    for part in parts:
        for title, body in part:
            raw_sections.append(f"## {len(raw_sections) + 1}. {title}\n\n{body.strip()}")
            grounded, dropped = ground_lines(body, cites, valid_slides)
            n_dropped += dropped
            if grounded.strip():
                content.append((inline(title), grounded))
    if not content:
        raise LectureReconError(f"{talk.nn}: 근거 ID 가 붙은 내용 절이 하나도 없다 — 응답을 확인한다")

    wanted = f"## 요약, ## {RESEARCH_NOTE}, ## 확인이 필요한 것" if lines_cfg else "## 요약, ## 확인이 필요한 것"
    user = "\n\n".join([
        f"[작업] 마무리 ({wanted})", info, slide_block,
        _lines_block(lines_cfg), _verify_digest(data),
        "<notes>\n" + context.data_safe("\n\n".join(raw_sections)) + "\n</notes>",
    ])
    final = llm_mod.collect_pending(
        lambda: llm.ask(f"notes-{talk.nn}-final", system, user, validate=validate_final), pending)
    if pending:
        raise llm_mod.ManualPending(pending, event.root)

    summary = sanitize(fix_slides(final["요약"].strip(), valid_slides))
    summary = cites.convert(summary)[0]
    if lines_cfg:
        research, research_dropped = _research_section(final.get(RESEARCH_NOTE, ""),
                                                       {x["name"] for x in lines_cfg}, cites, valid_slides)
    else:
        research = ""
        research_dropped = ([f"설정이 없어 응답의 `## {RESEARCH_NOTE}` 절을 버렸다"]
                            if final.get(RESEARCH_NOTE, "").strip() else [])
    check = _check_section(final.get("확인이 필요한 것", ""), data, cites, valid_slides)
    n_photos = photos.count(event, talk)[0]
    header = note_header(event, talk, len(pt.paragraphs), n_photos, len(valid_slides),
                         data["date"] if data else "없음(원문 대조 전)")
    body_parts = [header, f"## 요약\n\n{summary}"]
    body_parts += [f"## {i}. {title}\n\n{body}" for i, (title, body) in enumerate(content, 1)]
    body_parts += [current_slides_section(event, talk, pt.paragraphs), verify_section(talk, data, cites)]
    if lines_cfg:
        body_parts.append(f"## {RESEARCH_NOTE}\n\n{research}")
    body_parts.append(f"## 확인이 필요한 것\n\n{check}")
    text = "\n\n".join(body_parts).rstrip() + "\n"
    write_output(event, note_path, record, text, backend=llm.label)
    scaffold.fill(event)
    return {"path": note_path, "sections": len(content), "dropped_lines": n_dropped,
            "invalid_ids": sorted(set(cites.invalid)), "research_lines": len(lines_cfg),
            "research_dropped": research_dropped, "verify": data["counts"] if data else None}


# report --------------------------------------------------------------------

def _note_titles(path: Path) -> list[str]:
    return [t for _, level, t in lint._headings(path.read_text(encoding="utf-8")) if level == 2]


def _strip_links(md: str) -> str:
    return LINKED_CITE_RE.sub(r"[\1]", md)


def validate_report(text: str, need_themes: bool = True) -> dict:
    purpose = ""
    m = re.search(r"^\s*목적\s*:\s*(.+)$", text, re.M)
    if m:
        purpose = m.group(1).strip()
    got = {title: body for title, body in sections(text) if title}
    if need_themes and not any(t for t, _ in sections(got.get("관통 주제", ""), level=3) if t):
        raise llm_mod.ResponseError("`## 관통 주제` 아래 `### 1. 주제` 가 없다")
    return {"purpose": purpose, **got}


def report_inputs(event: Event) -> tuple[list[Talk], str, list[str], list[str], set[str] | None]:
    """(노트가 있는 발표, 행사 정보, 주제용 노트 발췌, 아이디어·확인용 발췌, 노트의 연구 라인 이름).
    연구 라인 이름은 노트들의 `## 연구 라인에 걸리는 것` 아래 `###` 소제목이고, 그 절이 있는 노트가 하나도 없으면 None.
    run_report 와 --plan 이 같이 쓴다."""
    talks = [t for t in event.talks if written_note(event, t)]
    if not talks:
        raise LectureReconError("작성된 노트가 하나도 없다(골격뿐) — 먼저 `lecture-recon notes NN`")
    keep = ("요약", RESEARCH_NOTE, "확인이 필요한 것")
    theme_src, idea_src, note_lines = [], [], None
    for t in talks:
        note = _strip_links(event.note_path(t).read_text(encoding="utf-8"))
        secs = sections(note)
        titles = " | ".join(_note_titles(event.note_path(t)))
        head = f'<notes talk="{t.nn}" speaker="{t.speaker_org}" title="{t.title}">\n절 제목: {titles}'
        content = [f"## {title}\n{body}" for title, body in secs if title == "요약" or NUMBERED_RE.match(title)]
        ideas = [f"## {title}\n{body}" for title, body in secs if title in keep[1:]]
        for title, body in secs:
            if title == RESEARCH_NOTE:
                note_lines = (note_lines or set()) | {x for x, _ in sections(body, level=3) if x}
        theme_src.append(head + "\n" + context.data_safe("\n\n".join(content)) + "\n</notes>")
        idea_src.append(head + "\n" + context.data_safe("\n\n".join(ideas)) + "\n</notes>")
    info = f"[행사] {event.name} ({event.date}). 노트가 있는 발표 {len(talks)}건 / 전체 {len(event.talks)}건."
    return talks, info, theme_src, idea_src, note_lines


def report_calls(event: Event) -> int:
    _, _, theme_src, idea_src, _ = report_inputs(event)
    return 1 if sum(map(len, theme_src)) + sum(map(len, idea_src)) <= REPORT_SPLIT_CHARS else 2


def run_report(event: Event, llm, force: bool = False) -> dict:
    path = event.report_path
    record = event.work_dir / "report.json"
    why = guard_overwrite(event, path, record, force)
    if why:
        raise LectureReconError(why)
    _, info, theme_src, idea_src, note_lines = report_inputs(event)
    research = note_lines is not None
    system = llm_mod.prompt("report")
    total = sum(map(len, theme_src)) + sum(map(len, idea_src))
    no_ideas = f"(노트에 `## {RESEARCH_NOTE}` 절이 없다 — `## {RESEARCH_REPORT}` 는 쓰지 않는다)"
    pending: list = []
    if total <= REPORT_SPLIT_CHARS:
        task = ("[작업] 목적 줄 + 세 절 전부" if research
                else f"[작업] 목적 줄 + `## 관통 주제` 와 `## 확인이 필요한 것` 두 절 {no_ideas}")
        user = "\n\n".join([info, task, *theme_src, *idea_src])
        got = llm_mod.collect_pending(lambda: llm.ask("report-1", system, user, validate=validate_report), pending)
        parts = [got] if got else []
    else:
        u1 = "\n\n".join([info, "[작업] 목적 줄 + `## 관통 주제` 만", *theme_src])
        task = (f"[작업] `## {RESEARCH_REPORT}` 와 `## 확인이 필요한 것` 만" if research
                else f"[작업] `## 확인이 필요한 것` 만 {no_ideas}")
        u2 = "\n\n".join([info, task, *idea_src])
        g1 = llm_mod.collect_pending(lambda: llm.ask("report-1", system, u1, validate=validate_report), pending)
        g2 = llm_mod.collect_pending(lambda: llm.ask("report-2", system, u2,
                                                     validate=lambda t: validate_report(t, need_themes=False)), pending)
        parts = [g for g in (g1, g2) if g]
    if pending:
        raise llm_mod.ManualPending(pending, event.root)
    merged: dict = {}
    for p in parts:
        for k, v in p.items():
            if v and not merged.get(k):
                merged[k] = v
    text, info_out = assemble_report(event, merged, note_lines)
    write_output(event, path, record, text, backend=llm.label)
    scaffold.fill(event)
    return {"path": path, **info_out}


def _note_link(event: Event, talk: Talk, anchor_title: str | None) -> str:
    note = event.note_path(talk)
    target = f"{event.slug}-notes/{talk.stem}.md"
    if anchor_title:
        wanted = anchor_title.strip()
        titles = {t: lint.slugify(t) for _, _, t in lint._headings(note.read_text(encoding="utf-8"))}
        slug = titles.get(wanted)
        if slug is None:
            slug = next((s for t, s in titles.items() if lint.slugify(wanted) in (s, lint.slugify(t))), None)
        if slug:
            target += f"#{slug}"
    return f"[{talk.speaker}]({target})"


def _report_tokens(text: str, event: Event, cites: Cites, cited: set[str]) -> str:
    by_nn = {t.number: t for t in event.talks}

    def note_link(nn: str, title: str | None, fallback: str = "") -> str:
        t = by_nn.get(int(nn))
        if t is None:
            return fallback
        if not written_note(event, t):
            return t.speaker
        cited.add(t.nn)
        return _note_link(event, t, title)

    # 감싼 꼴을 먼저 푼다(안쪽 토큰만 바꾸면 `[x]([발표자](…))` 처럼 링크가 겹친다)
    text = rewrite_note_links(text, note_link)
    text = NOTE_TOKEN_RE.sub(lambda m: note_link(m.group(1), m.group(2)), text)
    out = []
    for line in text.splitlines():
        new, _ = cites.convert(line)
        cited.update(m.group(1).split("-")[0] for m in re.finditer(r"#segment-(\d{2,}-\d{3,})", new))
        out.append(new)
    return "\n".join(out)


def assemble_report(event: Event, parts: dict, note_lines: set[str] | None) -> tuple[str, dict]:
    """note_lines: report_inputs 의 노트 연구 라인 이름(None 이면 `## 연구에 옮길 아이디어` 를 두지 않는다)."""
    states = scaffold.collect(event)
    with_notes = [st for st in states if written_note(event, st.talk)]
    cites = Cites.build(event, f"{event.slug}-transcripts")
    slides_total = sum(len(context.slide_numbers(context.slides_table(event, st.talk))) for st in states)
    rounds = sorted({context.correction_round(event.transcript_path(st.talk).read_text(encoding="utf-8"))
                     for st in states if st.paragraphs is not None})
    round_txt = (f"{rounds[0]}차" if len(rounds) == 1 else f"{rounds[0]}–{rounds[-1]}차") if rounds else "0차"
    n_photos = sum(st.n_photos for st in states)

    # 관통 주제: 소제목마다 다루지 않은 발표를 인용 기준으로 다시 쓴다
    themes, theme_titles = [], []
    for title, body in sections(parts.get("관통 주제", ""), level=3):
        if not title:
            continue
        cited: set[str] = set()
        body = "\n".join(line for line in body.splitlines() if not UNCOVERED_RE.match(line))
        body = _report_tokens(body, event, cites, cited)
        missing = [st.talk for st in with_notes if st.talk.nn not in cited]
        uncovered = ", ".join(f"{t.nn} {t.speaker}" for t in missing) or "없음"
        if len(with_notes) < len(states):
            uncovered += f" (녹음·노트가 없는 발표 {len(states) - len(with_notes)}건은 대상 밖)"
        body = sanitize(body.rstrip()) + f"\n- 다루지 않은 발표: {uncovered}"
        m = NUMBERED_RE.match(title)
        clean = inline(m.group(1) if m else title)
        theme_titles.append(clean)
        themes.append(f"### {len(themes) + 1}. {clean}\n\n{body.strip()}")

    # 연구에 옮길 아이디어: 노트에 연구 라인 절이 하나라도 있을 때만 둔다(소제목은 노트에 있는 라인 이름만)
    research = note_lines is not None
    ideas, dropped = [], []
    for title, body in sections(parts.get(RESEARCH_REPORT, ""), level=3):
        if not title:
            continue
        if title not in (note_lines or set()):
            dropped.append(title)
            continue
        ideas.append(f"### {title}\n\n" + sanitize(_report_tokens(body, event, cites, set()).strip()))
    check = sanitize(_report_tokens(parts.get("확인이 필요한 것", "").strip(), event, cites, set())) or "- 없음."

    # 머리말·한눈에 보기·자료 범위와 신뢰도 (사실에서 직접)
    title = f"# {event.name} 종합 리포트"  # 부제(관통 주제 나열)는 붙이지 않는다(사용자 지시 2026-11-11)
    readme = event.notes_dir / "README.md"
    collection = (f"[{event.name} 청강 재구성 노트]({event.slug}-notes/README.md)" if readme.exists()
                  else f"`{event.slug}-notes/`")
    material = f"현장 녹음과 사진({round_txt} 교정본)" if n_photos else f"현장 녹음(사진 없음, {round_txt} 교정본)"
    srcs = "; ".join(f"[근거]({s})" if re.match(r"^https?://", s) else inline(s) for s in event.sources)
    event_line = ", ".join(x for x in (event.name, event.date, event.venue, event.organizer) if x)
    talk_lines = []
    for st in states:
        t = st.talk
        if event.note_path(t).exists():
            talk_lines.append(f"  {t.number}. [{t.speaker_org}]({event.slug}-notes/{t.stem}.md): *{t.title}*")
        else:
            talk_lines.append(f"  {t.number}. {t.speaker_org}: *{t.title}* (노트 없음)")
    purpose = inline(parts.get("purpose") or "")
    if purpose and not purpose.startswith("이 리포트는"):
        purpose = f"이 리포트는 {purpose}"
    totals = scaffold._totals(states)
    glance = [
        f"- 행사: {event_line}." + (f" 근거: {srcs}." if srcs else ""),
        f"- 발표 {len(states)}건:", *talk_lines,
        f"- 자료: 전사 {totals['n_transcripts']}건(문단 {totals['n_paragraphs']}개), 사진 {n_photos}장"
        f"(슬라이드 {slides_total}장). {round_txt} 교정본.",
    ] + ([f"- {purpose}"] if purpose else [])

    vdata = [(st.talk, verify.load_verify(event, st.talk)) for st in states]
    counts = {cat: 0 for cat in verify.CATEGORIES}
    dates = []
    for _, d in vdata:
        if d:
            dates.append(d["date"])
            for cat in verify.CATEGORIES:
                counts[cat] += d["counts"].get(cat, 0)
    resolved = 0
    for st in states:
        rec = event.work_dir / "correct" / f"{st.talk.nn}.json"
        if rec.exists():
            r = json.loads(rec.read_text(encoding="utf-8"))
            resolved += r.get("unclear_resolved", 0) + sum(h.get("unclear_resolved", 0) for h in r.get("history", []))
    single = [st.talk for st in states if len(st.talk.recordings) == 1 and st.paragraphs is not None]
    conflicts = [(t, c) for t, d in vdata if d for c in d["claims"] if c["category"] == verify.CATEGORIES[1]][:3]
    with_p = [st for st in states if st.n_photos]
    without_p = [st for st in states if not st.n_photos]
    source_bits = []
    if with_p:
        source_bits.append(f"슬라이드 대조 가능한 발표 {scaffold._talk_names(with_p)}")
    if without_p:
        source_bits.append(f"녹음만 있는 발표 {scaffold._talk_names(without_p)}")
    if single:
        source_bits.append("녹음이 하나라 교차 검증 없음(" + ", ".join(f"{t.nn} {t.speaker}" for t in single) + ")")
    unresolved = event.transcripts_dir / "unresolved-segments.md"
    unclear_line = f"- 불명확 표시 {totals['n_unclear']}곳, `lecture-recon correct` 로 푼 것 {resolved}곳"
    if unresolved.exists():
        unclear_line += f" → [남은 표시 목록]({event.slug}-transcripts/unresolved-segments.md)"
    scope = [
        "- 원자료: " + " · ".join(source_bits) + f". {round_txt} 교정본.",
        unclear_line + ".",
        "- 원문 대조" + (f"({max(dates)}, 인용문을 캐시된 원문에서 다시 찾은 것만 \"확인\"): "
                     + " · ".join(f"{cat} {counts[cat]}" for cat in verify.CATEGORIES) + "."
                     if dates else ": 하지 않았다(`lecture-recon verify` 결과 없음). 모든 수치는 확인하지 않은 것이다."),
    ]
    if conflicts:
        items = []
        for t, c in conflicts:
            refs = cites.links(c["paragraph_ids"])
            s = c.get("source") or {}
            url = s.get("url", "")
            quote = inline(c.get("quote") or "")
            quote = quote if len(quote) <= 80 else quote[:79].rsplit(" ", 1)[0] + "…"
            where = f"[원문]({url})" if url.startswith(("http://", "https://")) else ""
            items.append(f"{inline(c['claim'])}({refs}) ↔ \"{quote}\"" + (f" {where}" if where else ""))
        scope.append("- 발언과 원문이 다른 대표 사례(외부 인용은 원문값): " + " · ".join(items) + ".")
    else:
        scope.append("- 발언과 원문이 다른 대표 사례: 없음.")

    blocks = [
        title,
        f"> 재구성: {event.reconstructor or '미기재'} · 원자료: {material}\n>\n> 노트 컬렉션: {collection}.",
        "## 한눈에 보기\n\n" + "\n".join(glance),
        "## 자료 범위와 신뢰도\n\n" + "\n".join(scope),
        "## 관통 주제\n\n" + ("\n\n".join(themes) if themes else "관통 주제를 만들지 못했다."),
        *([f"## {RESEARCH_REPORT}\n\n" + ("\n\n".join(ideas) if ideas else "노트에 근거 있는 연결이 없다.")]
          if research else []),
        "## 확인이 필요한 것\n\n" + check,
    ]
    text = re.sub(r"\n{3,}", "\n\n", "\n\n".join(blocks)).rstrip() + "\n"
    return text, {"themes": theme_titles, "ideas_section": research, "ideas_dropped": dropped,
                  "invalid_ids": sorted(set(cites.invalid))}


# slides --------------------------------------------------------------------

SLIDE_BATCH = 10


def validate_slides(text: str, expected: list[str]) -> dict[str, dict]:
    rows = llm_mod.json_object(text, "slides")
    bad = [i for i, r in enumerate(rows) if not isinstance(r, dict) or not isinstance(r.get("photo"), str)]
    if bad:
        raise llm_mod.ResponseError(f"slides[{bad[0]}] 가 photo 문자열을 가진 객체가 아니다: {str(rows[bad[0]])[:60]}")
    got = [r["photo"].strip() for r in rows]
    got = [Path(g).stem for g in got]
    missing = [p for p in expected if p not in got]
    extra = [p for p in got if p not in expected]
    dup = sorted({p for p in got if got.count(p) > 1})
    if missing or extra or dup:
        raise llm_mod.ResponseError("photo 목록이 입력과 다르다: " + "; ".join(x for x in [
            f"빠짐 {', '.join(missing)}" if missing else "", f"모르는 사진 {', '.join(extra)}" if extra else "",
            f"중복 {', '.join(dup)}" if dup else ""] if x))
    return {Path(str(r["photo"])).stem: r for r in rows}


def _cells(line: str) -> list[str]:
    return [c.strip() for c in re.split(r"(?<!\\)\|", line.strip())[1:-1]]


def run_slides(event: Event, talk: Talk, llm) -> dict:
    path = event.note_path(talk)
    if not path.exists():
        raise LectureReconError(f"{talk.nn}: 노트가 없다 — 먼저 `lecture-recon align` → `scaffold`")
    text = path.read_text(encoding="utf-8")
    lines = text.splitlines()
    try:
        start = next(i for i, x in enumerate(lines) if x.strip() == "## 슬라이드")
    except StopIteration:
        raise LectureReconError(f"{talk.nn}: 노트에 `## 슬라이드` 표가 없다(사진 없는 발표이거나 scaffold 전)") from None
    rows = []
    for i in range(start + 1, len(lines)):
        if lines[i].startswith("## "):
            break
        cells = _cells(lines[i]) if lines[i].lstrip().startswith("|") else []
        if len(cells) >= 6 and re.match(r"\d+", cells[0]):
            rows.append((i, cells))
    if not rows:
        raise LectureReconError(f"{talk.nn}: 슬라이드 표에 행이 없다 — `lecture-recon align` 후 `scaffold --force`")
    photo_dir = event.slides_dir(talk)
    files = {p.stem: p for p in photo_dir.glob("*.jpg")} if photo_dir.is_dir() else {}
    paras = {p.id: p for p in context.load_transcript(event, talk)[1].paragraphs}
    stems = list(dict.fromkeys(cells[1] for _, cells in rows))
    system = llm_mod.prompt("slides")
    pending, got_all = [], {}
    for k in range(0, len(stems), SLIDE_BATCH):
        batch = stems[k:k + SLIDE_BATCH]
        listing = []
        for stem in batch:
            cells = next(c for _, c in rows if c[1] == stem)
            pid = (re.search(r"\d{2,}-\d{3,}", cells[5]) or [None])[0]
            hint = f", 대응 문단 {pid}: \"{paras[pid].text[:150]}\"" if pid in paras else ""
            where = files[stem] if stem in files else "(축소본 없음)"
            listing.append(f"- photo: {stem} (파일: {where}, 녹음 안 시각 {cells[2]}{hint})")
        user = "\n".join([f"[사진 {len(batch)}장] 이 메시지와 함께 아래 순서대로 사진을 첨부한다.", *listing])
        got = llm_mod.collect_pending(
            lambda: llm.ask(f"slides-{talk.nn}-{k // SLIDE_BATCH + 1}", system, user,
                            validate=lambda t: validate_slides(t, batch)), pending)
        if got:
            got_all.update(got)
    if pending:
        raise llm_mod.ManualPending(pending, event.root)
    filled = 0
    for i, cells in rows:
        r = got_all.get(cells[1])
        if not r:
            continue
        title = verify.cell(inline(r.get("title")) or "(판독 불가)")
        content = verify.cell(inline(r.get("content")) or "(판독 불가)")
        cells[3], cells[4] = title, content
        lines[i] = "| " + " | ".join(cells) + " |"
        filled += 1
    n_slides = len({int(re.match(r"\d+", c[0]).group()) for _, c in rows})
    new = "\n".join(lines) + ("\n" if text.endswith("\n") else "")
    new = re.sub(r"(사진 \d+장\(슬라이드 )[^)]*(장\))", rf"\g<1>{n_slides}\g<2>", new, count=1)
    record = event.work_dir / "notes" / f"{talk.nn}.json"
    ours = record.exists() and not _hand_written(path, record, event)
    scaffold.write_tool(event, path, new)
    if ours:  # notes 가 쓴 노트에 표만 채웠으면 계속 notes 가 다시 쓸 수 있게 둔다
        write_record(record, new, backend=llm.label, slides=True)
    return {"path": path, "filled": filled, "rows": len(rows)}
