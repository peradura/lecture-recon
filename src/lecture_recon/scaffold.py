"""templates/*.md 로 전사 README·노트 README·발표별 노트·종합 리포트 골격을 만든다.
템플릿은 templates_dir(설정)에 같은 이름이 있으면 그것을 쓴다. `<!-- if KEY -->`·`<!-- endif -->` 는 각각 한 줄에 단독으로
쓰고(중첩 미지원), 그 사이 줄은 KEY 가 참일 때만 남긴다. KEY 는 render 값 또는 공통 조건(conditions)이다.
노트·리포트 골격 첫 줄에는 SKELETON_MARK 를 넣는다(compose 가 골격과 손으로 쓴 노트를 가른다).
fill: LLM 없이 사실에서 채울 수 있는 자리(README 처리 과정·검증 기록·자료 규모·작업 기록, 전사 Quality note)를 채운다."""

from __future__ import annotations

import hashlib

import datetime as dt
import json
import re
from dataclasses import dataclass
from pathlib import Path

import yaml

from . import align, context, llm, photos, transcript
from .config import Event, LectureReconError, Talk

NO_PHOTOS_LINE = "사진이 없어 녹음 전사가 원자료의 전부다."
SKELETON_MARK = "<!-- lecture-recon:skeleton -->"
# 조건 블록: 마커는 단독 줄(앞뒤 공백 허용)이고 줄째 지운다. 치환 뒤에 남은 마커는 짝이 안 맞거나 단독 줄이 아닌 것이다
IF_BLOCK_RE = re.compile(r"^[ \t]*<!--[ \t]*if[ \t]+([\w-]+)[ \t]*-->[ \t]*\n(.*?)"
                         r"^[ \t]*<!--[ \t]*endif[ \t]*-->[ \t]*(?:\n|\Z)", re.S | re.M)
IF_OPEN_RE = re.compile(r"<!--\s*if\b")
IF_LEFT_RE = re.compile(r"<!--\s*(if|endif)\b[^\n]*")


def template(name: str) -> str:
    return llm.template_path(name).read_text(encoding="utf-8")


def conditions() -> dict[str, bool]:
    """모든 템플릿의 `<!-- if KEY -->` 에 쓰는 공통 조건."""
    return {"research_lines": bool(llm.research_lines())}


def render(template_name: str, /, **values) -> str:
    """{{key}} 를 values 로 바꾸고 조건 블록을 푼다. 조건 KEY 는 values 에 있으면 그 값, 없으면 conditions()."""
    source = llm.template_path(template_name)
    text = source.read_text(encoding="utf-8")
    if IF_LEFT_RE.search(text):
        common = conditions()

        def block(m: re.Match) -> str:
            key, body = m.group(1), m.group(2)
            if IF_OPEN_RE.search(body):
                raise LectureReconError(f"{source}: 조건 블록 안에 `<!-- if` 가 또 있다 — 중첩은 지원하지 않는다")
            if key in values:
                keep = values[key]
            elif key in common:
                keep = common[key]
            else:
                raise LectureReconError(f"{source}: 모르는 조건 키 `{key}` (쓸 수 있는 것: {', '.join(sorted(common))})")
            return body if keep else ""

        text = IF_BLOCK_RE.sub(block, text)
        left = IF_LEFT_RE.search(text)
        if left:
            line = text.count("\n", 0, left.start()) + 1
            raise LectureReconError(f"{source}: 짝이 맞지 않거나 단독 줄이 아닌 조건 마커(치환 뒤 {line}번째 줄): "
                                    f"{left.group().strip()} — `<!-- if KEY -->` 와 `<!-- endif -->` 는 각각 한 줄에 단독으로 쓴다")
    for key, value in values.items():
        text = text.replace("{{" + key + "}}", str(value))
    left = re.search(r"\{\{\w+\}\}", text)
    if left:
        raise LectureReconError(f"{source}: 값이 없는 자리 {left.group()}")
    return text


