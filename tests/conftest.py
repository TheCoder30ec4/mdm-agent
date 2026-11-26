import pytest


@pytest.fixture(autouse=True)
def _isolated_library(tmp_path, monkeypatch):
    """Tests never read the user's ~/MDM Library (settings.json, keys); each gets an empty one."""
    monkeypatch.setenv("LIBRARY_DIR", str(tmp_path / "library"))
