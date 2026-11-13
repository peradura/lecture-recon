---
name: lecture-recon
description: 강연 녹음·슬라이드 사진을 DAIS 위키식 3층 노트(전사/발표별 노트/종합 리포트)로 재구성. 교정·슬라이드 판독·원문 대조·연구 연결은 Claude가, 전사·정렬·골격·검사는 lecture-recon CLI가 담당
triggers: ["lecture-recon", "강연 정리", "강연 재구성", "녹음 정리", "출장 강연", "학회 노트"]
---

# lecture-recon — 강연 재구성 워크플로

레포: 이 스킬 폴더의 두 단계 위(기본 `~/research/lecture-recon`; 사양 `PROJECT.md`, 형식 `docs/format.md`). CLI: 레포의 `.venv/bin/lecture-recon`. 설치는 레포 README "설치".
출력: `~/research/lectures/<slug>/`. **공용 위키(`~/research/wiki`)에는 쓰지 않는다.** 위키 반영은 사용자 확인 후 별도.

## 원칙 (형식의 핵심)

1. **모든 문장에 근거 ID.** 전사 문단 `[NN-MMM](…#segment-NN-MMM)`, 슬라이드 `(슬라이드 NN)`. 근거 없는 문장은 쓰지 않는다.
2. **전사는 고치지 않는다.** 발언과 원문이 다르면 노트 "발언과 원문이 다른 것"에 둘 다 적고, 외부 인용은 원문값.
3. **지어내지 않는다.** 안 들리면 `[불명확]` 유지. 고유명사·용어 교정은 슬라이드·원문·공식 정보로 뒷받침될 때만.
4. **발표자 해석과 사실을 구분.** "발표자의 해석이다", "추정" 을 명시.
5. **과잉 일반화 금지.** 관통 주제마다 "다루지 않은 발표"를 적는다.
6. **사진 없는 발표도 같은 틀.** 슬라이드 절만 "사진이 없어 녹음 전사가 원자료의 전부다."로 대체. 신뢰도 절에 녹음 의존을 명시.
7. 한국어 단문 평서체("~다"). 숫자는 구체적으로. 템플릿 placeholder(`<…>`)를 남기지 않는다.

## 단계

진행 상황은 항상 `lecture-recon status <event_dir>` 로 확인하고 빠진 단계부터 한다.

### 0. 초기화 (사용자에게 받을 것)
행사명·날짜·장소·주관, 발표 목록(순서·발표자·소속·제목·언어), 녹음 파일, 사진 폴더, 녹음 시작 시각.
```
lecture-recon init <slug> --name "<행사명>" --date YYYY-MM-DD
```
→ `event.yaml` 을 받은 정보로 채운다. 녹음 시작 절대시각(`started_at`)을 모르면 사용자에게 묻고, 없으면 파일 메타로 추정하되 README에 "추정"으로 적는다. 원자료는 `raw/audio/`, `raw/photos/NN/` 에 둔다(복사 또는 심링크).

### 1. 수집 → 2. 전사 → 3. 문단화 (CLI)
```
lecture-recon ingest <event_dir>
lecture-recon asr <event_dir> --device cpu          # GPU(cuda)는 실행 전 반드시 사용자 승인 (공유 서버 규칙)
lecture-recon segment <event_dir>
```
- 긴 작업은 `run_in_background`. ASR 모델 기본 `large-v3`, 한국어 강연은 `--language ko`, 영어 혼용이면 `mixed`(자동 감지). faster-whisper가 없으면 `uv pip install -e ".[asr]"` (exit 2).
- `ingest`가 사진 축소본(`*-notes/slides/NN/<원파일명>.jpg`, 긴 변 1600px)과 EXIF manifest를 만든다.
- 녹음이 둘이면 둘 다 `asr` 하고, 교정 때 두 json을 대조한다(문단화는 첫 녹음 기준).
- `segment`는 기존 전사가 있으면 건너뛴다(교정본 보호, exit 1). 다시 만들려면 `--force`.

### 4. 전사 교정 (Claude)
각 `*-transcripts/NN_*.md` 를 읽고:
- 내용 전환마다 `## [HH:MM:SS] 장 제목` 을 붙인다(발표 흐름 단위, 5~12개).
- 고유명사·용어·숫자 오인식을 근거 있을 때만 고친다. 근거는 사진(슬라이드), 발표자 공식 자료, 다른 녹음의 전사. 고친 것은 README "검증 기록"에 종류·개수로 적는다.
- `[불명확: "…"]` 는 근거가 있을 때만 푼다. 푼 개수 기록.
- 문단 ID·타임스탬프·`<a id>` 줄은 **절대 바꾸지 않는다**(링크 깨짐).
- 상단 메타의 `교정: 0차 (초안)` 을 `교정: N차 (날짜)` 로, 안내 문장 `> 기계 전사 초안이다(교정 전)…` 를 format.md의 교정본 문장으로 바꾸고, Quality note placeholder를 실제 내용으로 채운다.