@dataclass
class TalkState:
    talk: Talk
    paragraphs: list[transcript.Paragraph] | None  # 전사가 없으면 None
    n_photos: int
    aligned: list[dict] | None

    @property
    def n_unclear(self) -> int:
        return sum(len(transcript.unclear_spans(p.text)) for p in self.paragraphs or [])


def collect(event: Event) -> list[TalkState]:
    states = []
    for talk in event.talks:
        tpath = event.transcript_path(talk)
        paras = transcript.parse(tpath.read_text(encoding="utf-8")).paragraphs if tpath.exists() else None
        states.append(TalkState(talk, paras, photos.count(event, talk)[0], align.load(event, talk)))
    return states


def _or(value: str, placeholder: str) -> str:
    return value if value else placeholder


def _segment_link(event: Event, talk: Talk, pid: str) -> str:
    return f"[{pid}](../{event.slug}-transcripts/{talk.stem}.md#segment-{pid})"


def slides_section(event: Event, st: TalkState) -> str:
    if not st.n_photos:
        return NO_PHOTOS_LINE
    lines = ["## 슬라이드", "", "| 슬라이드 | 원 사진 | 시각 | 제목 | 핵심 내용 | 대응 문단 |", "|---|---|---|---|---|---|"]
    if not st.aligned:
        lines.append("| <NN> | <원 사진> | <HH:MM:SS> | <제목> | <핵심 내용> | <NN-MMM> |")
    for a in st.aligned or []:
        off = a["offset_in_talk_sec"]
        when = transcript.fmt_ts(off) if off >= 0 else "(녹음 전)"
        para = _segment_link(event, st.talk, a["paragraph_id"]) if a["paragraph_id"] else "(정렬 밖)"
        lines.append(f"| {a['slide_hint']:02d} | {Path(a['photo']).stem} | {when} | <제목> | <핵심 내용> | {para} |")
    return "\n".join(lines)


def note_text(event: Event, st: TalkState) -> str:
    t = st.talk
    k = str(len(st.paragraphs)) if st.paragraphs is not None else "<K>"
    source = f"전사 [{t.stem}.md](../{event.slug}-transcripts/{t.stem}.md) (문단 {k}개) · "
    source += f"사진 {st.n_photos}장(슬라이드 <S>장)" if st.n_photos else "사진 없음(녹음만)"
    return SKELETON_MARK + "\n" + render("note.md", nn=t.nn, speaker_org=t.speaker_org, title=t.title,
                                         source_line=source, slug=event.slug, stem=t.stem,
                                         slides_section=slides_section(event, st))


def _totals(states: list[TalkState]) -> dict:
    return {
        "n_talks": len(states),
        "n_transcripts": sum(st.paragraphs is not None for st in states),
        "n_paragraphs": sum(len(st.paragraphs or []) for st in states),
        "n_unclear": sum(st.n_unclear for st in states),
        "n_photos": sum(st.n_photos for st in states),
    }


def _talk_rows(states: list[TalkState]) -> list[str]:
    return [
        f"| {st.talk.nn} | [{st.talk.stem}]({st.talk.stem}.md) | {st.talk.speaker_org} | {st.talk.title} | "
        + (f"{st.n_photos}장 |" if st.n_photos else "없음 |")
        for st in states
    ]


def notes_readme_text(event: Event, states: list[TalkState]) -> str:
    rows = _talk_rows(states)
    sources = ", ".join(f"[{s}]({s})" for s in event.sources)
    return render("notes_readme.md", name=event.name, reconstructor=_or(event.reconstructor, "<재구성자>"),
                  slug=event.slug, date=event.date, venue=_or(event.venue, "<장소>"),
                  organizer=_or(event.organizer, "<주관>"), sources=_or(sources, "<근거 링크>"),
                  talk_rows="\n".join(rows), today=dt.date.today().isoformat(), **_totals(states))


