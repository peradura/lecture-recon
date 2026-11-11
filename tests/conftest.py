import pytest

import synth
from lecture_recon.config import load_event


@pytest.fixture(autouse=True)
def isolated_llm_env(tmp_path_factory, monkeypatch):
    """사용자 설정 파일·환경변수가 테스트에 새지 않게 한다. 설정 디렉터리(기본 research_lines.yaml 위치)도 임시 디렉터리다."""
    monkeypatch.setenv("LECTURE_RECON_CONFIG", str(tmp_path_factory.mktemp("cfg") / "none.yaml"))
    for name in ("LECTURE_RECON_BACKEND", "LECTURE_RECON_MODEL", "LECTURE_RECON_BASE_URL", "LECTURE_RECON_API_KEY",
                 "LECTURE_RECON_TEMPLATES_DIR", "LECTURE_RECON_PROMPTS_DIR",
                 "GEMINI_API_KEY", "GROQ_API_KEY", "OPENROUTER_API_KEY", "OPENAI_API_KEY",
                 "TAVILY_API_KEY", "BRAVE_API_KEY"):
        monkeypatch.delenv(name, raising=False)


@pytest.fixture
def event_dir(tmp_path):
    return synth.build_event(tmp_path)


@pytest.fixture
def event(event_dir):
    return load_event(event_dir)


@pytest.fixture
def built(tmp_path):
    """init → 사진 ingest → segment → align → scaffold 까지 된 합성 행사."""
    return synth.build_scaffolded(tmp_path)
