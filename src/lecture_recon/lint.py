"""docs/format.md 의 lint 규칙 6개, 그리고 unresolved-segments.md 재생성.
노트 필수 절(규칙 5)은 설정 파일 `required_sections:`(문자열 목록)로 바꿀 수 있다."""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import unquote

from . import llm, photos, transcript
from .config import Event, LectureReconError, Talk

UNRESOLVED = "unresolved-segments.md"
RESEARCH_SECTION = "연구 라인에 걸리는 것"
# RESEARCH_SECTION 은 연구 라인 설정이 있을 때만 필수다(required_sections() 가 넣는다)
REQUIRED_SECTIONS = ("요약", "원문 대조 결과", "확인이 필요한 것")

FENCE_RE = re.compile(r"^\s*(```|~~~)")
CODE_SPAN_RE = re.compile(r"(`+)[^`]*?\1")
HEADING_RE = re.compile(r"^(#{1,6})\s+(.*?)(?:\s+#+)?\s*$")
LINK_RE = re.compile(r"!?\[([^\]]*)\]\(([^)\s]+)(?:\s+\"[^\"]*\")?\)")
SCHEME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9+.-]*:")
PLACEHOLDER_RE = re.compile(r"<([^<>\n]+)>")
HTML_TAG_RE = re.compile(r"^(?:/?(a|br|img|sup|sub|span|div|details|summary|p|b|i|em|strong|code|kbd|hr)\b|!--)", re.I)
TODO_RE = re.compile(r"\bTODO\b")
SLIDE_MENTION_RE = re.compile(r"슬라이드\s*(\d+)(?![\d장])")
PARA_ID_RE = re.compile(r"(\d{2,}-\d{3,})")


def required_sections() -> tuple[str, ...]:
    """노트 필수 절: 설정 파일 `required_sections:` 가 있으면 그것 그대로, 없으면 REQUIRED_SECTIONS
    (연구 라인 설정이 있으면 `연구 라인에 걸리는 것` 도)."""
    value = llm.read_config().get("required_sections")
    if value is None:
        if llm.research_lines():
            return (*REQUIRED_SECTIONS[:2], RESEARCH_SECTION, *REQUIRED_SECTIONS[2:])
        return REQUIRED_SECTIONS
    if not isinstance(value, list) or not all(isinstance(x, str) for x in value):
        raise LectureReconError(f"{llm.config_path()}: required_sections 는 문자열 목록이어야 한다")
    return tuple(x.strip() for x in value if x.strip())


@dataclass
class LintResult:
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    infos: list[str] = field(default_factory=list)


def slugify(heading: str) -> str:
    """GitHub 헤딩 앵커: 소문자, `-`·`_` 외 구두점 제거, 공백→`-`, 한글 등 문자는 유지."""
    text = re.sub(r"!?\[([^\]]*)\]\([^)]*\)", r"\1", heading)  # 링크는 텍스트만
    text = re.sub(r"<[^>]+>", "", text).strip().lower()
    kept = "".join(ch for ch in text if ch in " -_" or unicodedata.category(ch)[0] in "LMN")
    return kept.replace(" ", "-")


def _scan(text: str) -> list[tuple[int, str, str]]:
    """펜스 코드 블록 밖의 줄: (줄 번호, 원문, 인라인 코드를 지운 줄)."""
    out, fenced = [], False
    for no, line in enumerate(text.splitlines(), 1):
        if FENCE_RE.match(line):
            fenced = not fenced
            continue
        if not fenced:
            out.append((no, line, CODE_SPAN_RE.sub("", line)))
    return out


def _headings(text: str) -> list[tuple[int, int, str]]:
    """(줄 번호, 레벨, 제목)."""
    return [(no, len(m.group(1)), m.group(2)) for no, raw, _ in _scan(text) if (m := HEADING_RE.match(raw))]


def _anchors(path: Path) -> tuple[set[str], set[str]]:
    """(헤딩 앵커, 명시적 <a id>)."""
    text = path.read_text(encoding="utf-8")
    slugs, counts = set(), {}
    for _, _, title in _headings(text):
        base = slugify(title)
        n = counts.get(base, 0)
        counts[base] = n + 1
        slugs.add(base if n == 0 else f"{base}-{n}")
    return slugs, set(transcript.ANCHOR_RE.findall(text))