def _talk_names(states: list[TalkState]) -> str:
    return ", ".join(f"{st.talk.nn} {st.talk.speaker}" for st in states) or "없음"


def report_text(event: Event, states: list[TalkState]) -> str:
    talk_list = "\n".join(
        f"  {st.talk.number}. [{st.talk.speaker_org}]({event.slug}-notes/{st.talk.stem}.md): *{st.talk.title}*"
        for st in states
    )
    sources = ", ".join(f"[근거]({s})" for s in event.sources) or "<근거 링크>"
    event_line = f"{event.date}, {_or(event.venue, '<장소>')}, {_or(event.organizer, '<주관>')}, {sources}"
    return SKELETON_MARK + "\n" + render(
        "report.md", name=event.name, reconstructor=_or(event.reconstructor, "<재구성자>"),
        slug=event.slug, event_line=event_line, talk_list=talk_list,
        with_photos=_talk_names([s for s in states if s.n_photos]),
        without_photos=_talk_names([s for s in states if not s.n_photos]), **_totals(states))


def asr_meta(event: Event, talk: Talk) -> dict | None:
    path = event.asr_path(talk)
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return None


def _asr_line(event: Event, states: list[TalkState]) -> str:
    for st in states:
        meta = asr_meta(event, st.talk)
        if meta:
            return (f"faster-whisper `{meta.get('model')}` ({meta.get('device')}, "
                    f"{meta.get('compute_type')}), {meta.get('created')}")
    return ""


def transcripts_readme_text(event: Event, states: list[TalkState]) -> str:
    talk_list = "\n".join(
        f"{st.talk.number}. [{st.talk.nn} {st.talk.speaker_org} — {st.talk.title}]({st.talk.stem}.md)" for st in states
    )
    asr_line = _asr_line(event, states) or ASR_PLACEHOLDER
    multi = [st.talk for st in states if len(st.talk.recordings) > 1]
    if multi:
        multi_line = "; ".join(
            f"{t.nn}: 녹음 {len(t.recordings)}개 — 첫 녹음으로 전사, 나머지는 "
            + ", ".join(f"work/asr/{t.key(k)}.json" for k in range(1, len(t.recordings)))
            for t in multi
        ) + ". 교차 대조: <대조한 범위를 적는다>"
    else:
        multi_line = "없음 (발표마다 녹음 1개)"
    return render("transcripts_readme.md", name=event.name, date=event.date, n_talks=len(states),
                  talk_list=talk_list, asr_line=asr_line, multi_line=multi_line)


def skeleton_record(event: Event, path: Path) -> Path:
    """scaffold 가 쓴 골격의 sha256 (compose 가 '마커는 남았지만 손으로 고친 파일'을 가려낸다)."""
    return event.work_dir / "scaffold" / f"{path.name}.sha256"


def write_tool(event: Event, path: Path, text: str) -> None:
    """lecture-recon 이 골격을 기계적으로 고쳐 쓸 때(fill·slides 표 채움): 쓰고, 마커가 남아 있으면 골격 sha256 도 갱신한다."""
    context.write_atomic(path, text)
    if SKELETON_MARK in text:
        rec = skeleton_record(event, path)
        rec.parent.mkdir(parents=True, exist_ok=True)
        rec.write_text(hashlib.sha256(text.encode("utf-8")).hexdigest(), encoding="utf-8")


def _write(event: Event, path: Path, text: str, force: bool) -> str:
    if path.exists() and path.stat().st_size > 0 and not force:
        return "보존"
    path.parent.mkdir(parents=True, exist_ok=True)
    existed = path.exists()
    write_tool(event, path, text)
    return "덮어씀" if existed else "생성"


