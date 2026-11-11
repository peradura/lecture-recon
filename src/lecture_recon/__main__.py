"""lecture-recon CLI (argparse). `lecture-recon <command> …` 또는 `python -m lecture_recon <command> …`."""

from __future__ import annotations

import argparse
import datetime as dt
import json
import math
import sys
from pathlib import Path

from . import align, asr, audio, compose, context, correct, lint, llm, photos, scaffold, sources, transcript, verify
from .config import EVENT_FILE, SLUG_RE, LectureReconError, load_event


def _err(msg: str) -> None:
    print(msg, file=sys.stderr)


def cmd_init(args) -> int:
    if not SLUG_RE.match(args.slug):
        raise LectureReconError(f"slug 는 영문·숫자·`-`·`_`·`.` 만 쓴다: {args.slug!r}")
    root = Path(args.root).expanduser() / args.slug
    if (root / EVENT_FILE).exists():
        raise LectureReconError(f"이미 있다: {root / EVENT_FILE}")
    for sub in ("raw/audio", "raw/photos"):
        (root / sub).mkdir(parents=True, exist_ok=True)
    text = scaffold.render("event.yaml", slug=json.dumps(args.slug), name=json.dumps(args.name or args.slug, ensure_ascii=False),
                           date=json.dumps(args.date or ""))
    (root / EVENT_FILE).write_text(text, encoding="utf-8")
    gitignore = root / ".gitignore"
    if not gitignore.exists():
        gitignore.write_text(scaffold.template("event.gitignore"), encoding="utf-8")
    print(f"생성: {root / EVENT_FILE}")
    print(f"다음: event.yaml 의 {'date, ' if not args.date else ''}talks 를 채우고 녹음은 raw/audio/, 사진은 raw/photos/NN/ 에 둔다.")
    return 0


def cmd_ingest(args) -> int:
    ev = load_event(args.event_dir)
    ok = True
    for talk in ev.select(None):
        for k, rec in enumerate(talk.recordings):
            src, dst = ev.root / rec.file, ev.audio_path(talk, k)
            if not src.exists():
                _err(f"{talk.nn}: 녹음 파일 없음: {rec.file}")
                ok = False
            elif dst.exists() and dst.stat().st_mtime >= src.stat().st_mtime:
                print(f"{talk.nn}: {ev.rel(dst)} 이미 있음")
            else:
                try:
                    audio.to_wav(src, dst)
                except LectureReconError as e:
                    _err(f"{talk.nn}: {e}")
                    ok = False
                    continue
                print(f"{talk.nn}: {rec.file} → {ev.rel(dst)} (16 kHz mono)")
        if talk.photos_dir:
            try:
                manifest, unsupported = photos.ingest(ev, talk)
            except LectureReconError as e:
                _err(str(e))
                ok = False
                continue
            shots = manifest["photos"]
            n_mtime = sum(p["time_source"] == "mtime" for p in shots)
            print(f"{talk.nn}: 사진 {len(shots)}장 → {ev.rel(ev.photos_manifest_path(talk))}, {ev.rel(ev.slides_dir(talk))}/")
            if n_mtime:
                _err(f"{talk.nn}: EXIF 촬영 시각 없는 사진 {n_mtime}장 — 파일 mtime 을 썼다(정렬 오차 가능)")
            if unsupported:
                _err(f"{talk.nn}: 지원하지 않는 형식 {len(unsupported)}장(HEIC 등) 건너뜀 — JPEG 로 바꿔 넣는다")
    return 0 if ok else 1


def cmd_asr(args) -> int:
    ev = load_event(args.event_dir)
    talks = ev.select(args.talk)
    if args.device == "cuda":
        print("주의: GPU(cuda)로 실행한다. 공유 서버 규칙상 사용자 승인을 받은 경우에만 진행한다.", flush=True)
    ok = asr.run(ev, talks, args.model, args.device, args.compute_type, args.language)
    return 0 if ok else 1


