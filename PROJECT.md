# lecture-recon — 강연 녹음·슬라이드 사진 → 3층 재구성 노트

## 목적

출장·학회에서 들은 강연을 **녹음 파일 + 슬라이드 사진(있으면)** 만으로, DAIS 위키의
`conference/` 관례(KDD 2026 전사·정리, AI TECH DAY 2026 리포트)와 같은 **3층 문서**로 재구성한다.
기계적 단계(전사·문단 ID·사진 정렬·골격 생성·링크 검사)는 Python 패키지가 하고,
판단이 필요한 단계(전사 교정·슬라이드 판독·노트 작성·원문 대조·종합 리포트·연구 연결)는
Claude Code 스킬 `lecture-recon`이 사용자 메모리(연구 주제)를 바탕으로 수행한다.
서버·Claude 토큰이 없을 때는 같은 판단 단계를 CLI 가 무료 LLM 백엔드(Gemini 무료 키·Ollama·무료 챗 붙여넣기)로 돌린다([docs/design-backends.md](docs/design-backends.md)).

## 결정 기록

| 날짜 | 결정 | 근거 |
|---|---|---|
| 2026-10-29 | 3층 구조(전사 / 발표별 노트 / 종합 리포트) 전부 생성 | 사용자 결정. 모든 문장이 전사 문단 ID로 추적되는 것이 형식의 핵심 |
| 2026-10-29 | 원문 대조(논문·기사 웹 검색)를 기본 단계로 포함, 불가 시 "확인 필요"로 남김 | 사용자 결정 ("가능하면") |
| 2026-10-29 | "연구에 옮길 아이디어"는 **사용자의 연구 라인** 기준(당시 `configs/research_lines.yaml`) | 사용자 결정 |
| 2026-10-29 | 사진 없는 발표도 같은 틀로 진행. 슬라이드 절·대조만 비우고 "녹음만 원자료"로 명시 | 사용자 결정 |
| 2026-10-29 | 슬라이드 OCR은 별도 엔진 없이 Claude가 사진을 직접 읽음(Read 도구) | tesseract 미설치·sudo 불가. VLM 판독이 교정 OCR보다 정확 |
| 2026-10-29 | ASR은 faster-whisper(optional extra). GPU 사용은 실행 전 사용자 승인 필수 | 공유 서버 규칙: GPU 는 승인 후 |
| 2026-10-29 | 출력은 `~/research/lectures/<event>/`(개인). 공용 위키 반영은 사용자 확인 후 별도 | 개인 노트는 개인 디렉터리에 두고, 공용 위키는 확인 후 반영한다 |
| 2026-11-02 | 판단 단계(교정·주장·원문 대조·노트·리포트·슬라이드)를 CLI 로도 돌린다. LLM 은 OpenAI 호환 HTTP 하나(gemini/groq/openrouter/ollama/openai) + `manual`(무료 챗 붙여넣기). 설정이 없으면 `claude-code`(CLI 는 LLM 을 안 부름). 원문 대조는 키 없는 arXiv·Semantic Scholar·DuckDuckGo 가 1차. 인용문·문단 ID·타임스탬프는 CLI 가 코드로 검증하고, 실패는 "확인하지 않은 것"(사유 포함)으로 둔다 | 서버·Claude 토큰 없이 노트북+무료 자원으로 같은 3층 산출물. 설계 검토(Codex) 반영. [docs/design-backends.md](docs/design-backends.md) |
| 2026-11-12 | 공개 전환 준비: 연구 라인 설정을 레포에서 빼고(예시 `configs/research_lines.example.yaml`), 설정이 없으면 노트·리포트·골격에 연구 라인 절을 두지 않는다. 형식은 docs/format.md 가 기본이고, 사용자 형식(config `templates_dir`·`prompts_dir`·`required_sections`)이 있으면 그것을 쓴다 | 사용자 결정 ("사용자가 원하는 형식이 있으면 그걸 우선, 없으면 우리 형식을 기본으로. 연구 라인은 설정하지 않으면 공백") |

## 출력 형식 (docs/format.md 에 상세)

```
~/research/lectures/<slug>/
  event.yaml                      # 행사·발표·녹음·사진 메타 (사람이 작성, init이 골격 생성)
  raw/{audio,photos}/             # 원자료 (git 제외)
  work/                           # asr json, 사진 manifest, 정렬 json (git 제외)
  <slug>-transcripts/
    README.md                     # 목록 + 처리 과정 + 검증 기록
    NN_<speaker>.md               # 문단 ID NN-MMM, 타임스탬프, [불명확] 표시
    unresolved-segments.md        # 남은 불명확 표시 목록
  <slug>-notes/
    README.md                     # 행사 정보, 발표 목록, 자료 규모
    NN_<org>_<speaker>.md         # 내용 절 → 슬라이드(있으면) → 원문 대조 결과 → 연구 라인 연결(설정 시) → 확인 필요
    slides/NN/                    # 판독용 축소 사진 (git 제외 가능)
  <slug>-report.md                # 한눈에 보기 → 자료 범위와 신뢰도 → 관통 주제 → 연구에 옮길 아이디어(노트에 있을 때) → 확인이 필요한 것
```