def run(event: Event, force: bool = False) -> list[tuple[Path, str]]:
    """[(경로, 생성|덮어씀|보존)]. 비어 있지 않은 기존 파일은 force 일 때만 덮어쓴다."""
    states = collect(event)
    plan = [
        (event.transcripts_dir / "README.md", transcripts_readme_text(event, states)),
        (event.notes_dir / "README.md", notes_readme_text(event, states)),
        *[(event.note_path(st.talk), note_text(event, st)) for st in states],
        (event.report_path, report_text(event, states)),
    ]
    return [(path, _write(event, path, text, force)) for path, text in plan]


# fill: 사실에서 채울 수 있는 자리 --------------------------------------------------

QUALITY_PREFIX = "> Quality note: 기계 집계 —"
ASR_PLACEHOLDER = "<model>, <device>, <YYYY-MM-DD>"
PREPROC_PLACEHOLDER = " <증폭·잡음 제거를 했으면 적고, 없으면 이 문장을 지운다>"
PREPROC_FILLED = " 증폭·잡음 제거는 하지 않았다(`lecture-recon ingest` 는 변환만 한다)."
CROSS_PLACEHOLDER = "교차 대조: <대조한 범위를 적는다>"
CROSS_FILLED = "교차 대조: 기록 없음(사람이 대조하면 적는다)"
# 전사 README 검증 기록: 템플릿 자리, 또는 교정 기록이 없을 때 fill 이 쓴 줄(correct 가 나중에 바꾼다)
VERIFY_PLACEHOLDER_RE = re.compile(r"^- (?:<YYYY-MM-DD>: <무엇을 어떻게 확인했는지>\.|\d{4}-\d{2}-\d{2}: 교정 기록 없음\().*$",
                                   re.M)
# 아래 줄들은 "CLI 가 다시 만들 수 있는 모양" 과 줄 전체가 똑같을 때만 바꾼다. 사람이 한 글자라도 덧붙이거나 고치면
# 모양이 깨져 그대로 둔다(fill 은 사람이 쓴 내용을 덮지 않는다).
ROUND_TAIL = " 고유명사·용어는 슬라이드·원문·공식 정보로 뒷받침될 때만 고쳤다."
_ROUNDS = r"(?:<N>차 \(<YYYY-MM-DD>\)|(?:\d{2,} )?\d+차 \([^)\n]*\)(?:, \d{2,} \d+차 \([^)\n]*\))*)"
ROUND_LINE_RE = re.compile(rf"^- 교정: {_ROUNDS}\.{re.escape(ROUND_TAIL)}$", re.M)
QUALITY_LINE_RE = re.compile(r"^> Quality note: 기계 집계 — (?:ASR `[^`\n]*`\([^)\n]*\) 세그먼트 \d+개|ASR 기록 없음\(work/asr/\d+\.json\)), "
                             r"남은 `\[불명확\]` \d+개\. 녹음 상태는 사람이 보충한다\.$")
DATA_LINE_RE = re.compile(r"^- 전사: \d+건, 문단 \d+개 \((?:초안의|남은) `\[불명확\]` \d+개\)$", re.M)
PHOTO_LINE_RE = re.compile(r"^- 사진: \d+장 \(슬라이드 (?:<S>|\d+)장\)$", re.M)
TALK_TABLE_HEAD = "| 번호 | 노트 | 발표자·소속 | 제목 | 사진 |"
PHOTO_CELL_RE = re.compile(r" \| (?:\d+장|없음) \|$")
WORKLOG_LINE_RE = re.compile(r"^- \d{4}-\d{2}-\d{2}: `lecture-recon (?:correct \d+` \d+차 교정|claims \d+` claim \d+개|"
                             r"verify \d+` 원문 대조 claim \d+개|notes \d+` 노트 작성|report` 종합 리포트 작성)\([^()\s]+\)\.$")
UNKNOWN = "미기재"