def cmd_segment(args) -> int:
    ev = load_event(args.event_dir)
    ok = True
    for talk in ev.select(args.talk):
        src, out = ev.asr_path(talk), ev.transcript_path(talk)
        if not src.exists():
            _err(f"{talk.nn}: {ev.rel(src)} 없음 — 먼저 `lecture-recon asr`")
            ok = False
            continue
        if out.exists() and out.stat().st_size > 0 and not args.force:
            _err(f"{talk.nn}: {ev.rel(out)} 이미 있음 — 교정본 보호를 위해 덮어쓰지 않는다 (다시 만들려면 --force)")
            ok = False
            continue
        data = json.loads(src.read_text(encoding="utf-8"))
        paras = transcript.paragraphize([transcript.Segment.from_dict(s) for s in data["segments"]], talk.number)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(transcript.render(ev, talk, paras, data), encoding="utf-8")
        n_unclear = sum(len(transcript.unclear_spans(p.text)) for p in paras)
        print(f"{talk.nn}: 문단 {len(paras)}개 (불명확 {n_unclear}) → {ev.rel(out)}")
    return 0 if ok else 1


def cmd_align(args) -> int:
    ev = load_event(args.event_dir)
    ok = True
    for talk in ev.select(args.talk):
        if photos.count(ev, talk)[0] == 0:
            if args.talk is not None:
                print(f"{talk.nn}: 사진 없음 — 정렬할 것이 없다")
            continue
        try:
            result, estimated = align.run(ev, talk)
        except LectureReconError as e:
            _err(str(e))
            ok = False
            continue
        if estimated:
            _err(f"{talk.nn}: started_at 이 없어 녹음 시작을 파일 mtime - 길이로 추정했다")
        missed = sum(a["paragraph_id"] is None for a in result)
        print(f"{talk.nn}: 사진 {len(result)}장 정렬 → {ev.rel(ev.align_path(talk))}"
              + (f" (녹음 범위 밖 {missed}장)" if missed else ""))
    return 0 if ok else 1


def _lines_off() -> str:
    """연구 라인이 0개인 까닭(출력용): 설정이 없는지, 파일은 있으나 유효 라인이 0개인지."""
    path = llm.research_lines_path()
    if path is None:
        return "설정 없음(절 생략)"
    return f"경고 — 파일은 있으나 라인 0개: {path} (`lines:` 항목마다 `name` 이 있어야 한다). 절 생략"


def cmd_scaffold(args) -> int:
    ev = load_event(args.event_dir)
    ev.select(None)
    for path, status in scaffold.run(ev, force=args.force):
        print(f"{status}: {ev.rel(path)}")
    if llm.research_lines_path() is not None and not llm.research_lines():
        _err(f"연구 라인: {_lines_off()}")
    for talk in ev.talks:
        if photos.count(ev, talk)[0] and align.load(ev, talk) is None:
            _err(f"{talk.nn}: 사진은 있으나 정렬 결과가 없다 — `lecture-recon align` 후 `scaffold --force` 로 표를 채울 수 있다")
    return 0


def print_lint(ev) -> int:
    result = lint.run(ev)
    for msg in result.errors:
        print(f"ERROR {msg}")
    for msg in result.warnings:
        print(f"WARN  {msg}")
    for msg in result.infos:
        print(f"INFO  {msg}")
    print(f"오류 {len(result.errors)} · 경고 {len(result.warnings)}")
    return 1 if result.errors else 0


def cmd_lint(args) -> int:
    return print_lint(load_event(args.event_dir))


def cmd_fill(args) -> int:
    ev = load_event(args.event_dir)
    ev.select(None)
    changed = scaffold.fill(ev)
    for path in changed:
        print(f"채움: {ev.rel(path)}")
    if not changed:
        print("채울 자리가 없다(이미 채웠거나 사람이 쓴 내용).")
    return print_lint(ev)


# LLM 단계 ------------------------------------------------------------------

def _client(args, ev):
    settings = llm.load_settings(args.backend, args.model, args.rpm, args.no_cache)
    return settings, llm.make_llm(settings, ev.work_dir)


def _plan_settings(args):
    return llm.load_settings(args.backend, args.model, args.rpm, args.no_cache)


def _plan_head(what: str, settings, n_calls: int, chunk: int | None = None, cached: int | None = None,
               calls: str | None = None) -> str:
    label = f"{settings.backend}/{settings.model}"
    size = f", 묶음 {chunk:,}자" if chunk else ""
    tail = f" (쓸 수 있는 캐시 {cached} · 새 호출 {n_calls - cached})" if cached is not None else ""
    return f"계획: {what} — {label}{size} → LLM 호출 {calls or f'{n_calls}회'}{tail}"


