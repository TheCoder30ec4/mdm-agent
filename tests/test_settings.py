"""agent/settings.py and agent/llm.py: precedence, the file, and the model factory (no network)."""
import json

import pytest

from agent import llm, settings

ENV = ("GROQ_API_KEY", "ANTHROPIC_API_KEY", "OPENAI_API_KEY", "GOOGLE_API_KEY", "MDM_PROVIDER", "MDM_MODEL",
       "GROQ_MODEL", "MDM_BASE_URL", "FIRECRAWL_API_URL", "FIRECRAWL_API_KEY")


@pytest.fixture
def path(tmp_path, monkeypatch):
    for k in ENV:
        monkeypatch.delenv(k, raising=False)
    return tmp_path / "lib" / "settings.json"


def test_env_only_groq_is_as_before(path, monkeypatch):
    monkeypatch.setenv("GROQ_API_KEY", "k1")
    monkeypatch.setenv("GROQ_MODEL", "llama-3.3-70b-versatile")
    cfg = settings.effective(path)
    assert (cfg["provider"], cfg["model"], cfg["keys"]["groq"]) == ("groq", "llama-3.3-70b-versatile", "k1")
    assert cfg["firecrawl_url"] == "http://localhost:3002" and cfg["firecrawl_key"] is None
    assert settings.ready(cfg) and not path.exists()


def test_file_beats_env_beats_default(path, monkeypatch):
    monkeypatch.setenv("MDM_PROVIDER", "openai")
    monkeypatch.setenv("OPENAI_API_KEY", "env-key")
    monkeypatch.setenv("FIRECRAWL_API_URL", "http://env:3002")
    assert settings.effective(path)["model"] == "gpt-5.1"  # default for the env provider
    monkeypatch.setenv("GROQ_MODEL", "groq-only")  # not used for another provider
    assert settings.effective(path)["model"] == "gpt-5.1"
    settings.update(path, {"api_key": "file-key", "firecrawl_url": "https://fc.example"})
    cfg = settings.effective(path)
    assert cfg["keys"]["openai"] == "file-key" and cfg["firecrawl_url"] == "https://fc.example"
    settings.update(path, {"api_key": ""})  # cleared: the env key shows through again
    assert settings.effective(path)["keys"]["openai"] == "env-key"


def test_file_is_private_and_atomic(path):
    settings.update(path, {"provider": "google", "api_key": "g-key"})
    assert path.stat().st_mode & 0o777 == 0o600
    assert [p.name for p in path.parent.iterdir()] == ["settings.json"]  # no temp file left
    data = json.loads(path.read_text())
    assert data["provider"] == "google" and data["keys"]["google"] == "g-key" and data["keys"]["groq"] is None


def test_provider_change_resets_model_and_key_goes_to_new_provider(path):
    settings.update(path, {"model": "custom-groq-model", "api_key": "g1"})
    pub = settings.update(path, {"provider": "anthropic", "api_key": "a1"})
    assert pub["model"] == "claude-sonnet-5-5" and pub["keys"] == {
        "groq": True, "anthropic": True, "openai": False, "google": False, "compatible": False}
    assert "g1" not in json.dumps(pub) and "a1" not in json.dumps(pub)


@pytest.mark.parametrize("body", [{"provider": "x"}, {"base_url": "javascript:alert(1)"}, {"base_url": "http://"},
                                  {"firecrawl_url": "file:///x"}, {"model": 3}, {"api_key": "k" * 3000}])
def test_bad_input(path, body):
    with pytest.raises(ValueError):
        settings.update(path, body)
    assert not path.exists()


def test_unreadable_file_falls_back(path, monkeypatch):
    path.parent.mkdir(parents=True)
    path.write_text("{not json")
    monkeypatch.setenv("GROQ_API_KEY", "k")
    assert settings.effective(path)["keys"]["groq"] == "k"


def test_compatible_needs_base_url_and_model(path):
    cfg = settings.effective(path) | {"provider": "compatible", "model": None}
    assert not settings.ready(cfg)
    with pytest.raises(llm.NotConfigured, match="base URL"):
        llm.make(cfg)
    m = llm.make(cfg | {"base_url": "http://localhost:11434/v1", "model": "llama3.1"})
    assert type(m).__name__ == "ChatOpenAI" and m.openai_api_base == "http://localhost:11434/v1"


@pytest.mark.parametrize("provider,cls", [("groq", "ChatGroq"), ("anthropic", "ChatAnthropic"),
                                          ("openai", "ChatOpenAI"), ("google", "ChatGoogleGenerativeAI")])
def test_factory_builds_each_provider_without_calling_it(path, provider, cls):
    cfg = settings.effective(path) | {"provider": provider, "model": settings.BY_ID[provider]["default_model"]}
    with pytest.raises(llm.NotConfigured, match=settings.ENV_KEY[provider]):
        llm.make(cfg)
    m = llm.make(cfg | {"keys": {provider: "not-a-key"}}, timeout=20, max_retries=0)
    assert type(m).__name__ == cls and m.max_retries == 0


def test_missing_message_never_contains_a_key(path):
    cfg = settings.effective(path) | {"provider": "anthropic", "keys": {"groq": "SECRET-groq"}}
    with pytest.raises(llm.NotConfigured) as e:
        llm.make(cfg)
    assert "SECRET" not in str(e.value) and "Settings" in str(e.value)


def test_default_path_follows_library_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("LIBRARY_DIR", str(tmp_path))
    assert settings.default_path() == tmp_path.resolve() / "settings.json"