def quality_line(event: Event, talk: Talk, text: str) -> str:
    """전사 Quality note 의 기계 집계 한 문장(ASR 모델·세그먼트 수·남은 [불명확] 수). 녹음 상태는 사람이 보충한다."""
    meta = asr_meta(event, talk)
    n_unclear = sum(len(transcript.unclear_spans(p.text)) for p in transcript.parse(text).paragraphs)
    asr = (f"ASR `{meta.get('model', '?')}`({meta.get('device', '?')}) 세그먼트 {len(meta.get('segments') or [])}개"
           if meta else f"ASR 기록 없음(work/asr/{talk.nn}.json)")
    return f"{QUALITY_PREFIX} {asr}, 남은 `[불명확]` {n_unclear}개. 녹음 상태는 사람이 보충한다."


def fill_quality(event: Event, talk: Talk, text: str) -> str:
    """Quality note 가 placeholder 이거나 기계 집계 줄 그대로면 지금 값으로 다시 쓴다. 사람이 손댄 줄은 둔다."""
    lines = text.split("\n")
    for i, line in enumerate(lines):
        if line.strip() == transcript.QUALITY_PLACEHOLDER or QUALITY_LINE_RE.fullmatch(line):
            lines[i] = quality_line(event, talk, text)
            break
    return "\n".join(lines)


def correct_record_line(rec: dict) -> str:
    """work/correct/NN.json 기록 하나 → 전사 README 검증 기록 줄."""
    n_guess = sum(a.get("level") == "추정" for a in rec.get("applied", []))
    n_rej = len(rec.get("rejected", [])) + len(rec.get("chapters_rejected", []))
    return (f"- {rec['date']}: `lecture-recon correct` {rec['round']}차 교정({rec['backend']}): 발표 {rec['talk']} 장 제목 "
            f"{len(rec.get('chapters', []))}개, 치환 {len(rec.get('applied', []))}개 적용(추정 {n_guess}개), "
            f"{n_rej}개 거부. 풀어 낸 `[불명확]` {rec.get('unclear_resolved', 0)}개. 기록: work/correct/{rec['talk']}.json.")


def correct_record_lines(event: Event) -> list[str]:
    """모든 발표의 교정 기록(이전 차수 포함) → 검증 기록 줄."""
    return [correct_record_line(r) for t in event.talks for r in _correct_records(event, t)]


def _load(path: Path):
    if not path.exists():
        return None
    try:
        text = path.read_text(encoding="utf-8")
        return yaml.safe_load(text) if path.suffix == ".yaml" else json.loads(text)
    except (json.JSONDecodeError, yaml.YAMLError, OSError):
        return None


def _correct_records(event: Event, talk: Talk) -> list[dict]:
    rec = _load(event.work_dir / "correct" / f"{talk.nn}.json")
    if not isinstance(rec, dict):
        return []
    return [r for r in [*rec.get("history", []), {k: v for k, v in rec.items() if k != "history"}]
            if isinstance(r, dict) and r.get("date") and r.get("round") is not None]


def _round_text(event: Event, states: list[TalkState]) -> str:
    labels = [(st.talk.nn, context.correction_label(event.transcript_path(st.talk).read_text(encoding="utf-8")))
              for st in states if st.paragraphs is not None]
    if not labels:
        return ""
    if len({label for _, label in labels}) == 1:
        return labels[0][1]
    return ", ".join(f"{nn} {label}" for nn, label in labels)


