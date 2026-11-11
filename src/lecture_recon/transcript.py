"""ASR 세그먼트 → 문단(NN-MMM) 전사 md 초안, 그리고 전사 md 파서(교정 후에도 동작)."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import PurePosixPath

from .config import Event, Talk

GAP_SEC = 1.5          # 세그먼트 간 침묵이 이 이상이면 문단을 끊는다
MAX_PARA_SEC = 45.0    # 문단 길이가 이 이상이 되면 끊는다
MAX_PARA_CHARS = 400   # 문단 글자 수가 이 이상이 되면 끊는다
UNCLEAR_LOGPROB = -1.0
UNCLEAR_NO_SPEECH = 0.6

PARA_RE = re.compile(r"^\*\*\[(\d{2,}-\d{3,})\] \[(\d{2,}):(\d{2}):(\d{2})\]\*\*\s?(.*)$")
ANCHOR_RE = re.compile(r'<a\s+(?:id|name)="([^"]+)"\s*>\s*</a>')
CHAPTER_RE = re.compile(r"^##\s+\[(\d{2,}):(\d{2}):(\d{2})\]\s*(.*)$")
DURATION_RE = re.compile(r"^- Duration:\s*(\d{2,}):(\d{2}):(\d{2})")
UNCLEAR_RE = re.compile(r"\[불명확([^\]\n]*)\]?")
QUALITY_PLACEHOLDER = '> Quality note: <저음량·잡음·누락 구간 등. 없으면 "특이사항 없음">'


@dataclass
class Segment:
    start: float
    end: float
    text: str
    avg_logprob: float = 0.0
    no_speech_prob: float = 0.0

    @classmethod
    def from_dict(cls, d: dict) -> Segment:
        return cls(
            start=float(d["start"]),
            end=float(d["end"]),
            text=str(d.get("text") or ""),
            avg_logprob=float(d.get("avg_logprob") or 0.0),
            no_speech_prob=float(d.get("no_speech_prob") or 0.0),
        )

    @property
    def unclear(self) -> bool:
        return self.avg_logprob < UNCLEAR_LOGPROB or self.no_speech_prob > UNCLEAR_NO_SPEECH


@dataclass
class Paragraph:
    id: str          # "NN-MMM"
    start: float     # 초
    text: str
    line: int = 0    # 파싱했을 때 md 의 줄 번호(1부터)


@dataclass
class ParsedTranscript:
    paragraphs: list[Paragraph] = field(default_factory=list)
    anchors: dict[str, int] = field(default_factory=dict)        # id → 줄 번호
    chapters: list[tuple[float, str]] = field(default_factory=list)
    duration: float | None = None


def fmt_ts(sec: float) -> str:
    s = max(0, int(sec))
    return f"{s // 3600:02d}:{s % 3600 // 60:02d}:{s % 60:02d}"


def _hms(h: str, m: str, s: str) -> float:
    return int(h) * 3600 + int(m) * 60 + int(s)


def wrap_unclear(text: str) -> str:
    return '[불명확: "' + text.replace('"', "'") + '"]'


def paragraphize(segments: list[Segment], talk_number: int) -> list[Paragraph]:
    """세그먼트를 문단으로 묶는다.

    직전 세그먼트와의 침묵이 GAP_SEC 이상이면 새 문단을 시작하고, 세그먼트를 붙인 뒤
    문단 길이(첫 시작~마지막 끝)가 MAX_PARA_SEC 이상이거나 글자 수가 MAX_PARA_CHARS
    이상이면 그 자리에서 문단을 닫는다. 신뢰도 낮은 세그먼트는 `[불명확: "…"]`로 감싼다.
    """
    paras: list[Paragraph] = []
    pieces: list[str] = []
    start = prev_end = 0.0

    def flush() -> None:
        if pieces:
            pid = f"{talk_number:02d}-{len(paras) + 1:03d}"
            paras.append(Paragraph(id=pid, start=start, text=" ".join(pieces)))
            pieces.clear()

    for seg in segments:
        piece = seg.text.strip()
        if not piece:
            continue
        if seg.unclear:
            piece = wrap_unclear(piece)
        if pieces and seg.start - prev_end >= GAP_SEC:
            flush()
        if not pieces:
            start = seg.start
        pieces.append(piece)
        prev_end = seg.end
        if seg.end - start >= MAX_PARA_SEC or len(" ".join(pieces)) >= MAX_PARA_CHARS:
            flush()
    flush()
    return paras


def render(event: Event, talk: Talk, paragraphs: list[Paragraph], asr: dict) -> str:
    """format.md 1층 형식의 전사 초안(장 제목 없음, 교정 0차)."""
    lines = [f"# {talk.title}", "", f"{talk.speaker_org} · {event.name} {event.date}", ""]
    for k, rec in enumerate(talk.recordings):
        name = PurePosixPath(rec.file).name
        suffix = "" if k == 0 else f" (보조 녹음, 전사에 쓰지 않음: work/asr/{talk.key(k)}.json)"
        lines.append(f"- Source audio: `{name}`{suffix}")
    rec0 = talk.recordings[0]
    if rec0.started_at:
        recorded = f"{rec0.started_at.date().isoformat()} ({event.timezone}), started {rec0.started_at:%H:%M:%S} (event.yaml 기준)"
    else:
        recorded = f"{event.date} ({event.timezone}), started 미상 (event.yaml 에 started_at 없음)"
    segs = asr.get("segments") or []
    duration = asr.get("duration") or (segs[-1]["end"] if segs else 0)
    lines += [
        f"- Recorded: {recorded}",
        f"- Duration: {fmt_ts(duration)}",
        f"- Language: {talk.language}",
        f"- ASR: {asr.get('model', '?')}, {asr.get('device', '?')}, {asr.get('created', '?')} · 교정: 0차 (초안)",
        "",
        QUALITY_PLACEHOLDER,
        "",
        "> 기계 전사 초안이다(교정 전). 신뢰도 낮은 세그먼트는 `[불명확: \"…\"]`로 감쌌다.",
        "",
    ]
    for p in paragraphs:
        lines += [f'<a id="segment-{p.id}"></a>', f"**[{p.id}] [{fmt_ts(p.start)}]** {p.text}", ""]
    return "\n".join(lines)


def parse(text: str) -> ParsedTranscript:
    """전사 md 에서 문단 ID·시각·본문, 앵커, 장 제목, Duration 을 읽는다."""
    out = ParsedTranscript()
    current: Paragraph | None = None
    for no, line in enumerate(text.splitlines(), 1):
        for m in ANCHOR_RE.finditer(line):
            out.anchors.setdefault(m.group(1), no)
        m = PARA_RE.match(line)
        if m:
            current = Paragraph(id=m.group(1), start=_hms(*m.group(2, 3, 4)), text=m.group(5).strip(), line=no)
            out.paragraphs.append(current)
            continue
        if not line.strip() or line.startswith("#") or ANCHOR_RE.search(line):
            current = None
        elif current is not None:
            current.text += " " + line.strip()
        m = CHAPTER_RE.match(line)
        if m:
            out.chapters.append((_hms(*m.group(1, 2, 3)), m.group(4).strip()))
            continue
        m = DURATION_RE.match(line)
        if m and out.duration is None:
            out.duration = _hms(*m.group(1, 2, 3))
    return out


def unclear_spans(text: str) -> list[str]:
    """문단 본문의 `[불명확…]` 표시마다 '들리는 대로' 내용을 돌려준다(없으면 빈 문자열)."""
    spans = []
    for m in UNCLEAR_RE.finditer(text):
        body = m.group(1).lstrip(":").strip()
        if len(body) >= 2 and body[0] == body[-1] == '"':
            body = body[1:-1]
        spans.append(body)
    return spans


def batches(paragraphs: list[Paragraph], max_chars: int) -> list[list[Paragraph]]:
    """문단을 순서대로 max_chars 안팎의 묶음으로 나눈다(문단은 쪼개지 않는다)."""
    out: list[list[Paragraph]] = []
    size = 0
    for p in paragraphs:
        if out and size + len(p.text) > max_chars and out[-1]:
            out.append([])
            size = 0
        if not out:
            out.append([])
        out[-1].append(p)
        size += len(p.text) + 20
    return out
