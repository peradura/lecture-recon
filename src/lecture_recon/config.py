"""event.yaml 로드·검증과 행사 디렉터리 경로 규칙."""

from __future__ import annotations

import datetime as dt
import re
from dataclasses import dataclass, field
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import yaml

EVENT_FILE = "event.yaml"
LANGUAGES = ("ko", "en", "mixed")
SLUG_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")


class LectureReconError(Exception):
    """사용자에게 그대로 보여 줄 오류."""


class ConfigError(LectureReconError):
    def __init__(self, path: Path, errors: list[str]):
        self.path = path
        self.errors = errors
        super().__init__(f"{path}\n" + "\n".join(f"  - {e}" for e in errors))


def file_stem(number: int, org: str, speaker: str) -> str:
    """`NN_<org>_<speaker>` (공백·`/` → `_`, 소속이 비면 생략)."""
    parts = [f"{number:02d}"] + [p.strip() for p in (org, speaker) if p and p.strip()]
    return "_".join(re.sub(r"[\s/]+", "_", p) for p in parts)


@dataclass
class Recording:
    file: str
    started_at: dt.datetime | None = None  # 행사 timezone 기준 naive 시각
    offset_sec: float = 0.0


@dataclass
class Talk:
    number: int
    speaker: str
    org: str
    title: str
    language: str
    recordings: list[Recording]
    photos_dir: str | None = None
    aliases: list[str] = field(default_factory=list)  # 발표자 측 별칭(영문 이름·연구실·회사): verify 의 귀속 검사

    @property
    def nn(self) -> str:
        return f"{self.number:02d}"

    @property
    def stem(self) -> str:
        return file_stem(self.number, self.org, self.speaker)

    @property
    def speaker_org(self) -> str:
        return f"{self.speaker}, {self.org}" if self.org else self.speaker

    @property
    def asr_language(self) -> str | None:
        return None if self.language == "mixed" else self.language

    def key(self, k: int = 0) -> str:
        """녹음 k(0=주 녹음)의 work 파일 이름: `NN`, `NN_2`, `NN_3`, …"""
        return self.nn if k == 0 else f"{self.nn}_{k + 1}"


@dataclass
class Event:
    root: Path
    slug: str
    name: str
    date: str
    timezone: str = "Asia/Seoul"
    venue: str = ""
    organizer: str = ""
    sources: list[str] = field(default_factory=list)
    reconstructor: str = ""
    talks: list[Talk] = field(default_factory=list)

    @property
    def work_dir(self) -> Path:
        return self.root / "work"

    @property
    def transcripts_dir(self) -> Path:
        return self.root / f"{self.slug}-transcripts"

    @property
    def notes_dir(self) -> Path:
        return self.root / f"{self.slug}-notes"

    @property
    def report_path(self) -> Path:
        return self.root / f"{self.slug}-report.md"

    def audio_path(self, talk: Talk, k: int = 0) -> Path:
        return self.work_dir / "audio" / f"{talk.key(k)}.wav"

    def asr_path(self, talk: Talk, k: int = 0) -> Path:
        return self.work_dir / "asr" / f"{talk.key(k)}.json"

    def photos_manifest_path(self, talk: Talk) -> Path:
        return self.work_dir / "photos" / f"{talk.nn}.json"

    def align_path(self, talk: Talk) -> Path:
        return self.work_dir / "align" / f"{talk.nn}.json"

    def slides_dir(self, talk: Talk) -> Path:
        return self.notes_dir / "slides" / talk.nn

    def transcript_path(self, talk: Talk) -> Path:
        return self.transcripts_dir / f"{talk.stem}.md"

    def note_path(self, talk: Talk) -> Path:
        return self.notes_dir / f"{talk.stem}.md"

    def rel(self, path: Path) -> str:
        try:
            return str(path.relative_to(self.root))
        except ValueError:
            return str(path)

    def select(self, number: int | None) -> list[Talk]:
        if not self.talks:
            raise LectureReconError("event.yaml 에 talks 가 비어 있다. 발표를 먼저 적는다.")
        if number is None:
            return list(self.talks)
        for t in self.talks:
            if t.number == number:
                return [t]
        raise LectureReconError(f"event.yaml 에 발표 {number:02d} 가 없다.")


def _text(value) -> str:
    if value is None:
        return ""
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return str(value)
    return value if isinstance(value, str) else ""


def _parse_started_at(value, tz: ZoneInfo | None) -> dt.datetime:
    if isinstance(value, dt.datetime):
        ts = value
    elif isinstance(value, str):
        ts = dt.datetime.fromisoformat(value.strip())
    else:
        raise ValueError
    if ts.tzinfo is not None and tz is not None:
        ts = ts.astimezone(tz).replace(tzinfo=None)
    return ts.replace(tzinfo=None)


def _parse_recording(raw, where: str, tz: ZoneInfo | None, errors: list[str]) -> Recording | None:
    if not isinstance(raw, dict):
        errors.append(f"{where}: 매핑(file, started_at, offset_sec)이어야 한다")
        return None
    file = _text(raw.get("file")).strip()
    if not file:
        errors.append(f"{where}.file: 비어 있다")
    started_at = None
    if raw.get("started_at") not in (None, ""):
        try:
            started_at = _parse_started_at(raw["started_at"], tz)
        except ValueError:
            errors.append(f"{where}.started_at: ISO 시각(예: \"2026-10-20T10:05:00\")이 아니다: {raw['started_at']!r}")
    offset = raw.get("offset_sec", 0)
    if offset is None:
        offset = 0
    if isinstance(offset, bool) or not isinstance(offset, (int, float)):
        errors.append(f"{where}.offset_sec: 숫자(초)여야 한다: {offset!r}")
        offset = 0
    return Recording(file=file, started_at=started_at, offset_sec=float(offset))