def _plan_foot(settings, n_retry: int, why: str = "형식 오류") -> list[str]:
    lines = []
    if n_retry:
        lines.append(f"  {why}면 요청마다 1회 더 부른다(최악 {n_retry}회 더).")
    if settings.backend == "manual":
        lines.append("  manual: 호출 1회 = 붙여넣기 1회.")
    if settings.backend == "claude-code":
        lines.append("  backend claude-code: 실제 실행에서는 CLI 가 LLM 을 부르지 않는다(스킬 흐름).")
    if settings.model in llm.MEASURED_LIMITS:
        lines.append(f"  참고: {settings.model} {llm.MEASURED_LIMITS[settings.model]}")
    return lines + ["LLM 을 부르지 않았다(--plan)."]


def _cached(ev, settings, rq) -> bool:
    """실행 때 다시 부르지 않을 캐시인가: 파일이 있고 응답이 형식 검사를 통과한다(빈 claims 같은 것은 아니다)."""
    if not settings.cache or settings.backend in ("manual", "claude-code"):
        return False
    key = llm.cache_key(settings.backend, settings.model, rq.messages, settings.base_url)
    path = ev.work_dir / "llm-cache" / f"{key}.json"
    try:
        response = json.loads(path.read_text(encoding="utf-8"))["response"]
        if rq.validate:
            rq.validate(response)
    except (OSError, KeyError, ValueError, llm.ResponseError):
        return False
    return True


def print_batch_plan(ev, settings, what: str, reqs: list, chunk: int) -> int:
    hits = [_cached(ev, settings, rq) for rq in reqs]
    print(_plan_head(what, settings, len(reqs), chunk, sum(hits)))
    for rq, hit in zip(reqs, hits):
        paras = rq.paragraphs
        size = sum(len(p.text) for p in paras)
        extra = f" · 후보 문장 {len(rq.candidates)}개" if rq.name.startswith("claims-") else ""
        print(f"  {rq.name}: {paras[0].id}–{paras[-1].id} 문단 {len(paras)}개 {size:,}자{extra}"
              + (" · 캐시 있음" if hit else ""))
    why = "형식 오류·빈 claims(후보 문장이 있을 때)" if what.startswith("claims") else "형식 오류"
    for line in _plan_foot(settings, len(reqs) - sum(hits), why):
        print(line)
    return 0


def _usage(client) -> str:
    if client.manual:
        return "manual: 응답 파일을 모두 읽었다"
    return f"{client.label}: API 호출 {client.calls}회, 캐시 {client.cache_hits}회"


def cmd_correct(args) -> int:
    ev = load_event(args.event_dir)
    talk = ev.select(args.nn)[0]
    if args.plan:
        settings = _plan_settings(args)
        chunk = args.chunk_chars or context.default_chunk_chars(settings.backend)
        return print_batch_plan(ev, settings, f"correct {talk.nn}", correct.correct_requests(ev, talk, chunk), chunk)
    _, client = _client(args, ev)
    res = correct.run(ev, talk, client, args.chunk_chars)
    n_guess = sum(a["level"] == "추정" for a in res["applied"])
    rejected = res["rejected"] + res["chapters_rejected"]
    print(f"{talk.nn}: {res['round']}차 교정 — 장 제목 {len(res['chapters'])}개, 치환 {len(res['applied'])}개 적용"
          f"(추정 {n_guess}개), 거부 {len(rejected)}개, 풀어 낸 [불명확] {res['unclear_resolved']}개"
          f" → {ev.rel(ev.transcript_path(talk))}")
    for r in rejected[:30]:
        print(f"  거부 {r.get('paragraph_id', '?')}: {r['why']}")
    if len(rejected) > 30:
        print(f"  … 나머지 {len(rejected) - 30}개는 work/correct/{talk.nn}.json")
    print(f"기록: work/correct/{talk.nn}.json · 원본 백업: {res['backup']} · {_usage(client)}")
    if not res["readme"]:
        _err(f"{ev.rel(ev.transcripts_dir / 'README.md')} 이 없어 검증 기록을 못 남겼다 — `lecture-recon scaffold` 후 다시 적는다")
    if res["quality_filled"]:
        print("메모: 전사 Quality note 에 기계 집계 한 문장(ASR·세그먼트·남은 [불명확])을 넣었다. 녹음 상태는 손으로 보충한다.")
    elif res["quality_placeholder"]:
        print("메모: 전사 상단 Quality note 는 손으로 채운다(녹음 상태는 LLM 이 판단하지 않는다).")
    return 0


