"""슬라이드 사진: 촬영 시각(EXIF DateTimeOriginal, 없으면 mtime), manifest, 판독용 축소본."""

from __future__ import annotations

import datetime as dt
import json
from pathlib import Path
from zoneinfo import ZoneInfo

from PIL import Image, ImageOps

from .config import Event, LectureReconError, Talk

IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".webp", ".tif", ".tiff"}
UNSUPPORTED_EXTS = {".heic", ".heif"}
MAX_SIDE = 1600
EXIF_IFD = 0x8769
DATETIME_ORIGINAL = 36867


def taken_at(path: Path, timezone: str) -> tuple[dt.datetime, str]:
    """(촬영 시각, 근거) — 근거는 "exif" 또는 "mtime". 시각은 행사 timezone 기준 naive."""
    try:
        with Image.open(path) as im:
            raw = im.getexif().get_ifd(EXIF_IFD).get(DATETIME_ORIGINAL)
    except OSError:
        raw = None
    if isinstance(raw, str):
        try:
            return dt.datetime.strptime(raw.strip("\x00 "), "%Y:%m:%d %H:%M:%S"), "exif"
        except ValueError:
            pass
    mtime = dt.datetime.fromtimestamp(path.stat().st_mtime, ZoneInfo(timezone))
    return mtime.replace(tzinfo=None), "mtime"


def resize_copy(src: Path, dst: Path, max_side: int = MAX_SIDE) -> tuple[int, int]:
    """회전(EXIF orientation) 반영 후 긴 변 ≤ max_side 인 JPEG 사본을 만든다."""
    with Image.open(src) as im:
        out = ImageOps.exif_transpose(im).convert("RGB")
    out.thumbnail((max_side, max_side))
    dst.parent.mkdir(parents=True, exist_ok=True)
    out.save(dst, "JPEG", quality=85)
    return out.size


def list_images(directory: Path) -> tuple[list[Path], list[Path]]:
    """(처리할 이미지, 지원하지 않는 형식) — 파일 이름순."""
    files = sorted(p for p in directory.iterdir() if p.is_file())
    return ([p for p in files if p.suffix.lower() in IMAGE_EXTS],
            [p for p in files if p.suffix.lower() in UNSUPPORTED_EXTS])


def ingest(event: Event, talk: Talk) -> tuple[dict, list[Path]]:
    """사진 manifest(work/photos/NN.json)와 축소본(<slug>-notes/slides/NN/)을 만든다."""
    directory = event.root / (talk.photos_dir or "")
    if not talk.photos_dir or not directory.is_dir():
        raise LectureReconError(f"{talk.nn}: 사진 폴더가 없다: {talk.photos_dir}")
    images, unsupported = list_images(directory)
    entries = []
    for src in images:
        when, source = taken_at(src, event.timezone)
        dst = event.slides_dir(talk) / f"{src.stem}.jpg"
        width, height = resize_copy(src, dst)
        entries.append({
            "file": event.rel(src),
            "slide_copy": event.rel(dst),
            "taken_at": when.isoformat(),
            "time_source": source,
            "size": [width, height],
        })
    entries.sort(key=lambda e: (e["taken_at"], e["file"]))
    manifest = {"talk": talk.number, "photos_dir": talk.photos_dir, "photos": entries}
    path = event.photos_manifest_path(talk)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(manifest, ensure_ascii=False, indent=1), encoding="utf-8")
    return manifest, unsupported


def load_manifest(event: Event, talk: Talk) -> dict | None:
    path = event.photos_manifest_path(talk)
    if not path.exists():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def count(event: Event, talk: Talk) -> tuple[int, bool]:
    """(사진 수, ingest 했는지). manifest 가 없으면 photos_dir 의 이미지 수."""
    manifest = load_manifest(event, talk)
    if manifest is not None:
        return len(manifest["photos"]), True
    directory = event.root / (talk.photos_dir or "")
    if talk.photos_dir and directory.is_dir():
        return len(list_images(directory)[0]), False
    return 0, False
