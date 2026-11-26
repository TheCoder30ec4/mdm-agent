"""AI provider and web search settings (contracts/settings.md): $LIBRARY_DIR/settings.json, mode 600.

Precedence for each value: the settings file, then the environment, then the defaults. Key values never
leave this module through `public()`; only whether each key is set.
"""
import json
import logging
import os
import tempfile
from pathlib import Path
from urllib.parse import urlsplit

log = logging.getLogger(__name__)

PROVIDERS = [
    {"id": "groq", "label": "Groq", "needs_key": True, "needs_base_url": False, "default_model": "openai/gpt-oss-120b"},
    {"id": "anthropic", "label": "Anthropic", "needs_key": True, "needs_base_url": False, "default_model": "claude-sonnet-5-5"},
    {"id": "openai", "label": "OpenAI", "needs_key": True, "needs_base_url": False, "default_model": "gpt-5.1"},
    {"id": "google", "label": "Google Gemini", "needs_key": True, "needs_base_url": False, "default_model": "gemini-2.5-flash"},
    {"id": "compatible", "label": "OpenAI-compatible (Ollama, LM Studio, OpenRouter...)", "needs_key": False,
     "needs_base_url": True, "default_model": None},
]
BY_ID = {p["id"]: p for p in PROVIDERS}
ENV_KEY = {"groq": "GROQ_API_KEY", "anthropic": "ANTHROPIC_API_KEY", "openai": "OPENAI_API_KEY", "google": "GOOGLE_API_KEY"}
FIRECRAWL_URL = "http://localhost:3002"
MAX_LEN = 2000


def default_path() -> Path:
    """Where Jobs() keeps the library when nothing overrides it (same rule as agent/jobs.py)."""
    return Path(os.environ.get("LIBRARY_DIR") or Path.home() / "MDM Library").resolve() / "settings.json"


def read(path) -> dict:
    """The raw file, or {} when there is none (or it is unreadable)."""
    if path is None:
        return {}
    try:
        data = json.loads(Path(path).read_text())
        return data if isinstance(data, dict) else {}
    except FileNotFoundError:
        return {}
    except (OSError, ValueError) as e:
        log.warning("ignoring unreadable settings file %s: %s", path, type(e).__name__)
        return {}


def _write(path, data: dict) -> None:
    """Atomic: a temp file (mode 600) in the same folder, then os.replace."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".settings-", suffix=".tmp")  # mkstemp: mode 600
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(data, f, indent=1)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def _s(v):
    return v if isinstance(v, str) and v.strip() else None


def effective(path=None) -> dict:
    """Every value resolved: {provider, model, base_url, keys{...}, firecrawl_url, firecrawl_key}. Holds keys."""
    f = read(path)
    fkeys = f.get("keys") if isinstance(f.get("keys"), dict) else {}
    provider = _s(f.get("provider")) or _s(os.environ.get("MDM_PROVIDER")) or "groq"
    if provider not in BY_ID:
        provider = "groq"
    model = (_s(f.get("model")) or _s(os.environ.get("MDM_MODEL"))
             or (_s(os.environ.get("GROQ_MODEL")) if provider == "groq" else None)  # GROQ_MODEL is a Groq model
             or BY_ID[provider]["default_model"])
    keys = {p: _s(fkeys.get(p)) or (_s(os.environ.get(ENV_KEY[p])) if p in ENV_KEY else None) for p in BY_ID}
    return {
        "provider": provider,
        "model": model,
        "base_url": _s(f.get("base_url")) or _s(os.environ.get("MDM_BASE_URL")),
        "keys": keys,
        "firecrawl_url": _s(f.get("firecrawl_url")) or _s(os.environ.get("FIRECRAWL_API_URL")) or FIRECRAWL_URL,
        "firecrawl_key": _s(f.get("firecrawl_key")) or _s(os.environ.get("FIRECRAWL_API_KEY")),
    }


def ready(cfg: dict) -> bool:
    """The provider in effect has what it needs to make a call."""
    p = BY_ID[cfg["provider"]]
    if p["needs_base_url"]:
        return bool(cfg["base_url"] and cfg["model"])
    return bool(cfg["keys"].get(cfg["provider"]))


def public(path=None) -> dict:
    """GET /api/settings: values, but for keys only whether each one is set."""
    cfg = effective(path)
    return {
        "provider": cfg["provider"],
        "model": cfg["model"],
        "base_url": cfg["base_url"],
        "firecrawl_url": cfg["firecrawl_url"],
        "keys": {p: bool(k) for p, k in cfg["keys"].items()},
        "firecrawl_key": bool(cfg["firecrawl_key"]),
        "providers": PROVIDERS,
    }


def _url(v, what):
    if v in (None, ""):
        return None
    if not isinstance(v, str) or len(v) > MAX_LEN or urlsplit(v.strip()).scheme not in ("http", "https") \
            or not urlsplit(v.strip()).netloc:
        raise ValueError(f"{what} must be an http or https URL")
    return v.strip()


def update(path, body: dict) -> dict:
    """PUT /api/settings: merge a subset into the file. ValueError on bad input. Returns public()."""
    if path is None:
        raise RuntimeError("no library folder to keep settings in")
    for k, v in body.items():
        if (v is not None and not isinstance(v, str)) or (isinstance(v, str) and len(v) > MAX_LEN):
            raise ValueError(f"{k} must be a short string")
    data = read(path)
    data["keys"] = {p: _s((data.get("keys") or {}).get(p)) for p in BY_ID}
    if "provider" in body:
        if body["provider"] not in BY_ID:
            raise ValueError("unknown provider")
        if body["provider"] != effective(path)["provider"] and "model" not in body:
            data["model"] = None  # another provider's model name would not work: fall back to the default
        data["provider"] = body["provider"]
    if "model" in body:
        data["model"] = _s(body["model"]) and body["model"].strip()
    if "base_url" in body:
        data["base_url"] = _url(body["base_url"], "base_url")
    if "firecrawl_url" in body:
        data["firecrawl_url"] = _url(body["firecrawl_url"], "firecrawl_url")
    if "firecrawl_key" in body:
        data["firecrawl_key"] = _s(body["firecrawl_key"]) and body["firecrawl_key"].strip()
    if "api_key" in body:  # stored under the provider in effect after this update; "" clears it
        prov = data["provider"] if data.get("provider") in BY_ID else effective(path)["provider"]
        data["keys"][prov] = _s(body["api_key"]) and body["api_key"].strip()
    _write(path, data)
    return public(path)