### 5. 사진 정렬 (CLI)
```
lecture-recon align <event_dir>
```
`work/align/NN.json` 이 생긴다(사진 → 녹음 안 시각 → 대응 문단). 전사 교정 후에 돌려도 된다(문단 시각은 전사 md에서 읽음). 정렬이 눈에 띄게 어긋나면(사진 속 내용과 문단이 안 맞음) `event.yaml` 의 `offset_sec` 를 조정하고 다시 돌린다. **반드시 scaffold 전에** 돌린다. 슬라이드 표가 이 결과로 미리 채워진다.

### 6. 슬라이드 판독 (Claude)
`*-notes/slides/NN/` 의 사진을 **Read 도구로 하나씩 본다**(OCR 엔진 없음, 직접 판독).
- 사진마다: 슬라이드 제목, 핵심 내용(수치·고유명사 그대로), 그림/표면 무엇의 그래프인지. 판독 불가는 `(판독 불가)`.
- 같은 슬라이드를 여러 장 찍었으면 하나로 합치고 "원 사진" 열에 번호를 모두 적는다.
- 슬라이드 번호는 노트 안에서 1부터 새로 매긴다. 사진 번호와 다르면 병기.
- 결과를 노트 `## 슬라이드` 표에 쓰고, `align` 의 대응 문단이 내용과 맞는지 확인해 고친다.

### 7. 발표별 노트 (Claude)
```
lecture-recon scaffold <event_dir>      # 골격 생성 (기존 파일 보존). --force 는 노트를 쓰기 전에만 쓴다(전부 덮어씀)
```
골격의 슬라이드 표는 촬영 순서로 슬라이드 번호, 원 사진 stem, 녹음 안 시각, 대응 문단 링크가 채워져 있고 제목·핵심 내용만 `<…>`다. 6단계 판독 결과로 채우고, 같은 슬라이드 중복 사진은 행을 합친다. 녹음 범위 밖 사진은 `(정렬 밖)`.
각 `*-notes/NN_*.md` 를 `docs/format.md` 2층 형식대로 채운다.
- 내용 절은 발표 흐름 순으로 4~10개. 각 주장 끝에 문단 링크, 슬라이드로 확인되면 `(슬라이드 NN)`.
- "연구 라인에 걸리는 것"은 연구 라인 설정(`~/.config/lecture-recon/research_lines.yaml` 또는 config.yaml `research_lines:`)이 **있을 때만** 둔다. 소제목은 설정의 라인 이름 그대로. **근거 있는 연결만.** 억지로 채우지 않는다. 라인 설명이 부족하면 사용자 메모리의 연구 주제를 참고한다. 설정이 없으면 이 절을 두지 않는다(scaffold 골격에도 없다).
- 골격 첫 줄 `<!-- lecture-recon:skeleton -->` 는 노트를 다 쓰면 지운다(손으로 고친 파일은 해시가 달라 CLI 가 덮어쓰지 않지만, 마커를 남기지 않는다). 리포트도 같다.
- 노트 README 와 전사 README 를 갱신(발표 목록·자료 규모·작업 기록).

### 8. 원문 대조 (Claude, 가능하면 항상)
노트의 수치·논문·시스템·기사 언급마다 WebSearch(standard; 못 찾으면 extended)/WebFetch 로 원문을 찾아 판정한다.
- 판정 4분류: **확인**(원문으로 수치·고유명사 대조) / **발언과 원문이 다른 것**(둘 다 기록) / **검색했으나 찾지 못한 것** / **확인하지 않은 것**(시간·범위상 미착수).
- 원문으로 대조한 것만 "확인". 검색 색인·2차 블로그만 본 것은 신뢰도를 낮춰 적는다.
- 대조일을 노트 상단에 적는다. 확인 못 한 수치는 "확인이 필요한 것"으로 보낸다.
- 네트워크가 없거나 사용자가 생략을 원하면 전부 "확인하지 않은 것"으로 두고 리포트 신뢰도 절에 명시.