def cmd_claims(args) -> int:
    ev = load_event(args.event_dir)
    talk = ev.select(args.nn)[0]
    if args.plan:
        settings = _plan_settings(args)
        chunk = args.chunk_chars or context.default_chunk_chars(settings.backend)
        return print_batch_plan(ev, settings, f"claims {talk.nn}", verify.claims_requests(ev, talk, chunk), chunk)
    _, client = _client(args, ev)
    data = verify.run_claims(ev, talk, client, args.chunk_chars, progress=lambda msg: print(msg, flush=True))
    kinds = {}
    for c in data["claims"]:
        kinds[c["kind"]] = kinds.get(c["kind"], 0) + 1
    print(f"{talk.nn}: claim {len(data['claims'])}개 ("
          + ", ".join(f"{k} {v}" for k, v in kinds.items()) + f") → {ev.rel(verify.claims_path(ev, talk))}")
    st = data["batch_stats"]
    n_cand = sum(b["candidates"] for b in st)
    if n_cand:
        print(f"  후보 문장 {n_cand}개: claim 으로 뽑힘 {sum(c['status'] == 'claim' for c in data['candidates'])} · "
              f"건너뜀 {sum(b['skipped'] for b in st)} · 버린 claim 의 후보 "
              f"{sum(c['status'] == 'dropped' for c in data['candidates'])} · 분류 안 됨 {sum(b['unclassified'] for b in st)}")
    for d in data["dropped"][:20]:
        print(f"  버림 {d}")
    print(_usage(client))
    return 0


def cmd_verify(args) -> int:
    ev = load_event(args.event_dir)
    talk = ev.select(args.nn)[0]
    if args.plan:
        settings = _plan_settings(args)
        claims = verify.load_claims(ev, talk)
        redo = verify.redo_claims(ev, talk, claims, args.all)
        n = math.ceil(len(redo) / verify.JUDGE_BATCH)
        print(_plan_head(f"verify {talk.nn}", settings, n, calls=f"최대 {n}회 (최악 {2 * n}회 + 형식 오류 재요청)"))
        print(f"  claim {len(claims)}개 중 다시 볼 것 {len(redo)}개 → 판정 최대 {n}회(claim {verify.JUDGE_BATCH}개씩) "
              f"+ 불충분 재판정 최대 {n}회. 근거를 못 찾은 claim 은 LLM 없이 정한다.")
        print(f"  검색: claim {len(redo)}개 × 질의 ≤3 × 소스 {len(settings.search)}개({', '.join(settings.search)}) — "
              "--plan 은 검색도 하지 않는다.")
        for line in _plan_foot(settings, 2 * n):
            print(line)
        return 0
    settings, client = _client(args, ev)
    searcher = sources.Sources(ev.work_dir, settings.search)
    for note in searcher.notes:
        _err(f"검색: {note}")
    data = verify.run_verify(ev, talk, client, searcher, redo_all=args.all,
                             progress=lambda msg: print(msg, flush=True))
    c = data["counts"]
    print(f"{talk.nn}: " + " · ".join(f"{k} {v}" for k, v in c.items()) + f" (claim {len(data['claims'])}개)")
    print(f"→ {ev.rel(verify.verify_json_path(ev, talk))}, {ev.rel(verify.verify_md_path(ev, talk))}"
          + (f" (기존 손작업 표는 {data['kept_manual']} 로 옮겼다)" if data.get("kept_manual") else ""))
    for name, why in data["disabled"].items():
        _err(f"검색 {name}: {why}")
    print(f"검색·취득 요청 {searcher.n_requests}회 · {_usage(client)}")
    return 0