def fill_transcripts_readme(event: Event, states: list[TalkState] | None = None, today: str | None = None) -> bool:
    """전사 README: 남은 자리(전처리·ASR·복수 녹음 교차·검증 기록 = work/correct/NN.json)를 채우고, 교정 차수 줄은
    전사 머리말에서 발표별로 다시 쓴다(줄이 CLI 모양 그대로일 때만)."""
    path = event.transcripts_dir / "README.md"
    if not path.exists():
        return False
    states = states if states is not None else collect(event)
    text = old = path.read_text(encoding="utf-8")
    text = text.replace(PREPROC_PLACEHOLDER, PREPROC_FILLED).replace(CROSS_PLACEHOLDER, CROSS_FILLED)
    asr = _asr_line(event, states)
    if asr:
        text = text.replace(ASR_PLACEHOLDER, asr)
    rounds = _round_text(event, states)
    if rounds:
        text = ROUND_LINE_RE.sub(lambda _m: f"- 교정: {rounds}.{ROUND_TAIL}", text, count=1)
    m = VERIFY_PLACEHOLDER_RE.search(text)
    if m:
        lines = correct_record_lines(event)
        if not lines:
            if m.group(0).startswith("- <YYYY"):
                lines = [f"- {today or context.today()}: 교정 기록 없음(`lecture-recon correct` 전). 풀어 낸 `[불명확]` 0개."]
            else:
                lines = [m.group(0)]
        text = text[:m.start()] + "\n".join(lines) + text[m.end():]
    if text != old:
        write_tool(event, path, text)
    return text != old


def work_log(event: Event) -> list[tuple[str, str]]:
    """작업 기록 (key, 줄): lecture-recon 기록 파일(work/correct·claims·verify·notes, report.json)에서."""
    out = []
    for t in event.talks:
        nn = t.nn
        for r in _correct_records(event, t):
            key = f"- {r['date']}: `lecture-recon correct {nn}` {r['round']}차"
            out.append((key, f"{key} 교정({r.get('backend', '?')})."))
        c = _load(event.work_dir / "claims" / f"{nn}.yaml")
        if isinstance(c, dict) and c.get("created"):
            key = f"- {c['created']}: `lecture-recon claims {nn}`"
            out.append((key, f"{key} claim {len(c.get('claims') or [])}개({c.get('backend', '?')})."))
        v = _load(event.work_dir / "verify" / f"{nn}.json")
        if isinstance(v, dict) and v.get("date"):
            key = f"- {v['date']}: `lecture-recon verify {nn}`"
            out.append((key, f"{key} 원문 대조 claim {len(v.get('claims') or [])}개({v.get('backend', '?')})."))
        n = _load(event.work_dir / "notes" / f"{nn}.json")
        if isinstance(n, dict) and n.get("written") and n.get("backend"):
            key = f"- {str(n['written'])[:10]}: `lecture-recon notes {nn}`"
            out.append((key, f"{key} 노트 작성({n['backend']})."))
    r = _load(event.work_dir / "report.json")
    if isinstance(r, dict) and r.get("written") and r.get("backend"):
        key = f"- {str(r['written'])[:10]}: `lecture-recon report`"
        out.append((key, f"{key} 종합 리포트 작성({r['backend']})."))
    return out


def _section_bounds(lines: list[str], title: str) -> tuple[int, int] | None:
    start = next((i for i, x in enumerate(lines) if x.strip() == f"## {title}"), None)
    if start is None:
        return None
    end = next((i for i in range(start + 1, len(lines)) if lines[i].startswith("## ")), len(lines))
    while end > start + 1 and not lines[end - 1].strip():
        end -= 1
    return start, end


def _sources_text(event: Event) -> str:
    return ", ".join(f"[{x}]({x})" if re.match(r"^https?://", x) else x for x in event.sources)