class _Linter:
    def __init__(self, event: Event):
        self.event = event
        self.result = LintResult()
        self.required = required_sections()
        self._anchor_cache: dict[Path, tuple[set[str], set[str]]] = {}

    def _msg(self, path: Path, line: int, text: str) -> str:
        return f"{self.event.rel(path)}:{line}: {text}" if line else f"{self.event.rel(path)}: {text}"

    def error(self, path: Path, line: int, text: str) -> None:
        self.result.errors.append(self._msg(path, line, text))

    def warn(self, path: Path, line: int, text: str) -> None:
        self.result.warnings.append(self._msg(path, line, text))

    # 전사 ------------------------------------------------------------------
    def transcripts(self) -> dict[Path, transcript.ParsedTranscript]:
        tdir = self.event.transcripts_dir
        if not tdir.is_dir():
            return {}
        parsed = {}
        for path in sorted(tdir.glob("*.md")):
            if path.name in ("README.md", UNRESOLVED):
                continue
            pt = transcript.parse(path.read_text(encoding="utf-8"))
            parsed[path] = pt
            seen: set[str] = set()
            for p in pt.paragraphs:
                if p.id in seen:
                    self.error(path, p.line, f"[문단ID] {p.id} 중복")
                seen.add(p.id)
                if f"segment-{p.id}" not in pt.anchors:
                    self.error(path, p.line, f'[문단ID] {p.id} 의 앵커 <a id="segment-{p.id}"></a> 가 없다')
            if pt.paragraphs and not pt.chapters:
                self.warn(path, 0, "장 제목(`## [HH:MM:SS] …`) 없음 — 교정 전 초안")
        return parsed

    # 규칙 4 ------------------------------------------------------------------
    def unresolved(self, parsed: dict[Path, transcript.ParsedTranscript]) -> None:
        tdir = self.event.transcripts_dir
        if not tdir.is_dir():
            return
        out = tdir / UNRESOLVED
        old_notes: dict[str, list[str]] = {}
        old_rows = 0
        if out.exists():
            for line in out.read_text(encoding="utf-8").splitlines():
                cells = re.split(r"(?<!\\)\|", line.strip())[1:-1]
                m = PARA_ID_RE.search(cells[0]) if len(cells) >= 4 else None
                if m:
                    old_rows += 1
                    old_notes.setdefault(m.group(1), []).append(cells[-1].strip())
        rows, n_marks = [], 0
        for path, pt in parsed.items():
            for p in pt.paragraphs:
                n_marks += p.text.count("[불명확")
                prev = old_notes.get(p.id, [])
                for k, heard in enumerate(transcript.unclear_spans(p.text)):
                    note = prev[k] if k < len(prev) else ""
                    link = f"[{p.id}]({path.name}#segment-{p.id})"
                    heard = heard.replace("|", "\\|")
                    rows.append(f"| {link} | {transcript.fmt_ts(p.start)} | {heard} | {note} |")
        text = "\n".join([
            "# 남은 불명확 구간",
            "",
            "> `lecture-recon lint` 가 전사의 `[불명확` 표시를 모아 다시 만든다. "
            "손으로 쓰는 것은 비고 열뿐이다(문단 ID 기준으로 보존).",
            "",
            "| 문단 | 시각 | 들리는 대로 | 비고 |",
            "|---|---|---|---|",
            *rows,
            "",
        ])
        out.write_text(text, encoding="utf-8")
        if n_marks != len(rows):
            self.error(out, 0, f"[규칙4] 전사의 `[불명확` {n_marks}개 ≠ 표 {len(rows)}행 (표시가 겹쳐 있는지 확인)")
        self.result.infos.append(f"{self.event.rel(out)}: {len(rows)}행으로 갱신 (이전 {old_rows}행)")

    # 규칙 1·2 ----------------------------------------------------------------
    def links(self, path: Path) -> None:
        for no, _, line in _scan(path.read_text(encoding="utf-8")):
            for m in LINK_RE.finditer(line):
                target = unquote(m.group(2))
                if SCHEME_RE.match(target):
                    continue
                file_part, _, anchor = target.partition("#")
                dest = (path.parent / file_part).resolve() if file_part else path
                if not dest.exists():
                    self.error(path, no, f"[규칙2] 링크 파일 없음: {target}")
                    continue
                if not anchor:
                    continue
                if dest.is_file() and dest.suffix == ".md":
                    if dest not in self._anchor_cache:
                        self._anchor_cache[dest] = _anchors(dest)
                    slugs, ids = self._anchor_cache[dest]
                else:
                    slugs, ids = set(), set()
                if anchor.startswith("segment-"):
                    if anchor not in ids:
                        self.error(path, no, f"[규칙1] 전사에 없는 문단: {target}")
                elif anchor.lower() not in slugs and anchor not in ids:
                    self.error(path, no, f"[규칙2] 앵커 없음: {target}")

    # 규칙 3·5 ----------------------------------------------------------------
    def note(self, path: Path, talk: Talk | None) -> None:
        text = path.read_text(encoding="utf-8")
        heads = _headings(text)
        h2 = {title.strip() for _, level, title in heads if level == 2}
        for name in self.required:
            if name not in h2:
                self.error(path, 0, f"[규칙5] 필수 절 `## {name}` 없음")
        has_photos = talk is not None and photos.count(self.event, talk)[0] > 0
        if has_photos and "슬라이드" not in h2:
            self.error(path, 0, "[규칙5] 사진이 있는 발표는 `## 슬라이드` 절이 필요하다")

        slides: set[int] = set()
        in_slides = False
        for _, raw, _ in _scan(text):
            m = HEADING_RE.match(raw)
            if m:
                in_slides = len(m.group(1)) == 2 and m.group(2).strip() == "슬라이드"
            elif in_slides and raw.lstrip().startswith("|"):
                first = raw.strip().strip("|").split("|")[0]
                if (num := re.match(r"\s*(\d+)", first)):
                    slides.add(int(num.group(1)))
        for no, _, line in _scan(text):
            for m in SLIDE_MENTION_RE.finditer(line):
                if int(m.group(1)) not in slides:
                    rng = f"{min(slides):02d}–{max(slides):02d}" if slides else "표 없음"
                    self.error(path, no, f"[규칙3] `슬라이드 {m.group(1)}` 가 슬라이드 표 범위({rng}) 밖이다")

    # 규칙 6, 빈 절 -----------------------------------------------------------
    def placeholders(self, path: Path) -> None:
        hits = []
        for no, _, line in _scan(path.read_text(encoding="utf-8")):
            found = [m for m in PLACEHOLDER_RE.finditer(line) if not HTML_TAG_RE.match(m.group(1))]
            if found or TODO_RE.search(line):
                hits.append(no)
        if hits:
            shown = ", ".join(map(str, hits[:8])) + (", …" if len(hits) > 8 else "")
            self.warn(path, 0, f"[규칙6] 템플릿 placeholder(`<…>`/TODO)가 {len(hits)}줄에 남아 있다 (줄 {shown})")

    def empty_sections(self, path: Path) -> None:
        lines = path.read_text(encoding="utf-8").splitlines()
        heads = _headings("\n".join(lines))
        for i, (no, level, title) in enumerate(heads):
            nxt = heads[i + 1] if i + 1 < len(heads) else None
            body = lines[no: nxt[0] - 1 if nxt else len(lines)]
            if all(not b.strip() for b in body) and (nxt is None or nxt[1] <= level):
                self.warn(path, no, f"[빈 절] `{'#' * level} {title}`")


def run(event: Event) -> LintResult:
    lt = _Linter(event)
    parsed = lt.transcripts()
    lt.unresolved(parsed)

    notes = sorted(event.notes_dir.glob("*.md")) if event.notes_dir.is_dir() else []
    linked = notes + ([event.report_path] if event.report_path.exists() else [])
    for path in linked:
        lt.links(path)
    by_stem = {t.stem: t for t in event.talks}
    for path in notes:
        if path.name != "README.md":
            lt.note(path, by_stem.get(path.stem))
    for path in linked:
        lt.empty_sections(path)
    for path in [*sorted(parsed), *([event.transcripts_dir / "README.md"]
                                     if (event.transcripts_dir / "README.md").exists() else []), *linked]:
        lt.placeholders(path)
    return lt.result