def cmd_notes(args) -> int:
    ev = load_event(args.event_dir)
    talk = ev.select(args.nn)[0]
    if args.plan:
        settings = _plan_settings(args)
        _, pt = context.load_transcript(ev, talk)
        n = len(transcript.batches(pt.paragraphs, compose.NOTES_CHUNK_CHARS))
        final = "요약·연구 라인·확인" if llm.research_lines() else f"요약·확인 — 연구 라인: {_lines_off()}"
        print(_plan_head(f"notes {talk.nn}", settings, n + 1))
        print(f"  내용 절 {n}회(전사 {compose.NOTES_CHUNK_CHARS:,}자 묶음) + 마무리 1회({final}).")
        for line in _plan_foot(settings, n + 1):
            print(line)
        return 0
    _, client = _client(args, ev)
    res = compose.run_notes(ev, talk, client, force=args.force)
    print(f"{talk.nn}: 노트 내용 절 {res['sections']}개 → {ev.rel(res['path'])}"
          + (" · 원문 대조 " + ", ".join(f"{k} {v}" for k, v in res["verify"].items()) if res["verify"]
             else " · 원문 대조 없음(verify 전)"))
    if res["dropped_lines"]:
        print(f"  근거 ID 없는 줄 {res['dropped_lines']}개를 버렸다")
    if res["invalid_ids"]:
        print(f"  전사에 없는 문단 ID 를 뺐다: {', '.join(res['invalid_ids'][:12])}")
    if not res["research_lines"]:
        print(f"  연구 라인: {_lines_off()}")
    for d in res["research_dropped"]:
        print(f"  연구 라인: {d}")
    print(_usage(client))
    return print_lint(ev)


def cmd_report(args) -> int:
    ev = load_event(args.event_dir)
    ev.select(None)
    if args.plan:
        settings = _plan_settings(args)
        n = compose.report_calls(ev)
        print(_plan_head("report", settings, n))
        print("  노트 전체가 " + (f"{compose.REPORT_SPLIT_CHARS:,}자 이하라 한 번에" if n == 1
                              else f"{compose.REPORT_SPLIT_CHARS:,}자를 넘어 둘로 나눠(주제 / 아이디어·확인)") + " 부른다.")
        for line in _plan_foot(settings, n):
            print(line)
        return 0
    _, client = _client(args, ev)
    res = compose.run_report(ev, client, force=args.force)
    print(f"리포트: 관통 주제 {len(res['themes'])}개 ({', '.join(res['themes'])}) → {ev.rel(res['path'])}")
    if not res["ideas_section"]:
        print("  연구 라인: 노트에 연구 라인 절이 없다(`## 연구에 옮길 아이디어` 생략)")
    if res["ideas_dropped"]:
        print(f"  노트에 없는 연구 라인이라 뺐다: {', '.join(res['ideas_dropped'])}")
    if res["invalid_ids"]:
        print(f"  전사에 없는 문단 ID 를 뺐다: {', '.join(res['invalid_ids'][:12])}")
    print(_usage(client))
    return print_lint(ev)


def cmd_slides(args) -> int:
    ev = load_event(args.event_dir)
    talk = ev.select(args.nn)[0]
    _, client = _client(args, ev)
    if not client.manual:
        print(f"{client.label}: API 백엔드의 사진 판독은 구현하지 않았다. 스킬 흐름(Claude 가 사진을 직접 읽음)이나 "
              "--backend manual(무료 챗에 사진 첨부)을 쓴다.")
        return 0
    res = compose.run_slides(ev, talk, client)
    print(f"{talk.nn}: 슬라이드 표 {res['filled']}/{res['rows']}행 채움 → {ev.rel(res['path'])}")
    return print_lint(ev)


def cmd_sources(args) -> int:
    ev = load_event(args.event_dir)
    talk = ev.select(args.talk)[0]
    if args.action == "add":
        if not args.target:
            raise LectureReconError("추가할 URL 이나 파일 경로를 준다: sources <event_dir> --talk NN add <url|path>")
        e = sources.add_user_source(ev.work_dir, talk.nn, args.target, args.note or "")
        print(f"{talk.nn}: 사용자 자료 {e['id']} {e['status']}" + (f" ({e['reason']})" if e["reason"] else "")
              + f" → work/sources/user/{talk.nn}/manifest.json")
        if e["status"] != "ok":
            _err("이 자료는 근거로 쓰지 않는다" + (" — verify 때 다시 받아 본다" if e["status"] == "fetch_failed" else ""))
        return 0 if e["status"] == "ok" else 1
    items = sources.load_manifest(ev.work_dir, talk.nn)
    if not items:
        print(f"{talk.nn}: 사용자 자료 없음")
    for e in items:
        print(f"{e['id']} [{e['status']}] {e['target']}" + (f" — {e['note']}" if e.get("note") else "")
              + (f" ({e['reason']})" if e.get("reason") else ""))
    return 0