def fill_notes_readme(event: Event, states: list[TalkState] | None = None) -> bool:
    """노트 README: 머리 인용(재구성자·원자료·교정 차수), 행사 정보의 비어 있던 칸(event.yaml 에 생기면 다시 채움),
    발표 목록 표의 사진 칸, 자료 규모(문단·남은 불명확·슬라이드), 작업 기록(기록 파일에서). CLI 모양 그대로인 줄만."""
    path = event.notes_dir / "README.md"
    if not path.exists():
        return False
    states = states if states is not None else collect(event)
    old = path.read_text(encoding="utf-8")
    rounds = sorted({context.correction_round(event.transcript_path(st.talk).read_text(encoding="utf-8"))
                     for st in states if st.paragraphs is not None})
    round_txt = (f"{rounds[0]}차" if len(rounds) == 1 else f"{rounds[0]}–{rounds[-1]}차") if rounds else "0차"
    totals = _totals(states)
    n_slides = sum(len(context.slide_numbers(context.slides_table(event, st.talk))) for st in states)
    slug = re.escape(event.slug)
    head_re = re.compile(rf"^> 재구성: (?P<who>[^·\n]+?) · 원자료: (?:현장 녹음과 사진|현장 녹음\(사진 없음\)) · "
                         rf"전사: \[{slug}-transcripts\]\(\.\./{slug}-transcripts/README\.md\) "
                         rf"\((?:<N>|\d+(?:–\d+)?)차 교정본\)$")
    facts = {"장소": event.venue, "주관": event.organizer, "근거": _sources_text(event)}
    blanks = {"장소": "<장소>", "주관": "<주관>", "근거": "<근거 링크>"}
    rows = {r.split(" | ", 1)[0]: r for r in _talk_rows(states)}
    lines = old.split("\n")
    for i, line in enumerate(lines):
        m = head_re.match(line)
        if m and m.group("who") in (event.reconstructor, UNKNOWN, "<재구성자>"):
            lines[i] = (f"> 재구성: {event.reconstructor or UNKNOWN} · 원자료: "
                        + ("현장 녹음과 사진" if totals["n_photos"] else "현장 녹음(사진 없음)")
                        + f" · 전사: [{event.slug}-transcripts](../{event.slug}-transcripts/README.md) ({round_txt} 교정본)")
        elif (f := next((k for k in facts if line in (f"- {k}: {blanks[k]}", f"- {k}: {UNKNOWN}")), None)):
            lines[i] = f"- {f}: {facts[f] or UNKNOWN}"
        elif DATA_LINE_RE.fullmatch(line):
            lines[i] = (f"- 전사: {totals['n_transcripts']}건, 문단 {totals['n_paragraphs']}개 "
                        f"(남은 `[불명확]` {totals['n_unclear']}개)")
        elif PHOTO_LINE_RE.fullmatch(line):
            lines[i] = f"- 사진: {totals['n_photos']}장 (슬라이드 {n_slides}장)"
        elif (gen := rows.get(line.split(" | ", 1)[0])) and line != gen \
                and line.rsplit(" | ", 1)[0] == gen.rsplit(" | ", 1)[0] and PHOTO_CELL_RE.search(line):
            lines[i] = gen  # 사진 칸만 CLI 값과 다르다(ingest 뒤): 사진 유무를 고친다. 손댄 행·칸은 둔다
    bounds = _section_bounds(lines, "작업 기록")
    if bounds:
        start, end = bounds
        if end == start + 1:
            lines.insert(end, "")
            end += 1
        for key, line in work_log(event):
            at = next((i for i in range(start + 1, end) if lines[i].startswith(key)), None)
            if at is None:
                lines.insert(end, line)
                end += 1
            elif WORKLOG_LINE_RE.fullmatch(lines[at]):
                lines[at] = line   # 같은 날 같은 명령을 다시 돌렸다. 사람이 덧붙인 줄은 둔다
    text = "\n".join(lines)
    if text != old:
        write_tool(event, path, text)
    return text != old


def fill(event: Event) -> list[Path]:
    """LLM 없이 채울 수 있는 자리를 채운다. 바뀐 파일 목록."""
    states = collect(event)
    changed = []
    for st in states:
        path = event.transcript_path(st.talk)
        if st.paragraphs is None:
            continue
        text = path.read_text(encoding="utf-8")
        new = fill_quality(event, st.talk, text)
        if new != text:
            context.write_atomic(path, new)
            changed.append(path)
    if fill_transcripts_readme(event, states):
        changed.append(event.transcripts_dir / "README.md")
    if fill_notes_readme(event, states):
        changed.append(event.notes_dir / "README.md")
    return changed
