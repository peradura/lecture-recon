from lecture_recon import transcript
from lecture_recon.transcript import Segment, paragraphize


def seg(start, end, text="말", logprob=-0.2, no_speech=0.0):
    return Segment(start=start, end=end, text=text, avg_logprob=logprob, no_speech_prob=no_speech)


def test_break_on_silence_gap():
    paras = paragraphize([seg(0, 2), seg(2.5, 4), seg(5.5, 7), seg(8.9, 10)], talk_number=3)
    # 4→5.5 (1.5초) 에서 끊고, 7→8.9 (1.9초) 에서 끊는다
    assert [p.id for p in paras] == ["03-001", "03-002", "03-003"]
    assert [p.start for p in paras] == [0, 5.5, 8.9]


def test_break_on_duration_and_chars():
    long = [seg(i * 10, i * 10 + 9.8) for i in range(6)]  # 간격 0.2초, 세그먼트 5개째에서 49.8초
    paras = paragraphize(long, 1)
    assert [p.start for p in paras] == [0, 50]
    wordy = [seg(i, i + 0.9, "가" * 150) for i in range(4)]  # 150+1+150+1+150 = 452자에서 끊김
    paras = paragraphize(wordy, 1)
    assert len(paras) == 2 and paras[1].start == 3


def test_unclear_wrapping_and_empty_segments():
    paras = paragraphize([
        seg(0, 1, " 분명한 말"),
        seg(1.1, 2, ' 웅얼 "거림"', logprob=-1.2),
        seg(2.1, 3, " 잡음", no_speech=0.7),
        seg(3.1, 4, "   "),
        seg(4.1, 5, " 경계값", logprob=-1.0, no_speech=0.6),
    ], 1)
    assert len(paras) == 1
    assert paras[0].text == """분명한 말 [불명확: "웅얼 '거림'"] [불명확: "잡음"] 경계값"""
    assert transcript.unclear_spans(paras[0].text) == ["웅얼 '거림'", "잡음"]


def test_unclear_spans_variants():
    text = '앞 [불명확] 중간 [불명확: 들리는 대로 "에이전트"] 끝 [불명확: "x"]'
    assert transcript.unclear_spans(text) == ["", '들리는 대로 "에이전트"', "x"]


def test_fmt_ts():
    assert transcript.fmt_ts(0) == "00:00:00"
    assert transcript.fmt_ts(3725.9) == "01:02:05"


def test_write_parse_roundtrip(event):
    talk = event.talks[0]
    segs = [seg(0, 4, "첫 문단"), seg(6, 9, "둘째 문단", logprob=-2), seg(70, 75, "셋째")]
    paras = paragraphize(segs, talk.number)
    md = transcript.render(event, talk, paras, {"model": "large-v3", "device": "cpu", "created": "2026-10-21",
                                                "duration": 80.0, "segments": []})
    assert '<a id="segment-01-001"></a>\n**[01-001] [00:00:00]** 첫 문단' in md
    assert "- Duration: 00:01:20" in md
    assert "started 10:05:00 (event.yaml 기준)" in md
    parsed = transcript.parse(md)
    assert [(p.id, p.start, p.text) for p in parsed.paragraphs] == [(p.id, int(p.start), p.text) for p in paras]
    assert set(parsed.anchors) == {f"segment-{p.id}" for p in paras}
    assert parsed.chapters == [] and parsed.duration == 80


def test_parse_corrected_transcript():
    md = "\n".join([
        "# 제목", "", "- Duration: 00:10:00", "",
        "## [00:00:00] 도입", "",
        '<a id="segment-02-001"></a>', "**[02-001] [00:00:00]** 첫 줄", "이어지는 줄", "",
        "## [00:01:05] 본론", "",
        '<a id="segment-02-002"></a>', "**[02-002] [00:01:05]** 본론 [불명확]", "",
    ])
    parsed = transcript.parse(md)
    assert [(p.id, p.start) for p in parsed.paragraphs] == [("02-001", 0), ("02-002", 65)]
    assert parsed.paragraphs[0].text == "첫 줄 이어지는 줄"
    assert parsed.chapters == [(0, "도입"), (65, "본론")]
    assert parsed.duration == 600