## 파이프라인

| # | 단계 | 담당 | 명령/행동 |
|---|---|---|---|
| 0 | 행사 초기화 | CLI | `lecture-recon init <slug>` → `event.yaml` 골격 |
| 1 | 원자료 수집 | CLI | `ingest` : 오디오 정규화(ffmpeg, 16k mono wav), 사진 EXIF 시각·manifest |
| 2 | 전사 | CLI | `asr` : faster-whisper → `work/asr/NN.json` (device는 config; GPU면 사용자 승인) |
| 3 | 문단화 | CLI | `segment` : 세그먼트 → 문단(ID NN-MMM) → `*-transcripts/NN_*.md` 초안 |
| 4 | 전사 교정 | Claude 또는 CLI | 고유명사·용어만 근거 있을 때 교정, 나머지 `[불명확]` 유지, README에 교정 차수 기록. CLI: `correct NN` (JSON 패치 검사·적용, `work/NN_draftN.md` 백업) |
| 5 | 사진 정렬 | CLI | `align` : 사진 촬영 시각 ↔ 문단 시각 매칭 → `work/align/NN.json`, notes/slides/NN/ 축소본 |
| 6 | 슬라이드 판독 | Claude 또는 CLI(manual) | 사진 Read → 슬라이드 번호·제목·본문 텍스트 → 노트의 슬라이드 절. CLI: `slides NN --backend manual` (무료 챗에 사진 첨부) |
| 7 | 발표별 노트 | Claude 또는 CLI | 템플릿대로 작성. 모든 주장에 문단 ID·슬라이드 번호 링크. CLI: `notes NN` (원문 대조 결과 절은 verify 결과 그대로, lint 자동) |
| 8 | 원문 대조 | Claude 또는 CLI | WebSearch/WebFetch로 논문·기사 확인 → 판정 4분류(확인/상충/미발견/미확인) + 확인표. CLI: `claims NN` → (`sources --talk NN add …`) → `verify NN` (키 없는 검색, 인용문 기계 검증) |
| 9 | 종합 리포트 | Claude 또는 CLI | 관통 주제(발표자별 bullet + 다르게 본 점 + 다루지 않은 발표), 연구 라인별 아이디어(설정 시), 확인 필요. CLI: `report` (다루지 않은 발표는 인용 기준 자동 계산) |
| 10 | 검사 | CLI | `lint` : 링크·앵커·문단 ID·슬라이드 번호 해석 여부, 빈 절 보고 |

## 현 단계

- [x] 형식 분석 (첨부 리포트 + 위키 KDD2026 선례) → docs/format.md
- [x] Python 패키지 (init/ingest/asr/segment/align/scaffold/lint/status) + tests 38개 통과 (2026-10-29)
- [x] 스킬 SKILL.md → `~/.claude/skills/lecture-recon` 심링크 (2026-10-29)
- [x] 합성 행사 스모크(한국어 나레이션 93초 + EXIF 사진 3장 + 사진 없는 발표): CPU small 모델 34초, lint 오류 0 (2026-10-29)
- [x] CLI 판단 단계 백엔드(correct/claims/verify/notes/report/slides/sources, manual·OpenAI 호환 HTTP, 키 없는 검색) + tests 104개(103 통과, 1개 faster-whisper 조건부 skip; 코드 리뷰 회귀 테스트 포함) (2026-11-02). 실제 행사 사본 manual 스모크: arXiv·DuckDuckGo·arXiv PDF 정상, Semantic Scholar 키 없음은 429. 노트·리포트 lint 오류 0
- [x] 실제 강연 1건 end-to-end: AI TECH DAY 2026 신진우 키노트(37분, 사진 없음) → ~/research/lectures/ai-tech-day-2026/, lint 오류 0 (2026-10-29). 교훈: 강연장 녹음은 고유명사 오인식이 많아 원문 대조(기술보고서 전문)가 교정의 주 근거가 됨

## 열린 질문

- 녹음 기기가 둘(Mac/Windows)일 때 교차 검증을 어디까지 자동화할지 (현재: 녹음 하나 기준, 복수면 `asr`가 각각 돌리고 Claude가 대조)
- 녹음 시작 절대시각: 파일 메타(mtime/생성시각)가 틀릴 수 있어 `event.yaml`의 `started_at`을 우선. 사진 정렬 오차 보정은 `offset_sec`로 수동
