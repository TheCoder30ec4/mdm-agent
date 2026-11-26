import pytest


@pytest.fixture(autouse=True)
def _isolated_library(tmp_path, monkeypatch):
    """Tests never read the user's ~/MDM Library (settings.json, keys); each gets an empty one."""
    monkeypatch.setenv("LIBRARY_DIR", str(tmp_path / "library"))


@pytest.fixture(autouse=True)
def _firecrawl_engine(monkeypatch):
    """Web tests fake Firecrawl's client; never probe a real port or fall back to the live lite search.
    tests/test_weblite.py sets the engine itself."""
    from agent import web
    monkeypatch.setattr(web, "engine", lambda: "firecrawl")
