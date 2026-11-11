"""OpenAI 호환 chat 클라이언트(stdlib urllib) + 설정·캐시·재시도·manual/claude-code 백엔드.

설정 우선순위: CLI 옵션 > 환경변수(LECTURE_RECON_*) > 설정 파일(~/.config/lecture-recon/config.yaml) > preset.
형식 덮어쓰기: templates_dir·prompts_dir(환경변수 LECTURE_RECON_TEMPLATES_DIR·LECTURE_RECON_PROMPTS_DIR 우선)에 같은 이름의
파일이 있으면 그것을, 없으면 패키지 것을 쓴다. 설정 파일의 상대 경로(research_lines·templates_dir·prompts_dir)는 설정 파일
디렉터리 기준이다. 연구 라인 설정(research_lines)은 LLM 설정 검증과 무관하게 read_config() 만으로 읽는다.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
import re
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from importlib.resources import files
from pathlib import Path
from typing import Callable
from zoneinfo import ZoneInfo

import yaml

from .config import LectureReconError

SCHEMA_VERSION = "1"
CONFIG_PATH = "~/.config/lecture-recon/config.yaml"
RESEARCH_LINES_FILE = "research_lines.yaml"
DEFAULT_SEARCH = ["arxiv", "semanticscholar", "ddg"]
MAX_RETRIES = 5
RETRY_STATUS = (429, 500, 502, 503, 504)


@dataclass(frozen=True)
class Preset:
    base_url: str
    model: str
    key_env: str  # 비면 키 없이 호출(ollama)


# 2026-11-02 각 사 공식 문서 기준. 무료 모델 이름은 자주 바뀌므로 --model 이나 설정 파일로 덮어쓴다.
PRESETS = {
    "gemini": Preset("https://generativelanguage.googleapis.com/v1beta/openai", "gemini-3.5-flash", "GEMINI_API_KEY"),
    "groq": Preset("https://api.groq.com/openai/v1", "llama-3.3-70b-versatile", "GROQ_API_KEY"),
    "openrouter": Preset("https://openrouter.ai/api/v1", "openrouter/free", "OPENROUTER_API_KEY"),
    "ollama": Preset("http://localhost:11434/v1", "qwen3:8b", ""),
    "openai": Preset("https://api.openai.com/v1", "gpt-6-luna", "OPENAI_API_KEY"),
}
BACKENDS = (*PRESETS, "manual", "claude-code")


class ResponseError(LectureReconError):
    """LLM 응답이 요구한 형식이 아니다(JSON 깨짐, id 불일치 등)."""


class LLMError(LectureReconError):
    """HTTP 호출 실패(재시도 후)."""


class QuotaExhausted(LLMError):
    """무료 티어 일일 한도 소진(429). 재시도하지 않는다. exit 4 — 초기화 뒤 같은 명령을 다시 실행한다."""

    exit_code = 4


# 2026-11-06 실측(새 무료 키). --plan 출력에 참고로 붙인다.
MEASURED_LIMITS = {
    "gemini-3.5-flash": "무료 티어 20회/일(RPD, 실측 2026-11-06)",
    "gemini-3.5-flash-lite": "flash 와 별도 한도. claims 는 후보 문장을 줘도 보수적(실측: 후보 34개 → claim 7개)",
}
DAILY_RE = re.compile(r"per[\s_-]*day|PerDay|daily", re.I)
# Google 구조화 필드(QuotaFailure.violations[].quotaId). Gemini 는 일일 한도에도 짧은 retryDelay 를 붙이므로 이것을 믿는다
QUOTA_ID_DAY_RE = re.compile(r'"quotaId"\s*:\s*"[^"]*PerDay', re.I)
LIMIT_RE = re.compile(r"limit:\s*(\d+)", re.I)
METRIC_RE = re.compile(r"metric:\s*([\w./-]+)|\"quotaMetric\"\s*:\s*\"([^\"]+)\"")
QUOTA_MODEL_RE = re.compile(r"model:\s*([\w.\-]+)|\"model\"\s*:\s*\"([^\"]+)\"")
RETRY_HINT_RE = re.compile(r'"retryDelay"\s*:\s*"(\d+(?:\.\d+)?)s"'
                           r"|(?:try again|retry) in\s*(\d+(?:\.\d+)?)\s*(ms|s|sec|seconds?|m|min|minutes?|h)?\b", re.I)
SHORT_HINT_SEC = 600.0
KST = ZoneInfo("Asia/Seoul")
PACIFIC = ZoneInfo("America/Los_Angeles")


def retry_hint(body: str) -> float | None:
    """429 본문의 재시도 안내(초): RetryInfo.retryDelay, "try again in 6.5s", "Please retry in 41.2s"."""
    m = RETRY_HINT_RE.search(body or "")
    if not m:
        return None
    if m.group(1):
        return float(m.group(1))
    value, unit = float(m.group(2)), (m.group(3) or "s").lower()
    return value / 1000 if unit == "ms" else value * 60 if unit.startswith("m") else value * 3600 if unit == "h" \
        else value


def daily_quota(body: str) -> dict | None:
    """429 가 일일 한도 소진이면 {limit, metric, model, hint}. 아니면 None(백오프로 기다린다).
    일일로 보는 것: Google `quotaId` 에 PerDay, 또는 본문에 per day/per-day/PerDay/daily 가 있고 10분 안의 재시도 안내가
    없을 때(groq "tokens per day … try again in 6.5s" 처럼 짧은 안내는 굴러가는 창이라 기다린다). 토큰 수 한도(TPM)·
    "limit: N" 만 있는 것은 분 단위로 본다."""
    body = body or ""
    structured = bool(QUOTA_ID_DAY_RE.search(body))
    hint = retry_hint(body)
    if not structured and (not DAILY_RE.search(body) or (hint is not None and hint < SHORT_HINT_SEC)):
        return None
    limit = LIMIT_RE.search(body)
    metric = METRIC_RE.search(body)
    model = QUOTA_MODEL_RE.search(body)
    return {"limit": int(limit.group(1)) if limit else None,
            "metric": (metric.group(1) or metric.group(2)) if metric else "",
            "model": (model.group(1) or model.group(2)) if model else "", "hint": hint}


def next_reset(now: dt.datetime | None = None, tz: ZoneInfo = PACIFIC) -> dt.datetime:
    """일일 한도 초기화 시각(tz 의 다음 자정)을 KST 로. Gemini = 태평양 시각, OpenRouter = UTC."""
    now = (now or dt.datetime.now(dt.timezone.utc)).astimezone(tz)
    midnight = dt.datetime.combine(now.date() + dt.timedelta(days=1), dt.time(0), tzinfo=tz)
    return midnight.astimezone(KST)


def reset_text(backend: str, quota: dict, now: dt.datetime | None = None) -> str:
    if backend == "gemini":
        return f"{next_reset(now):%Y-%m-%d %H:%M} KST (태평양 시각 자정)"
    if backend == "openrouter":
        return f"{next_reset(now, ZoneInfo('UTC')):%Y-%m-%d %H:%M} KST (UTC 자정)"
    hint = quota.get("hint")
    return (f"알 수 없다 — 서버 안내대로 {hint:.0f}초 뒤 다시 시도한다" if hint is not None
            else "알 수 없다 — 잠시 뒤(또는 한도 창이 지난 뒤) 다시 시도한다")


def quota_message(backend: str, model: str, quota: dict, now: dt.datetime | None = None,
                  why: str = "일일 무료 한도를 다 썼다") -> str:
    limit = f"{quota['limit']}회" if quota.get("limit") is not None else "알 수 없음"
    return "\n".join([
        f"{backend}/{model}: {why}(HTTP 429) — 더 재시도하지 않고 멈춘다.",
        f"  한도: {limit} · metric: {quota.get('metric') or '알 수 없음'}"
        + (f" · model: {quota['model']}" if quota.get("model") else ""),
        f"  다음 초기화: {reset_text(backend, quota, now)}",
        "성공한 응답은 work/llm-cache/ 에 있다. 초기화 뒤 같은 명령을 다시 실행하면 이어서 한다(exit 4).",
    ])


class ManualPending(LectureReconError):
    """manual 백엔드: 사용자가 응답 파일을 채워야 한다. exit 3."""

    exit_code = 3

    def __init__(self, items: list[tuple[str, str]], root: Path | None = None):
        self.items = items
        self.root = root
        super().__init__(self.render())

    def render(self) -> str:
        lines = [f"수동(manual) 백엔드: 응답이 필요한 프롬프트 {len(self.items)}개."]
        for name, problem in self.items:
            lines.append(f"  - work/prompts/{name}.md → work/responses/{name}.md ({problem})")
        lines += [
            "프롬프트 파일 전체를 무료 챗(Gemini 앱·ChatGPT·Claude 무료 등)에 붙여넣고, 응답 전체를",
            "같은 이름으로 work/responses/ 에 저장한 뒤 같은 명령을 다시 실행한다. 응답 첫 줄은 `request_id: …` 다.",
        ]
        if self.root is not None:
            lines.append(f"행사 디렉터리: {self.root}")
        return "\n".join(lines)


def config_path() -> Path:
    return Path(os.environ.get("LECTURE_RECON_CONFIG") or CONFIG_PATH).expanduser()


def config_dir() -> Path:
    """설정 파일이 있는 디렉터리(기본 ~/.config/lecture-recon). 기본 research_lines.yaml 도 여기서 찾는다."""
    return config_path().parent


def read_config(path: Path | None = None) -> dict:
    """설정 파일의 매핑. 파일이 없으면 {}."""
    path = path or config_path()
    if not path.is_file():
        return {}
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as e:
        raise LectureReconError(f"{path}: YAML 파싱 실패: {e}") from e
    if not isinstance(data, dict):
        raise LectureReconError(f"{path}: 최상위가 매핑이어야 한다")
    return data


def config_relative(value: str) -> Path:
    """설정 파일에 적은 경로: `~` 확장, 상대 경로는 설정 파일 디렉터리 기준(CWD 가 아니다). 절대 경로로 돌려준다."""
    path = Path(value).expanduser()
    return (path if path.is_absolute() else config_dir() / path).absolute()


def research_lines_path() -> Path | None:
    """연구 라인 설정 파일. config `research_lines:` 가 false 면 끈다(None), 경로면 그 파일(없으면 오류),
    비었거나 없으면 설정 디렉터리의 research_lines.yaml(없으면 None)."""
    value = read_config().get("research_lines")
    if value is False:
        return None
    if value is not None and not isinstance(value, str):
        raise LectureReconError(f"{config_path()}: research_lines 는 경로 문자열 또는 false 여야 한다: {value!r}")
    if value and value.strip():
        path = config_relative(value.strip())
        if not path.is_file():
            raise LectureReconError(f"research_lines 파일이 없다: {path} ({config_path()} `research_lines:`)")
        return path
    path = config_dir() / RESEARCH_LINES_FILE
    return path.absolute() if path.is_file() else None


def research_lines() -> list[dict]:
    """연구 라인 설정의 `lines:` 중 name 이 있는 항목. 설정이 없거나 꺼져 있으면 []."""
    path = research_lines_path()
    if path is None:
        return []
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as e:
        raise LectureReconError(f"{path}: YAML 파싱 실패: {e}") from e
    if data is None:
        return []
    if not isinstance(data, dict):
        raise LectureReconError(f"{path}: 최상위가 매핑(`lines:` 아래 목록)이어야 한다")
    lines = data.get("lines") or []
    if not isinstance(lines, list):
        raise LectureReconError(f"{path}: `lines:` 는 목록이어야 한다")
    return [x for x in lines if isinstance(x, dict) and x.get("name")]


def claude_code_refusal() -> str:
    return "\n".join([
        "backend 가 claude-code 다(설정이 없을 때의 기본값). 이 경로에서는 CLI 가 LLM 을 부르지 않는다.",
        "Claude Code 스킬 `lecture-recon` 흐름에서 Claude 가 같은 프롬프트(src/lecture_recon/prompts/*.md)로 직접 한다.",
        "CLI 에서 무료로 돌리려면:",
        "  --backend manual                     키 없음. 프롬프트 파일을 무료 챗에 붙여넣고 응답을 저장한다",
        "  --backend gemini  (GEMINI_API_KEY)   Google AI Studio 무료 키",
        "  --backend ollama                     로컬 모델, http://localhost:11434",
        f"기본값을 바꾸려면 설정 파일 {config_path()} 에 `backend: gemini` 처럼 적는다.",
    ])


# 설정 -------------------------------------------------------------------------

@dataclass
class Settings:
    backend: str = "claude-code"
    model: str = ""
    base_url: str = ""
    api_key_env: str = ""
    api_key: str = ""
    rpm: float = 0.0
    search: list[str] = field(default_factory=lambda: list(DEFAULT_SEARCH))
    cache: bool = True
    origin: str = "기본값"


def load_settings(backend: str | None = None, model: str | None = None, rpm: float | None = None,
                  no_cache: bool = False, env: dict | None = None, path: Path | None = None) -> Settings:
    env = os.environ if env is None else env
    path = path or config_path()
    data = read_config(path)

    file_backend = str(data.get("backend") or "").strip()
    chosen, origin = "claude-code", "기본값"
    for value, where in ((file_backend, "설정 파일"), (env.get("LECTURE_RECON_BACKEND", ""), "환경변수"),
                         (backend or "", "CLI")):
        if value:
            chosen, origin = value.strip(), where
    if chosen not in BACKENDS:
        raise LectureReconError(f"backend 는 {'|'.join(BACKENDS)} 중 하나다: {chosen!r} ({origin})")
    same = chosen == file_backend  # 파일의 model/base_url/api_key_env 는 같은 backend 일 때만 쓴다
    preset = PRESETS.get(chosen)

    def pick(cli, env_name, file_key, default):
        for value in (cli, env.get(env_name, "") if env_name else "", data.get(file_key) if same else None):
            if value not in (None, ""):
                return str(value).strip()
        return default

    s = Settings(backend=chosen, origin=origin)
    s.model = pick(model, "LECTURE_RECON_MODEL", "model", preset.model if preset else chosen)
    s.base_url = pick(None, "LECTURE_RECON_BASE_URL", "base_url", preset.base_url if preset else "").rstrip("/")
    s.api_key_env = pick(None, "", "api_key_env", preset.key_env if preset else "")
    s.api_key = env.get("LECTURE_RECON_API_KEY", "") or (env.get(s.api_key_env, "") if s.api_key_env else "")
    raw_rpm = rpm if rpm is not None else data.get("rpm", 0)
    try:
        s.rpm = float(raw_rpm or 0)
    except (TypeError, ValueError):
        raise LectureReconError(f"rpm 은 숫자여야 한다: {raw_rpm!r}") from None
    search = data.get("search")
    if search is not None:
        if not isinstance(search, list) or not all(isinstance(x, str) for x in search):
            raise LectureReconError(f"{path}: search 는 문자열 목록이어야 한다")
        s.search = [x.strip().lower() for x in search]
    s.cache = not no_cache and data.get("cache", True) is not False
    return s


# 프롬프트·JSON ------------------------------------------------------------------

def user_dir(kind: str) -> Path | None:
    """형식 덮어쓰기 디렉터리(kind = templates|prompts): 환경변수 LECTURE_RECON_<KIND>_DIR > 설정 파일 `<kind>_dir:`.
    설정 파일의 상대 경로는 설정 파일 디렉터리 기준, 환경변수의 상대 경로는 현재 디렉터리 기준."""
    env_name = f"LECTURE_RECON_{kind.upper()}_DIR"
    value = os.environ.get(env_name, "").strip()
    if value:
        path, origin = Path(value).expanduser().absolute(), f"환경변수 {env_name}"
    else:
        value = str(read_config().get(f"{kind}_dir") or "").strip()
        if not value:
            return None
        path, origin = config_relative(value), f"{config_path()} `{kind}_dir:`"
    if not path.is_dir():
        raise LectureReconError(f"{kind}_dir 디렉터리가 없다: {path} ({origin})")
    return path


def _resource(kind: str, name: str):
    """사용자 디렉터리에 같은 이름의 파일이 있으면 그것, 없으면 패키지 데이터 <kind>/<name>."""
    d = user_dir(kind)
    if d is not None and (d / name).is_file():
        return d / name
    return files("lecture_recon") / kind / name


def template_path(name: str):
    """templates/<name> (note.md, report.md, notes_readme.md, transcripts_readme.md, event.yaml, event.gitignore)."""
    return _resource("templates", name)


def prompt_path(name: str):
    """prompts/<name>.md (correct, claims, verify, notes, report, slides)."""
    return _resource("prompts", f"{name}.md")


def prompt(name: str) -> str:
    """prompts/<name>.md (CLI·스킬 공용 지시문). prompts_dir 에 같은 이름이 있으면 그것."""
    return prompt_path(name).read_text(encoding="utf-8")


FENCE_RE = re.compile(r"```[A-Za-z]*\s*\n?(.*?)```", re.S)
THINK_RE = re.compile(r"<think>.*?</think>", re.S)


def _json_values(text: str, limit: int = 200):
    """text 안에서 해석되는 JSON 값들(코드 펜스 안 → 전체 → 각 `{`/`[` 위치)을 차례로 낸다."""
    decoder = json.JSONDecoder()
    candidates = [m.group(1).strip() for m in FENCE_RE.finditer(text)] + [text]
    n = 0
    for cand in candidates:
        try:
            yield json.loads(cand)
            continue
        except json.JSONDecodeError:
            pass
        i = 0
        while i < len(cand) and n < limit:
            if cand[i] in "{[":
                n += 1
                try:
                    value, end = decoder.raw_decode(cand, i)
                    yield value
                    i = end
                    continue
                except json.JSONDecodeError:
                    pass
            i += 1


def extract_json(text: str, keys: tuple[str, ...] | None = None):
    """응답에서 JSON 을 꺼낸다(<think>·코드 펜스 제거). 객체를 배열보다 먼저 고른다(본문의 `[1]` 각주에 속지 않게).
    keys 를 주면 그 키 중 하나를 가진 첫 객체, 없으면 객체 목록인 첫 배열. 잘렸거나 없으면 ResponseError."""
    text = THINK_RE.sub("", text or "").strip()
    first_list = first_any = None
    found = False
    for value in _json_values(text):
        if not found:
            first_any, found = value, True
        if isinstance(value, dict) and (keys is None or any(k in value for k in keys)):
            return value
        if first_list is None and isinstance(value, list) and (keys is None or all(isinstance(v, dict) for v in value)):
            first_list = value
    if first_list is not None:
        return first_list
    if keys is None and found:
        return first_any
    tail = text[-80:].replace("\n", " ")
    want = f" (`{'`/`'.join(keys)}` 키를 가진 객체)" if keys else ""
    raise ResponseError(f"JSON{want}을 찾지 못했다(잘렸거나 형식 오류). 응답 끝: …{tail!r}")


def json_object(text: str, key: str) -> list:
    """`{key: [...]}` 또는 맨 배열(객체 목록)을 받아 목록을 돌려준다."""
    data = extract_json(text, keys=(key,))
    if isinstance(data, dict):
        data = data.get(key)
    if not isinstance(data, list):
        raise ResponseError(f'`{{"{key}": [...]}}` 형식이 아니다')
    return data


def sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def messages_key(messages: list[dict]) -> str:
    return json.dumps(messages, ensure_ascii=False, sort_keys=True)


def cache_key(backend: str, model: str, messages: list[dict], base_url: str = "") -> str:
    key = {"backend": backend, "model": model, "messages": messages}
    if base_url:  # 같은 backend 이름이라도 다른 서버(예: 원격 ollama)면 다른 응답이다
        key["base_url"] = base_url
    return sha256(json.dumps(key, ensure_ascii=False, sort_keys=True))


def request_id(backend: str, name: str, messages: list[dict]) -> str:
    """backend + 명령 이름 + 입력 해시 + 스키마 버전의 짧은 해시."""
    return sha256(f"{SCHEMA_VERSION}|{backend}|{name}|{sha256(messages_key(messages))}")[:12]


def _write_atomic(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


# 백엔드 -----------------------------------------------------------------------

class HTTPBackend:
    """POST {base_url}/chat/completions. 429/5xx 는 지수 백오프(최대 5회), 연결 오류는 1회 재시도."""

    _last_call: dict[str, float] = {}

    def __init__(self, settings: Settings, sleep: Callable[[float], None] = time.sleep,
                 opener: Callable = urllib.request.urlopen, timeout: float = 180.0):
        self.name = settings.backend
        self.model = settings.model
        self.base_url = settings.base_url
        self.api_key = settings.api_key
        self.rpm = settings.rpm
        self.sleep = sleep
        self.opener = opener
        self.timeout = timeout
        if settings.api_key_env and not self.api_key:
            raise LectureReconError(f"{self.name}: API 키가 없다. 환경변수 {settings.api_key_env} (또는 LECTURE_RECON_API_KEY)를 설정한다.\n"
                                    "키 없이 하려면 --backend manual 을 쓴다.")

    def _throttle(self) -> None:
        if self.rpm <= 0:
            return
        gap = 60.0 / self.rpm
        last = HTTPBackend._last_call.get(self.name)
        if last is not None:
            wait = last + gap - time.monotonic()
            if wait > 0:
                self.sleep(wait)
        HTTPBackend._last_call[self.name] = time.monotonic()

    def _request(self, body: dict) -> urllib.request.Request:
        headers = {"Content-Type": "application/json", "User-Agent": "lecture-recon/0.1"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        if self.name == "openrouter":
            headers["X-OpenRouter-Title"] = "lecture-recon"
        return urllib.request.Request(f"{self.base_url}/chat/completions", data=json.dumps(body).encode("utf-8"),
                                      headers=headers, method="POST")

    def complete(self, messages: list[dict], json_mode: bool = False) -> str:
        body = {"model": self.model, "messages": messages, "temperature": 0.2}
        if json_mode:
            body["response_format"] = {"type": "json_object"}
        conn_retries = 0
        attempt = 0
        while True:
            self._throttle()
            try:
                with self.opener(self._request(body), timeout=self.timeout) as resp:
                    raw = resp.read()
                try:
                    data = json.loads(raw.decode("utf-8", "replace"))
                except ValueError:
                    raise ResponseError(f"{self.name}: 응답 본문이 JSON 이 아니다: {raw[:160]!r}") from None
                break
            except urllib.error.HTTPError as e:
                full = e.read()[:16000].decode("utf-8", "replace") if e.fp else ""
                detail = full[:400]
                if e.code == 400 and "response_format" in body:
                    body.pop("response_format")  # JSON 모드를 모르는 서버: 프롬프트 지시만으로 다시
                    continue
                quota = daily_quota(full) if e.code == 429 else None
                if quota:  # 일일 한도는 기다려도 안 풀린다: 백오프 재시도로 시간을 쓰지 않는다
                    raise QuotaExhausted(quota_message(self.name, self.model, quota)) from None
                if e.code == 429 and attempt >= MAX_RETRIES:  # 분 단위로 보였지만 끝내 안 풀렸다: 한도로 보고 멈춘다
                    q = {"limit": int(m.group(1)) if (m := LIMIT_RE.search(full)) else None,
                         "metric": (m.group(1) or m.group(2)) if (m := METRIC_RE.search(full)) else "",
                         "model": "", "hint": retry_hint(full)}
                    raise QuotaExhausted(quota_message(self.name, self.model, q, why=(
                        f"429 가 재시도 {MAX_RETRIES}회 뒤에도 계속된다(한도로 본다)"))) from None
                if e.code in RETRY_STATUS and attempt < MAX_RETRIES:
                    retry_after = e.headers.get("Retry-After") if e.headers else None
                    hinted = retry_hint(full)
                    try:
                        delay = (min(120.0, float(retry_after)) if retry_after
                                 else min(120.0, hinted) if hinted is not None
                                 else min(60.0, 2.0 ** (attempt + 1)))
                    except ValueError:
                        delay = min(60.0, 2.0 ** (attempt + 1))
                    attempt += 1
                    self.sleep(delay)
                    continue
                raise LLMError(f"{self.name} HTTP {e.code} (재시도 {attempt}회 후): {detail}") from None
            except (urllib.error.URLError, TimeoutError, ConnectionError, OSError) as e:
                if conn_retries < 1:
                    conn_retries += 1
                    self.sleep(2.0)
                    continue
                raise LLMError(f"{self.name} 연결 실패 ({self.base_url}): {e}") from None
        try:
            content = data["choices"][0]["message"]["content"] or ""
        except (KeyError, IndexError, TypeError):
            raise LLMError(f"{self.name}: 응답에 choices[0].message.content 가 없다: {str(data)[:300]}") from None
        return THINK_RE.sub("", content).strip()


class ManualBackend:
    name = "manual"
    model = "manual"


# 클라이언트 -------------------------------------------------------------------

REQUEST_LINE_RE = re.compile(r"^\s*request_id\s*:\s*`?([0-9a-f]{6,64})`?\s*$", re.I)
REQUEST_JSON_RE = re.compile(r'"request_id"\s*:\s*"([0-9a-f]{6,64})"', re.I)


def split_request_id(text: str) -> tuple[str | None, str]:
    """응답 첫 줄 `request_id: …` (또는 JSON 안 "request_id") 와 나머지 본문."""
    text = text.lstrip("﻿")
    lines = text.splitlines()
    for i, line in enumerate(lines):
        if not line.strip():
            continue
        m = REQUEST_LINE_RE.match(line)
        if m:
            return m.group(1).lower(), "\n".join(lines[i + 1:]).strip()
        break
    m = REQUEST_JSON_RE.search(text)
    return (m.group(1).lower(), text.strip()) if m else (None, text.strip())


class LLM:
    """캐시(work/llm-cache/<sha256>.json)와 manual 프롬프트·응답 파일을 다루는 호출기."""

    def __init__(self, backend, work_dir: Path, cache: bool = True):
        self.backend = backend
        self.work_dir = Path(work_dir)
        self.use_cache = cache
        self.calls = 0       # 실제 API 호출 수(캐시 적중 제외)
        self.cache_hits = 0

    @property
    def label(self) -> str:
        return f"{self.backend.name}/{self.backend.model}"

    @property
    def manual(self) -> bool:
        return self.backend.name == "manual"

    def ask(self, name: str, system: str, user: str, *, json_mode: bool = False, validate=None):
        """validate(text) → 파싱 결과 (ResponseError 면 형식 오류). 없으면 본문 문자열."""
        messages = [{"role": "system", "content": system}, {"role": "user", "content": user}]
        check = validate or (lambda t: t)
        if self.manual:
            return self._manual(name, messages, check)
        key = cache_key(self.backend.name, self.backend.model, messages, getattr(self.backend, "base_url", ""))
        path = self.work_dir / "llm-cache" / f"{key}.json"
        if self.use_cache and path.exists():
            try:
                parsed = check(json.loads(path.read_text(encoding="utf-8"))["response"])
                self.cache_hits += 1
                return parsed
            except (ResponseError, KeyError, json.JSONDecodeError):
                pass  # 깨진 캐시는 무시하고 다시 부른다
        text = self.backend.complete(messages, json_mode)
        self.calls += 1
        try:
            parsed = check(text)
        except ResponseError as e:
            retry = messages + [{"role": "assistant", "content": text},
                                {"role": "user", "content": f"응답 형식 오류: {e}\n지시한 형식만 처음부터 다시 출력한다."}]
            text = self.backend.complete(retry, json_mode)
            self.calls += 1
            try:
                parsed = check(text)
            except ResponseError as e2:  # 하위 종류(예: verify.EmptyClaims)는 그대로 둔다: 부른 쪽이 가려 처리한다
                raise type(e2)(f"{name}: {self.label} 응답 형식 오류(1회 재요청 후): {e2}") from None
        if self.use_cache:
            _write_atomic(path, json.dumps({"backend": self.backend.name, "model": self.backend.model, "name": name,
                                            "created": dt.datetime.now().isoformat(timespec="seconds"),
                                            "response": text}, ensure_ascii=False, indent=1))
        return parsed

    def _manual(self, name: str, messages: list[dict], check):
        rid = request_id(self.backend.name, name, messages)
        prompt_path = self.work_dir / "prompts" / f"{name}.md"
        resp_path = self.work_dir / "responses" / f"{name}.md"
        _write_atomic(prompt_path, manual_prompt(name, rid, messages))
        resp_path.parent.mkdir(parents=True, exist_ok=True)
        if not resp_path.exists() or not resp_path.read_text(encoding="utf-8").strip():
            raise ManualPending([(name, "응답 없음")])
        got, body = split_request_id(resp_path.read_text(encoding="utf-8"))
        if got is None:
            raise ManualPending([(name, f"응답에 request_id 가 없다 — 첫 줄에 `request_id: {rid}` 를 넣는다")])
        if got != rid:
            raise ManualPending([(name, f"request_id 불일치(응답 {got}, 현재 {rid}) — 프롬프트가 바뀌었다. "
                                        "새 프롬프트를 다시 붙여넣고 응답을 새로 저장한다")])
        try:
            return check(body)
        except ResponseError as e:
            raise ManualPending([(name, f"형식 오류: {e} — 같은 프롬프트로 응답을 다시 받는다")]) from None


def manual_prompt(name: str, rid: str, messages: list[dict]) -> str:
    system = next(m["content"] for m in messages if m["role"] == "system")
    user = next(m["content"] for m in messages if m["role"] == "user")
    return "\n".join([
        f"<!-- lecture-recon manual prompt: {name} · request_id {rid} · 응답 저장 위치 work/responses/{name}.md -->",
        "",
        "아래 전체를 하나의 메시지로 붙여넣는다. 필요한 자료는 모두 이 안에 있다(로컬 파일을 읽을 수 없다고 가정한다).",
        "",
        "# 지시",
        "",
        system.strip(),
        "",
        "# 응답 첫 줄",
        "",
        f"응답의 첫 줄은 정확히 `request_id: {rid}` 이다. 둘째 줄부터 지시한 형식만 쓴다(설명·인사 없이).",
        "",
        "# 입력",
        "",
        user.strip(),
        "",
    ])


def make_llm(settings: Settings, work_dir: Path) -> LLM:
    if settings.backend == "claude-code":
        raise LectureReconError(claude_code_refusal())
    backend = ManualBackend() if settings.backend == "manual" else HTTPBackend(settings)
    return LLM(backend, work_dir, cache=settings.cache)


def collect_pending(run: Callable[[], object], pending: list[tuple[str, str]]):
    """manual 백엔드에서 프롬프트를 모두 쓰도록 ManualPending 을 모은다. 대기면 None."""
    try:
        return run()
    except ManualPending as e:
        pending.extend(e.items)
        return None
