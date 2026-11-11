"""LLM 단계(correct/claims/verify/notes/report/slides)가 같이 쓰는 입력 조립·파일 쓰기."""

from __future__ import annotations

import datetime as dt
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from . import transcript
from .config import Event, LectureReconError, Talk

API_CHUNK_CHARS = 3000      # API 백엔드: 문단 묶음 크기(자)
MANUAL_CHUNK_CHARS = 12000  # manual: 붙여넣기 횟수를 줄이려고 크게
# 컨텍스트가 크고 무료 일일 한도(RPD)가 작은 backend: 묶음을 크게 해 호출 수를 줄인다(gemini flash 실측 20회/일)
BIG_CHUNK_BACKENDS = {"manual": MANUAL_CHUNK_CHARS, "gemini": 12000}


@dataclass
class Request:
    """LLM 요청 하나(실행과 --plan 이 같이 쓴다). paragraphs·candidates 는 묶음 정보."""
    name: str
    system: str
    user: str
    paragraphs: list = field(default_factory=list)
    candidates: list = field(default_factory=list)
    validate: Callable | None = None   # 응답 형식 검사(--plan 이 캐시가 쓸 만한지 볼 때도 쓴다)

    @property
    def messages(self) -> list[dict]:
        return [{"role": "system", "content": self.system}, {"role": "user", "content": self.user}]


def today() -> str:
    return dt.date.today().isoformat()


def write_atomic(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


def default_chunk_chars(backend: str) -> int:
    return BIG_CHUNK_BACKENDS.get(backend, API_CHUNK_CHARS)


def chunk_chars(llm, override: int | None) -> int:
    if override:
        return override
    return default_chunk_chars(llm.backend.name)


def load_transcript(event: Event, talk: Talk) -> tuple[str, transcript.ParsedTranscript]:
    path = event.transcript_path(talk)
    if not path.exists():
        raise LectureReconError(f"{talk.nn}: 전사가 없다: {event.rel(path)} — 먼저 `lecture-recon segment`")
    text = path.read_text(encoding="utf-8")
    pt = transcript.parse(text)
    if not pt.paragraphs:
        raise LectureReconError(f"{talk.nn}: 전사에 문단(**[NN-MMM] [HH:MM:SS]**)이 없다: {event.rel(path)}")
    return text, pt


def data_safe(text: str) -> str:
    """데이터 블록 안에서 `<source …>`·`</source>`(transcript·notes 도) 를 흉내 낸 태그를 무력화한다."""
    return re.sub(r"<(\s*/?\s*)(source|transcript|notes)\b", r"‹\1\2", str(text), flags=re.I)


def transcript_block(paragraphs: list[transcript.Paragraph], chapters: list[tuple[float, str]] | None = None) -> str:
    """<transcript> … </transcript>: 장 제목은 해당 문단 앞에 `## [HH:MM:SS] 제목` 으로."""
    starts = {}
    for sec, title in chapters or []:
        starts.setdefault(sec, []).append(title)
    lines = ["<transcript>"]
    seen: set[float] = set()
    for p in paragraphs:
        for sec in sorted(s for s in starts if s <= p.start and s not in seen):
            for title in starts[sec]:
                lines.append(f"## [{transcript.fmt_ts(sec)}] {title}")
            seen.add(sec)
        lines.append(f"[{p.id}] [{transcript.fmt_ts(p.start)}] {data_safe(p.text)}")
    lines.append("</transcript>")
    return "\n".join(lines)


def event_info(event: Event, talk: Talk) -> str:
    others = ", ".join(f"{t.nn} {t.speaker_org}" for t in event.talks if t.number != talk.number) or "없음"
    return "\n".join([
        "[행사 정보]",
        f"- 행사: {event.name} ({event.date}{', ' + event.venue if event.venue else ''}"
        f"{', ' + event.organizer if event.organizer else ''})",
        f"- 이 발표: {talk.nn}. {talk.speaker_org} — {talk.title} (언어 {talk.language})",
        *([f"- 별칭: {', '.join(talk.aliases)}"] if talk.aliases else []),
        f"- 같은 행사의 다른 발표(event.yaml): {others}",
    ])


def section(text: str, title: str, level: int = 2) -> str | None:
    """`## title` 절 본문(다음 같은·상위 레벨 헤딩 전까지). 없으면 None."""
    lines = text.splitlines()
    head = re.compile(rf"^#{{1,{level}}}\s+")
    for i, line in enumerate(lines):
        if re.match(rf"^#{{{level}}}\s+{re.escape(title)}\s*$", line):
            body = []
            for nxt in lines[i + 1:]:
                if head.match(nxt):
                    break
                body.append(nxt)
            return "\n".join(body).strip("\n")
    return None


def slides_table(event: Event, talk: Talk) -> str:
    """노트의 `## 슬라이드` 표(있으면). 없으면 빈 문자열."""
    path = event.note_path(talk)
    if not path.exists():
        return ""
    body = section(path.read_text(encoding="utf-8"), "슬라이드")
    if not body:
        return ""
    return "\n".join(line for line in body.splitlines() if line.lstrip().startswith("|"))


def slide_numbers(table: str) -> set[int]:
    nums = set()
    for line in table.splitlines():
        first = line.strip().strip("|").split("|")[0]
        m = re.match(r"\s*(\d+)", first)
        if m:
            nums.add(int(m.group(1)))
    return nums


def correction_round(text: str) -> int:
    m = re.search(r"교정: (\d+)차", text)
    return int(m.group(1)) if m else 0


def correction_label(text: str) -> str:
    """전사 머리말의 `교정: N차 (날짜)` → "N차 (날짜)". 없으면 "0차 (초안)"."""
    m = re.search(r"교정: (\d+차 \([^)\n]*\))", text)
    return m.group(1) if m else "0차 (초안)"