def cmd_status(args) -> int:
    ev = load_event(args.event_dir)
    print(f"{ev.name} ({ev.date}) — {ev.root}")
    warnings = []
    if ev.talks:
        print("| NN | 발표 | audio | asr | transcript | corrected | photos | align | note |")
        print("|---|---|---|---|---|---|---|---|---|")
    for t in ev.talks:
        n = len(t.recordings)
        audio_cell = f"{sum(ev.audio_path(t, k).exists() for k in range(n))}/{n}"
        asr_cell = f"{sum(ev.asr_path(t, k).exists() for k in range(n))}/{n}"
        tpath = ev.transcript_path(t)
        transcript_cell = corrected = "-"
        if tpath.exists():
            pt = transcript.parse(tpath.read_text(encoding="utf-8"))
            transcript_cell = f"O ({len(pt.paragraphs)}문단)"
            corrected = f"O ({len(pt.chapters)}장)" if pt.chapters else "-"
        n_photos, ingested = photos.count(ev, t)
        photos_cell = "-" if not n_photos else (str(n_photos) if ingested else f"{n_photos} (ingest 전)")
        align_cell = ("O" if ev.align_path(t).exists() else "-") if n_photos else "n/a"
        note = ev.note_path(t)
        note_cell = "O" if note.exists() and note.stat().st_size > 0 else "-"
        print(f"| {t.nn} | {t.speaker_org} | {audio_cell} | {asr_cell} | {transcript_cell} | {corrected} | "
              f"{photos_cell} | {align_cell} | {note_cell} |")
        for rec in t.recordings:
            if not (ev.root / rec.file).exists():
                warnings.append(f"{t.nn}: 녹음 원본 없음: {rec.file}")
        if not t.recordings[0].started_at:
            warnings.append(f"{t.nn}: started_at 없음 — 사진 정렬 시 녹음 시작을 파일 mtime - 길이로 추정한다")
    if not ev.talks:
        print("talks 없음 — event.yaml 에 발표를 적는다.")
    report = ev.report_path
    print(f"report: {'O' if report.exists() and report.stat().st_size > 0 else '-'} ({ev.rel(report)})")
    for w in warnings:
        print(f"경고: {w}")
    return 0


def _talk_number(value: str) -> int:
    try:
        return int(value)
    except ValueError:
        raise argparse.ArgumentTypeError(f"발표 번호(NN)여야 한다: {value!r}") from None