def _parse_talk(raw, i: int, tz: ZoneInfo | None, errors: list[str]) -> Talk | None:
    where = f"talks[{i}]"
    if not isinstance(raw, dict):
        errors.append(f"{where}: 매핑이어야 한다")
        return None
    number = raw.get("number")
    if isinstance(number, bool) or not isinstance(number, int) or number < 1:
        errors.append(f"{where}.number: 1 이상의 정수여야 한다: {number!r}")
        number = 0
    speaker = _text(raw.get("speaker")).strip()
    title = _text(raw.get("title")).strip()
    if not speaker:
        errors.append(f"{where}.speaker: 비어 있다")
    if not title:
        errors.append(f"{where}.title: 비어 있다")
    language = _text(raw.get("language") or "ko").strip()
    if language not in LANGUAGES:
        errors.append(f"{where}.language: {'|'.join(LANGUAGES)} 중 하나여야 한다: {language!r}")
    recs_raw = raw.get("recordings")
    recordings: list[Recording] = []
    if not isinstance(recs_raw, list) or not recs_raw:
        errors.append(f"{where}.recordings: 녹음이 하나 이상 있어야 한다")
    else:
        for k, r in enumerate(recs_raw):
            rec = _parse_recording(r, f"{where}.recordings[{k}]", tz, errors)
            if rec:
                recordings.append(rec)
    photos_dir = _text(raw.get("photos_dir")).strip() or None
    aliases = raw.get("aliases")
    if aliases is None:   # 없거나 `aliases:` 만 쓴 것. false·0·{} 같은 값은 형식 오류
        aliases = []
    if not isinstance(aliases, list) or not all(isinstance(a, str) for a in aliases):
        errors.append(f"{where}.aliases: 문자열 목록이어야 한다: {aliases!r}")
        aliases = []
    return Talk(
        number=number,
        speaker=speaker,
        org=_text(raw.get("org")).strip(),
        title=title,
        language=language,
        recordings=recordings,
        photos_dir=photos_dir,
        aliases=[a.strip() for a in aliases if a.strip()],
    )


def load_event(event_dir: str | Path) -> Event:
    root = Path(event_dir).expanduser().resolve()
    path = root / EVENT_FILE
    if not path.is_file():
        raise ConfigError(path, ["파일이 없다 (`lecture-recon init <slug>` 으로 만든다)"])
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as e:
        raise ConfigError(path, [f"YAML 파싱 실패: {e}"]) from e
    if not isinstance(data, dict):
        raise ConfigError(path, ["최상위가 매핑이어야 한다"])

    errors: list[str] = []
    slug = _text(data.get("slug")).strip()
    if not SLUG_RE.match(slug):
        errors.append(f"slug: 영문·숫자·`-`·`_`·`.` 만 쓴다: {slug!r}")
    name = _text(data.get("name")).strip()
    if not name:
        errors.append("name: 비어 있다")

    date_raw = data.get("date")
    date = ""
    if isinstance(date_raw, dt.datetime):
        errors.append(f"date: 날짜만 쓴다(YYYY-MM-DD): {date_raw!r}")
    elif isinstance(date_raw, dt.date):
        date = date_raw.isoformat()
    else:
        try:
            date = dt.date.fromisoformat(_text(date_raw).strip()).isoformat()
        except ValueError:
            errors.append(f"date: YYYY-MM-DD 여야 한다: {date_raw!r}")

    timezone = _text(data.get("timezone") or "Asia/Seoul").strip()
    tz: ZoneInfo | None = None
    try:
        tz = ZoneInfo(timezone)
    except (ZoneInfoNotFoundError, ValueError):
        errors.append(f"timezone: 알 수 없는 시간대: {timezone!r}")

    sources = data.get("sources") or []
    if not isinstance(sources, list) or not all(isinstance(s, str) for s in sources):
        errors.append("sources: 문자열(URL) 목록이어야 한다")
        sources = []

    talks_raw = data.get("talks") or []
    talks: list[Talk] = []
    if not isinstance(talks_raw, list):
        errors.append("talks: 목록이어야 한다")
    else:
        for i, raw in enumerate(talks_raw):
            talk = _parse_talk(raw, i, tz, errors)
            if talk:
                talks.append(talk)
    seen: set[int] = set()
    for t in talks:
        if t.number and t.number in seen:
            errors.append(f"talks: number {t.number} 가 중복된다")
        seen.add(t.number)

    if errors:
        raise ConfigError(path, errors)
    talks.sort(key=lambda t: t.number)
    return Event(
        root=root,
        slug=slug,
        name=name,
        date=date,
        timezone=timezone,
        venue=_text(data.get("venue")).strip(),
        organizer=_text(data.get("organizer")).strip(),
        sources=sources,
        reconstructor=_text(data.get("reconstructor")).strip(),
        talks=talks,
    )
