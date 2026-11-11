import datetime as dt

import pytest
import yaml

import synth
from lecture_recon.__main__ import main
from lecture_recon.config import ConfigError, LectureReconError, file_stem, load_event


def write(tmp_path, data):
    d = tmp_path / "ev"
    d.mkdir(exist_ok=True)
    (d / "event.yaml").write_text(yaml.safe_dump(data, allow_unicode=True), encoding="utf-8")
    return d


def base():
    return yaml.safe_load(synth.EVENT_YAML)


def test_valid_event(event):
    assert event.slug == synth.SLUG and event.date == "2026-10-20"
    t1, t2 = event.talks
    assert t1.stem == "01_42dot_이희석" and t2.stem == "02_서울대_김_철수"
    assert t1.recordings[0].started_at == dt.datetime(2026, 10, 20, 10, 5)
    assert t2.recordings[0].started_at is None and t2.recordings[0].offset_sec == 0
    assert event.transcript_path(t2).name == "02_서울대_김_철수.md"
    assert event.asr_path(t1, 1).name == "01_2.json"


def test_file_stem():
    assert file_stem(3, "서울대", "이경무") == "03_서울대_이경무"
    assert file_stem(12, "", "Jane  Doe") == "12_Jane_Doe"


def test_missing_file(tmp_path):
    with pytest.raises(ConfigError, match="init"):
        load_event(tmp_path)


def test_errors_are_collected(tmp_path):
    data = base()
    data["date"] = "2026-13-40"
    data["timezone"] = "Mars/Base"
    data["talks"][0]["language"] = "jp"
    data["talks"][0]["recordings"][0]["offset_sec"] = "ten"
    data["talks"][1]["number"] = 1
    data["talks"][1]["recordings"] = []
    with pytest.raises(ConfigError) as exc:
        load_event(write(tmp_path, data))
    msgs = "\n".join(exc.value.errors)
    for needle in ("date", "timezone", "talks[0].language", "talks[0].recordings[0].offset_sec",
                   "talks[1].recordings", "중복"):
        assert needle in msgs


@pytest.mark.parametrize("patch, needle", [
    (lambda d: d.update(slug="bad slug"), "slug"),
    (lambda d: d.pop("name"), "name"),
    (lambda d: d["talks"][0].pop("speaker"), "talks[0].speaker"),
    (lambda d: d["talks"][0]["recordings"][0].update(started_at="어제"), "started_at"),
    (lambda d: d["talks"][0]["recordings"][0].pop("file"), "recordings[0].file"),
    (lambda d: d.update(talks={"number": 1}), "talks: 목록"),
])
def test_single_errors(tmp_path, patch, needle):
    data = base()
    patch(data)
    with pytest.raises(ConfigError) as exc:
        load_event(write(tmp_path, data))
    assert any(needle in e for e in exc.value.errors)


def test_aware_started_at_is_converted_to_event_timezone(tmp_path):
    data = base()
    data["talks"][0]["recordings"][0]["started_at"] = "2026-10-20T01:05:00+00:00"
    ev = load_event(write(tmp_path, data))
    assert ev.talks[0].recordings[0].started_at == dt.datetime(2026, 10, 20, 10, 5)


def test_init_skeleton(tmp_path):
    assert main(["init", "my-event", "--root", str(tmp_path), "--name", "내 행사: 2026", "--date", "2026-11-02"]) == 0
    ev = load_event(tmp_path / "my-event")
    assert (ev.name, ev.date, ev.talks) == ("내 행사: 2026", "2026-11-02", [])
    assert (tmp_path / "my-event" / ".gitignore").read_text().count("raw/") == 1
    with pytest.raises(LectureReconError, match="talks"):
        ev.select(None)
    assert main(["init", "my-event", "--root", str(tmp_path)]) == 2  # 이미 있음


def test_init_without_date_requires_filling(tmp_path):
    assert main(["init", "no-date", "--root", str(tmp_path)]) == 0
    with pytest.raises(ConfigError, match="date"):
        load_event(tmp_path / "no-date")


def test_talk_aliases_are_optional_string_list(tmp_path, event):
    assert event.talks[0].aliases == []
    data = base()
    data["talks"][0]["aliases"] = ["Jinwoo Shin", " KAIST AI ", ""]
    assert load_event(write(tmp_path, data)).talks[0].aliases == ["Jinwoo Shin", "KAIST AI"]
    data["talks"][0]["aliases"] = "KAIST"
    with pytest.raises(ConfigError) as exc:
        load_event(write(tmp_path, data))
    assert any("talks[0].aliases" in e for e in exc.value.errors)


@pytest.mark.parametrize("value", [False, 0, {}, "KAIST", [1]])
def test_talk_aliases_non_list_is_an_error(tmp_path, value):
    data = base()
    data["talks"][0]["aliases"] = value
    with pytest.raises(ConfigError) as exc:
        load_event(write(tmp_path, data))
    assert any("talks[0].aliases" in e for e in exc.value.errors)


def test_talk_aliases_null_is_empty(tmp_path):
    data = base()
    data["talks"][0]["aliases"] = None
    assert load_event(write(tmp_path, data)).talks[0].aliases == []