def _date(value: str) -> str:
    try:
        return dt.date.fromisoformat(value).isoformat()
    except ValueError:
        raise argparse.ArgumentTypeError(f"YYYY-MM-DD 여야 한다: {value!r}") from None


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="lecture-recon", description="강연 녹음·슬라이드 사진 → 3층 재구성 노트의 기계적 단계")
    sub = parser.add_subparsers(dest="command", required=True, metavar="<command>")

    def add(name: str, func, help: str, talk: bool = False, force: str | None = None):
        p = sub.add_parser(name, help=help, description=help)
        if name != "init":
            p.add_argument("event_dir", help="행사 디렉터리 (event.yaml 이 있는 곳)")
        if talk:
            p.add_argument("--talk", type=_talk_number, metavar="NN", help="이 발표만 처리")
        if force:
            p.add_argument("--force", action="store_true", help=force)
        p.set_defaults(func=func)
        return p

    p = add("init", cmd_init, "행사 디렉터리와 event.yaml 골격을 만든다")
    p.add_argument("slug", help="행사 slug (예: ai-tech-day-2026)")
    p.add_argument("--root", default="~/research/lectures", help="행사 디렉터리를 둘 곳 (기본 ~/research/lectures)")
    p.add_argument("--name", help="행사명 (기본: slug)")
    p.add_argument("--date", type=_date, help="행사 날짜 YYYY-MM-DD")

    add("ingest", cmd_ingest, "오디오 → work/audio/NN[_k].wav (16k mono), 사진 → manifest + 판독용 축소본")

    p = add("asr", cmd_asr, "faster-whisper 전사 → work/asr/NN[_k].json (GPU 는 사용자 승인 후)", talk=True)
    p.add_argument("--model", default="large-v3", help="Whisper 모델 (기본 large-v3)")
    p.add_argument("--device", choices=["cpu", "cuda"], default="cpu", help="기본 cpu. cuda 는 사용자 승인 후")
    p.add_argument("--compute-type", default="auto", help="CTranslate2 compute type (기본 auto)")
    p.add_argument("--language", help="ASR 언어 (기본: event.yaml 의 language, mixed/auto = 자동 감지)")

    add("segment", cmd_segment, "asr json → 문단(NN-MMM) 전사 md 초안", talk=True,
        force="이미 있는 전사(교정본일 수 있음)를 덮어쓴다")
    add("align", cmd_align, "사진 촬영 시각 ↔ 전사 문단 시각 → work/align/NN.json", talk=True)
    add("scaffold", cmd_scaffold, "노트 README·발표별 노트·리포트·전사 README 골격", force="비어 있지 않은 기존 파일도 덮어쓴다")
    add("lint", cmd_lint, "docs/format.md lint 규칙 검사 + unresolved-segments.md 재생성 (오류 시 exit 1)")
    add("fill", cmd_fill, "LLM 없이 사실에서 채울 수 있는 자리(전사·노트 README, 전사 Quality note)를 채우고 lint")
    add("status", cmd_status, "발표별 진행 상황")

    def llm_opts(p):
        p.add_argument("--backend", choices=llm.BACKENDS,
                       help="LLM 백엔드 (기본: 설정 파일 → 없으면 claude-code = CLI 는 LLM 을 부르지 않음)")
        p.add_argument("--model", help="모델 이름 (기본: preset)")
        p.add_argument("--rpm", type=float, help="분당 요청 상한 (무료 한도 보호)")
        p.add_argument("--no-cache", action="store_true", help="work/llm-cache/ 를 쓰지 않는다")
        return p

    def talk_cmd(name, func, help):
        p = add(name, func, help)
        p.add_argument("nn", type=_talk_number, metavar="NN", help="발표 번호")
        return llm_opts(p)

    def plan_opt(p):
        p.add_argument("--plan", action="store_true",
                       help="LLM 을 부르지 않고 호출 횟수(묶음 수)만 출력한다(API 키 없어도 된다)")
        return p

    chunk_help = "문단 묶음 크기(자). 기본 API 3000, gemini·manual 12000"
    p = plan_opt(talk_cmd("correct", cmd_correct, "전사 교정: LLM JSON 패치(장 제목·치환)를 검사해 적용, README 검증 기록 갱신"))
    p.add_argument("--chunk-chars", type=int, help=chunk_help)
    p = plan_opt(talk_cmd("claims", cmd_claims, "대조할 주장 뽑기 → work/claims/NN.yaml"))
    p.add_argument("--chunk-chars", type=int, help=chunk_help)
    p = plan_opt(talk_cmd("verify", cmd_verify, "원문 대조: 키 없는 검색 + 판정 → work/verify/NN.json, work/verify-NN.md"))
    p.add_argument("--all", action="store_true", help="확정 판정(확인·다름·못찾음)도 다시 한다")
    p = plan_opt(talk_cmd("notes", cmd_notes, "발표별 노트 작성(원문 대조 결과 절은 verify 결과로) + lint"))
    p.add_argument("--force", action="store_true", help="손으로 고친 노트도 덮어쓴다(원본은 work/backup/)")
    talk_cmd("slides", cmd_slides, "슬라이드 표 채우기 (manual: 사진 첨부용 프롬프트, API: 미구현)")
    p = plan_opt(llm_opts(add("report", cmd_report, "노트 전체로 종합 리포트 작성 + lint")))
    p.add_argument("--force", action="store_true", help="손으로 고친 리포트도 덮어쓴다(원본은 work/backup/)")

    p = add("sources", cmd_sources, "원문 대조용 사용자 자료(URL·PDF·HTML·텍스트) 추가·목록 → work/sources/user/NN/")
    p.add_argument("--talk", type=_talk_number, metavar="NN", required=True, help="발표 번호")
    p.add_argument("action", choices=["add", "list"])
    p.add_argument("target", nargs="?", help="add: URL 또는 파일 경로")
    p.add_argument("--note", help="자료 메모(예: 발표자 공식 슬라이드)")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except llm.ManualPending as e:
        _err(e.render())
        return 3
    except llm.QuotaExhausted as e:
        _err(str(e))
        return e.exit_code
    except LectureReconError as e:
        _err(f"오류: {e}")
        return 2


if __name__ == "__main__":
    sys.exit(main())
