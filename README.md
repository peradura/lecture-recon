# lecture-recon

강연 **녹음 + 슬라이드 사진(있으면)** 을 DAIS 위키식 3층 문서(전사 / 발표별 노트 / 종합 리포트)로 재구성하는 도구.
이 패키지는 기계적 단계(오디오 정규화·전사·문단 ID·사진 정렬·골격 생성·링크 검사)를 맡고,
판단이 필요한 단계(전사 교정·슬라이드 판독·노트·원문 대조·리포트)는 Claude Code 스킬 `lecture-recon` 이 맡는다.
스킬 없이 무료 LLM(Gemini 무료 키·Ollama·무료 챗에 붙여넣기)으로 같은 단계를 CLI 로 돌릴 수도 있다([아래](#판단-단계를-cli-로-돌리기-서버claude-토큰-없이)).
목적·결정 기록은 [PROJECT.md](PROJECT.md), 출력 형식은 [docs/format.md](docs/format.md).

## 두 경로: 스킬(Claude Code) vs CLI(무료 LLM)

뼈대는 같다. `init → ingest → asr → segment → align → scaffold → lint` 는 어느 경로든 CLI 가 한다.
갈리는 것은 판단 단계(전사 교정·주장 뽑기·원문 대조·노트·리포트)다.

| 경로 | 판단 단계 | LLM 설정 |
|---|---|---|
| 스킬(Claude Code) | Claude 가 전사·사진·노트 파일을 직접 읽고 쓴다(스킬 `lecture-recon`) | backend 기본값 `claude-code`. 이때 CLI 는 LLM 을 부르지 않는다 |
| CLI(무료 LLM) | `correct`·`claims`·`verify`·`notes`·`report` 명령이 LLM 을 부른다 | `--backend gemini\|manual\|ollama\|openai` (그 밖에 groq·openrouter) |

backend 는 설정 파일(`~/.config/lecture-recon/config.yaml` 의 `backend:`) → 환경변수 `LECTURE_RECON_BACKEND` → `--backend` 순으로 정한다(뒤가 앞을 덮는다).
두 경로는 섞어 쓸 수 있다. CLI 가 남긴 `work/verify/NN.json`·`work/correct/NN.json` 을 스킬 흐름이 근거로 쓴다.

## 설치

공유 서버(sudo 없음) 기준, 사용자 공간 venv 만 쓴다. 외부 도구: `ffmpeg`, `ffprobe`.

```bash
cd ~/research/lecture-recon
uv venv .venv
uv pip install -e ".[dev]"          # pyyaml, pillow, pytest
uv pip install -e ".[asr]"          # faster-whisper (asr 단계에서만 필요)
uv pip install -e ".[search]"       # ddgs, trafilatura, pymupdf (CLI 원문 대조에서만 필요)
.venv/bin/lecture-recon --help      # 또는 .venv/bin/python -m lecture_recon --help
```

## 대표 실행 순서

```bash
L=~/research/lecture-recon/.venv/bin/lecture-recon
$L init ai-tech-day-2026 --name "2026 AI TECH DAY" --date 2026-10-20   # ~/research/lectures/ai-tech-day-2026/
# event.yaml 의 talks 를 채우고, 녹음은 raw/audio/, 사진은 raw/photos/NN/ 에 둔다
#   (선택) talks[].aliases: [영문 이름, 연구실·회사 별칭] — verify 가 원문이 발표자 측 자료인지 가릴 때 쓴다
E=~/research/lectures/ai-tech-day-2026
$L ingest   $E      # 오디오 → work/audio/NN.wav (16k mono), 사진 → work/photos/NN.json + 축소본
$L asr      $E      # faster-whisper → work/asr/NN.json (기본 --device cpu --model large-v3)
$L segment  $E      # → <slug>-transcripts/NN_<org>_<speaker>.md 초안 (기존 전사는 --force 없이는 안 덮음)
#   (Claude: 전사 교정 — 장 제목 ## [HH:MM:SS], 고유명사 교정)
$L align    $E      # 사진 시각 ↔ 문단 시각 → work/align/NN.json
$L scaffold $E      # 노트·리포트·README 골격 (기존 파일 보존, --force 로 덮어씀)
#   (Claude: 슬라이드 판독, 노트, 원문 대조, 종합 리포트)
$L lint     $E      # format.md 규칙 검사 + unresolved-segments.md 재생성. 오류면 exit 1
$L status   $E      # 발표별 진행 상황
```

`asr`·`segment`·`align` 은 `--talk NN` 으로 발표 하나만 처리할 수 있다.

## 판단 단계를 CLI 로 돌리기 (서버·Claude 토큰 없이)

교정·원문 대조·노트·리포트는 기본적으로 Claude Code 스킬이 한다. 스킬 없이 노트북 + 무료 자원만으로 같은 산출물을 내려면
LLM 백엔드를 골라 CLI 명령으로 돌린다. 설계·결정은 [docs/design-backends.md](docs/design-backends.md).

```bash
uv pip install -e ".[search]"       # ddgs, trafilatura, pymupdf (원문 검색·본문 추출; 없어도 arXiv·S2 는 동작)
B="--backend gemini"                # 또는 --backend manual / --backend ollama
$L correct  $E 4 $B     # 전사 교정: JSON 패치 → 검사 후 적용, work/04_draftN.md 백업, README 검증 기록
$L claims   $E 4 $B     # 대조할 주장 → work/claims/04.yaml
$L sources  $E --talk 4 add ~/Downloads/official-slides.pdf --note "발표자 공식 슬라이드"   # 선택: 먼저 볼 자료
$L verify   $E 4 $B     # 키 없는 검색(arXiv·Semantic Scholar·DuckDuckGo) + 판정 → work/verify/04.json, work/verify-04.md
$L slides   $E 4 --backend manual   # 사진 판독(무료 챗에 사진 첨부). API 백엔드는 미구현
$L notes    $E 4 $B     # 노트(원문 대조 결과 절은 verify 결과 그대로) + lint
$L report   $E $B       # 종합 리포트 + lint
$L fill     $E          # LLM 없이: README 처리 과정·검증 기록·자료 규모·작업 기록, 전사 Quality note 채우고 lint
```

공통 옵션: `--backend`, `--model`, `--rpm`(분당 상한), `--no-cache`. `correct`·`claims`·`verify`·`notes`·`report` 는 `--plan`
(LLM·검색을 부르지 않고 호출 횟수만 출력, API 키 없어도 된다). `correct`·`claims` 는 `--chunk-chars`(기본 API 3,000자, gemini·manual 12,000자),
`verify` 는 `--all`(확정 판정도 다시), `notes`·`report` 는 `--force`(손으로 고친 파일 덮어쓰기, 원본은 `work/backup/`).
LLM 응답은 `work/llm-cache/` 에 캐시돼 같은 입력은 다시 부르지 않는다.
종료 코드: 0 성공, 1 lint 오류, 2 오류(설정·입력), 3 manual 응답 대기, 4 무료 한도 소진 — 일일 한도(Gemini `quotaId` PerDay, 또는 본문의 per day/per-day 에 10분 안의 재시도 안내가 없을 때)는 바로, 분 단위로 보인 429 도 재시도 5회 뒤에는 멈춘다. 다음 초기화 시각은 backend 별(Gemini 태평양 자정, OpenRouter UTC 자정, 그 밖은 서버 안내)로 KST 로 알려 준다. 그 뒤 같은 명령을 다시 실행하면 캐시된 묶음은 건너뛴다.

- `claims` 는 묶음마다 숫자·%·단위·영문·연도가 든 전사 문장을 CLI 가 먼저 골라 번호 붙인 "후보 문장"으로 주고, 모델이 후보마다 claim 또는 건너뜀(사유)을 정하게 한다.
  후보가 있는데 claims 도 skipped 도 빈 응답이면 한 번 다시 묻고(캐시하지 않음), 그래도 비면 `dropped` 에 "모델이 빈 목록을 반환"으로 남긴다. 후보를 모두 사유와 함께 건너뛴 응답은 그대로 받는다. 전사 인용이 `[추정: …]` 교정 구간과 겹치면 `from_correction`, 전부 `[불명확]` 안이면 `low_confidence` 를 달고 verify 표 비고에 적는다. 묶음마다 `후보 / claim / 버림 / 건너뜀` 수를 출력하고 `work/claims/NN.yaml` 의 `batch_stats`·`candidates` 에 남긴다.
- `correct` 의 evidence: reason 이 원문·논문·보고서를 이유로 들면 근거와 상관없이 거부한다(원칙 2). "공식 자료"는 event.yaml `sources` 의 URL(원문 대조용 `work/sources/user/` 는 아니다)이고 고친 표기가 그 URL 본문(`work/sources/` 캐시, 없으면 받아 봄)에 그대로 있을 때만 근거다 — 숫자·부정어·단위·`[불명확` 변경은 이 길만 된다(본문을 못 가져오거나 없으면 "공식 자료 본문에서 확인하지 못했다"로 거부). URL 없이 event.yaml 의 행사명·발표 제목·발표자·소속을 인용하는 것은 표기 교정에만 쓰고, 고친 부분이 그 문자열의 단어(3자 이상)와 같아야 한다. 그 밖의 "공식 자료"는 근거 없음(보호 항목 변경은 거부, 나머지는 `[추정: …]`). "다른 녹음"은 녹음이 둘 이상인 발표에서만 근거다.
- `notes`·`report`·`correct`·`fill` 은 사실에서 채울 수 있는 자리(전사 README 전처리·ASR·발표별 교정 차수·검증 기록, 노트 README 교정 차수·행사 정보의 빈 칸·발표 목록의 사진 칸·자료 규모·작업 기록, 전사 Quality note 의 기계 집계 한 문장)를 채운다. 줄 전체가 CLI 가 쓰는 모양 그대로일 때만 다시 쓰므로, 사람이 덧붙이거나 고친 줄은 그대로 둔다. 녹음 상태(저음량·잡음)는 사람이 Quality note 에 보충한다.

### 무료로 쓰는 세 가지 길

1. **노트북 + Gemini 무료 키** (권장): 전사는 노트북에서 `asr --model small`(또는 `medium`, `turbo`), 판단 단계는 Gemini 무료 티어.
   [Google AI Studio](https://aistudio.google.com/) 에서 키를 받아 `export GEMINI_API_KEY=…` 하고, 설정 파일을 둔다.
   ```yaml
   # ~/.config/lecture-recon/config.yaml
   backend: gemini          # 기본 모델 gemini-3.5-flash
   rpm: 8                   # 분당 한도 보호. 분 단위 429 는 자동 백오프, 일일 한도 429 는 바로 멈춘다(exit 4)
   ```
   실측(2026-11-06, 새 무료 키, 자세히는 [design-backends.md](docs/design-backends.md#실측-메모-2026-11-06-새-무료-키)):
   - `gemini-3.5-flash`: **하루 20회**(429 `generate_content_free_tier_requests, limit: 20`). 태평양 시각 자정(KST 17:00, 일광 절약 시간에는 16:00)에 초기화.
     발표 1건(37분, 문단 54개)은 12,000자 묶음으로 correct 2 + claims 2 + verify 약 6(claim 30개, 재판정 별도) + notes 2 + report 1 ≈ 13회다. 시작 전에 `--plan` 으로 센다.
   - `gemini-3.5-flash-lite`: flash 와 한도가 따로이고 빠르다. 그러나 **claims 에는 쓰지 않는다** — 예전 프롬프트로는 모든 묶음에서 빈 목록, 후보 문장 목록을 준 뒤에도 후보 34개 중 7개만 claim 으로 뽑았다(flash 의 판단보다 보수적).
   - `gemini-3.8-flash` 는 OpenAI 호환 엔드포인트에서 응답이 없고, `gemini-2.5-flash` 는 새 사용자에게 404 다.
   - 권장 조합: claims·verify 는 `gemini-3.5-flash`, notes·report 초안은 `--model gemini-3.5-flash-lite` 도 쓸 만하다(lint 가 근거 ID·링크를 검사한다).
2. **Ollama (로컬, 키 없음)**: `ollama pull qwen3:8b`, `OLLAMA_CONTEXT_LENGTH=16384 ollama serve`, 그리고 `--backend ollama`. 노트북 RAM 16 GB 이상 권장. 작은 모델은 JSON 형식을 자주 틀려 재요청이 늘 수 있다.
3. **manual (키·서버·토큰 전부 없음)**: `--backend manual`. CLI 가 `work/prompts/<이름>.md` 를 쓰고 exit 3 으로 멈춘다.
   그 파일 전체를 무료 챗(Gemini 앱, ChatGPT 무료, Claude 무료)에 붙여넣고, 응답 전체를 `work/responses/<같은 이름>.md` 에 저장한 뒤 같은 명령을 다시 실행한다.
   응답 첫 줄은 `request_id: …` 여야 한다(프롬프트가 바뀌면 다시 붙여넣으라고 알려 준다). 발표 1건에 붙여넣기 약 13~19회.

Groq(`GROQ_API_KEY`), OpenRouter(`OPENROUTER_API_KEY`, 기본 `openrouter/free`), OpenAI(`OPENAI_API_KEY`, 유료)도 preset 이 있다.
환경변수 `LECTURE_RECON_BACKEND`·`LECTURE_RECON_MODEL`·`LECTURE_RECON_BASE_URL`·`LECTURE_RECON_API_KEY` 가 설정 파일보다 우선하고, CLI 옵션이 가장 우선한다.
설정이 없으면 backend 는 `claude-code` 이고 CLI 는 LLM 을 부르지 않는다(스킬 흐름).

### 원문 대조가 하는 일

- 키 없는 검색을 먼저 쓴다. `TAVILY_API_KEY`·`BRAVE_API_KEY`·`SEMANTIC_SCHOLAR_API_KEY` 가 있으면 함께 쓴다. 공유 서버에서 키 없는 Semantic Scholar 는 429 가 잦아 실행 중 자동으로 빠진다.
- 인용문은 LLM 이 아니라 CLI 가 캐시된 원문에서 다시 찾는다. 못 찾으면 "확인"이 아니라 "확인하지 않은 것(인용문 불일치)"이다.
- 검색·취득·추출 실패나 네트워크 없음은 "검색했으나 찾지 못한 것"이 아니라 "확인하지 않은 것"에 사유와 함께 간다. 다시 돌리면 그것들만 다시 한다.
녹음이 여럿인 발표는 첫 녹음이 주 녹음(전사·정렬 기준)이고, 나머지는 `work/asr/NN_2.json` … 만 만든다.

## 출력 위치

```
~/research/lectures/<slug>/
  event.yaml  .gitignore            # init (gitignore: raw/ work/ *-notes/slides/)
  raw/{audio,photos/NN}/            # 원자료 (사람이 넣음)
  work/{audio,asr,photos,align}/    # 중간 산출물
  work/{prompts,responses,llm-cache,claims,verify,correct,sources,notes,backup}/   # CLI 판단 단계(선택)
  <slug>-transcripts/               # NN_<org>_<speaker>.md, README.md, unresolved-segments.md
  <slug>-notes/                     # README.md, NN_<org>_<speaker>.md, slides/NN/*.jpg (긴 변 ≤1600px)
  <slug>-report.md
```

공용 위키(`~/research/wiki`)에는 쓰지 않는다. 위키 반영은 사용자 확인 후 별도로 한다.

## 형식 바꾸기

기본은 이 레포의 형식이다([docs/format.md](docs/format.md)). 다른 형식을 쓰려면 `~/.config/lecture-recon/config.yaml` 에 아래 키를 둔다(모두 선택, `~` 확장, 상대 경로는 config.yaml 이 있는 디렉터리 기준).

```yaml
templates_dir: ~/my-format/templates   # note.md, report.md, notes_readme.md, transcripts_readme.md, event.yaml, event.gitignore
prompts_dir: ~/my-format/prompts       # correct.md, claims.md, verify.md, notes.md, report.md, slides.md
required_sections: [요약, 원문 대조 결과, 확인이 필요한 것]   # lint 규칙 5 의 노트 필수 절
```

- 디렉터리에 같은 이름의 파일이 있으면 그것을, 없으면 패키지 것(`src/lecture_recon/templates/`, `prompts/`)을 쓴다. 환경변수 `LECTURE_RECON_TEMPLATES_DIR`·`LECTURE_RECON_PROMPTS_DIR`(상대 경로는 현재 디렉터리 기준)가 설정 파일보다 우선한다.
- 템플릿은 `scaffold` 골격, `init`(`event.yaml`·`event.gitignore`), 스킬 흐름이 쓴다. CLI `notes`·`report` 는 절(머리말·원문 대조 결과·연구 라인 등)을 코드에서 조립하므로 그 고정 절과 사진 있는 발표의 `## 슬라이드` 표는 템플릿으로 바꿀 수 없다.
- 템플릿을 바꾸면 프롬프트(`notes.md`·`report.md` 의 형식 명세)와 `required_sections` 도 같이 맞춘다. `required_sections` 에 CLI `notes` 가 만들지 않는 절을 넣으면 `notes` 뒤 lint 가 오류(exit 1)를 낸다.
- 모든 템플릿에서 `<!-- if research_lines -->` … `<!-- endif -->` 블록은 연구 라인 설정이 있을 때만 남는다. 두 마커는 각각 한 줄에 단독으로 쓴다(앞뒤 공백 허용, 줄째 지움). 중첩·짝 없는 마커·모르는 키는 오류다.
- `scaffold` 는 노트·리포트 골격 첫 줄에 `<!-- lecture-recon:skeleton -->` 를 넣는다. 이 줄이 있고 내용이 scaffold 가 남긴 해시(`work/scaffold/`)와 같으면 CLI `notes`·`report` 가 골격으로 보고 `--force` 없이 덮어쓰며, 쓸 때 지운다. 손으로 한 줄이라도 고쳤으면 골격이 아니다. 노트를 다 썼으면 이 줄은 지운다.

### 연구 라인

노트의 `## 연구 라인에 걸리는 것`, 리포트의 `## 연구에 옮길 아이디어` 는 연구 라인 설정이 있을 때만 생긴다.
[configs/research_lines.example.yaml](configs/research_lines.example.yaml) 을 `~/.config/lecture-recon/research_lines.yaml` 로 복사해 고치거나, config.yaml 에 `research_lines: <경로>` 를 적는다.
둘 다 없으면 골격·노트·리포트에 두 절을 두지 않는다. `research_lines: false` 면 기본 파일이 있어도 끈다(빈 문자열은 기본 파일을 찾는다).
연구 라인이 있으면 lint 는 노트에 `## 연구 라인에 걸리는 것` 을 요구한다(`required_sections:` 를 적었으면 그것 그대로).
설정을 추가·제거했으면 기존 골격은 `scaffold --force` 로 다시 만든다(노트를 쓰기 전에만 — 전부 덮어쓴다).

## GPU 주의

`asr --device cuda` 는 공유 서버 GPU 를 쓴다. **실행 전에 반드시 사용자 승인을 받는다.**
기본값은 `--device cpu` 이며, CPU 의 large-v3 는 느리므로 긴 녹음은 백그라운드로 돌린다.

## 테스트

```bash
.venv/bin/python -m pytest -q
```

합성 행사(`tests/synth.py`: 발표 2건, 한 건은 EXIF 사진 3장)로 init → segment → align → scaffold → lint 를 검사한다.
판단 단계(correct/claims/verify/notes/report/slides)는 가짜 LLM(`tests/fakes.py` 의 FakeBackend)과 가짜 검색으로 검사한다.
네트워크·faster-whisper·API 키는 필요 없고, ffmpeg 가 없으면 오디오 테스트만 건너뛴다.