### 9. 종합 리포트 (Claude)
`<slug>-report.md` 를 3층 형식대로 작성.
- 관통 주제 3~5개: 발표 셋 이상이 걸리는 주제 우선. 주제마다 도입 → 발표자별 bullet(노트 절 링크 + 문단 ID) → 다르게 본 점 → 다루지 않은 발표.
- "연구에 옮길 아이디어": 각 노트의 "연구 라인에 걸리는 것"을 라인별로 모은다. 노트에 없는 연결을 리포트에서 새로 만들지 않는다. 그 절이 있는 노트가 하나도 없으면 이 절을 두지 않는다.
- "자료 범위와 신뢰도": 글머리 3~5개, 각 한 줄(원자료·교정 수 / 불명확 개수 / 대조 날짜·4분류 건수 / 다른 사례 `발언 ↔ 원문값([NN-MMM])`). 설명문·분류 해설을 늘어놓지 않는다.
- 제목은 `# <행사명> 종합 리포트` 만. 관통 주제를 부제로 붙이지 않는다(사용자 지시 2026-11-11).

### 10. 검사 (CLI)
```
lecture-recon lint <event_dir>
```
오류 0이 될 때까지 고친다. 경고(빈 절, placeholder)도 처리하거나 사유를 README 작업 기록에 적는다.

## CLI 백엔드로 돌리는 경우

서버·Claude 토큰 없이(노트북 + 무료 LLM) 하려면 4·6·7·8·9단계를 CLI 가 한다. 규칙은 이 문서와 같고, 지시문은 스킬과 CLI 가 같은 파일을 쓴다:
레포의 `src/lecture_recon/prompts/{correct,claims,verify,notes,report,slides}.md`. 스킬 흐름에서 Claude 가 해당 단계를 할 때도 이 파일의 규칙(교정 거부 조건, 판정 4가지, 근거 표기)을 따른다.
```
lecture-recon correct <event_dir> NN --backend gemini     # 또는 --backend manual | ollama
lecture-recon claims  <event_dir> NN --backend gemini
lecture-recon sources <event_dir> --talk NN add <url|파일>   # 선택: 먼저 볼 자료
lecture-recon verify  <event_dir> NN --backend gemini
lecture-recon slides  <event_dir> NN --backend manual     # 사진 판독(무료 챗에 첨부)
lecture-recon notes   <event_dir> NN --backend gemini
lecture-recon report  <event_dir> --backend gemini
```
- `--backend manual`: `work/prompts/<이름>.md` 를 무료 챗에 붙여넣고 응답을 `work/responses/<이름>.md` 에 저장, 같은 명령 재실행(exit 3 = 응답 대기). 응답 첫 줄 `request_id: …`.
- `--plan` 은 LLM 을 부르지 않고 호출 횟수만 센다(gemini flash 무료는 하루 20회). exit 4 = 일일 한도 소진(다음 초기화 시각 KST 를 알려 준다).
- `lecture-recon fill <event_dir>` 은 LLM 없이 README 처리 과정·검증 기록·자료 규모·작업 기록과 전사 Quality note 의 기계 집계 문장을 채운다. 스킬 흐름에서는 이 줄들을 실제 내용으로 고쳐 쓴다(녹음 상태 등).
- 설정이 없으면 backend 는 `claude-code` 이고 CLI 는 LLM 을 부르지 않는다. 기본값은 `~/.config/lecture-recon/config.yaml` 의 `backend:`.
- 사용자 형식이 있으면 그것을 따른다: config.yaml `templates_dir`·`prompts_dir`(같은 이름 파일이 패키지 것을 대신함), `required_sections`(lint 필수 절). 스킬 흐름에서 노트·리포트를 직접 쓸 때도 그 템플릿·프롬프트를 읽어 따른다.
- CLI 결과물(`work/verify/NN.json`, `work/correct/NN.json`)은 스킬 흐름에서도 근거로 쓸 수 있다. 판정이 "확인하지 않은 것"(사유: 검색 실패·인용문 불일치 등)인 항목은 WebSearch/WebFetch 로 다시 대조한다.
- 설계·한계는 레포의 `docs/design-backends.md`.

## 마무리 보고
사용자에게: 생성 파일 경로, 발표별 사진 유무·대조 판정 수, 남은 불명확 개수, 확인 못 한 항목 수, lint 결과. 위키 반영 여부는 묻는다.

## 자주 틀리는 것
- 문단 ID 링크 경로: 노트에서 전사는 `../<slug>-transcripts/`, 리포트에서 노트는 `<slug>-notes/`, 전사는 `<slug>-transcripts/`.
- 절 앵커는 GitHub 규칙(소문자, 공백→`-`, 구두점 제거). lint 가 검사한다.
- 발표자 한 명의 발언을 "연사 다수"로 쓰지 않는다. 몇 명인지 센다.
