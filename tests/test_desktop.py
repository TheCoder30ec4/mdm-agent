"""desktop/app.py helpers: no GUI (pywebview and pystray are imported only in main())."""
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

from desktop import app


def test_bind_loopback_gives_a_free_loopback_port():
    a, b = app.bind_loopback(), app.bind_loopback()
    try:
        assert a.getsockname()[0] == "127.0.0.1"
        assert a.getsockname()[1] > 0 and a.getsockname()[1] != b.getsockname()[1]
    finally:
        a.close()
        b.close()


def test_lock_is_single_instance(tmp_path):
    first = app.acquire_lock(tmp_path / "lib" / app.LOCK_FILE)
    assert first is not None
    assert app.acquire_lock(tmp_path / "lib" / app.LOCK_FILE) is None
    first.close()
    again = app.acquire_lock(tmp_path / "lib" / app.LOCK_FILE)
    assert again is not None
    again.close()


def test_port_file(tmp_path):
    assert app.read_port(tmp_path) is None
    app.write_port(tmp_path, 51234)
    assert app.read_port(tmp_path) == 51234
    for bad in ("", "abc", "0", "70000"):
        (tmp_path / app.PORT_FILE).write_text(bad)
        assert app.read_port(tmp_path) is None


def test_mdm_cli_per_os():
    base = Path("/bundle")
    assert app.mdm_cli(base, "darwin") == base / "mdm" / "misterdownloadmanager-cli"
    assert app.mdm_cli(base, "win32") == base / "mdm" / "misterdownloadmanager-cli.exe"
    assert app.bundle_dir() is None  # not frozen under pytest: MDM_CLI is left to agent/mdm.py


def test_library_dir_default(monkeypatch, tmp_path):
    monkeypatch.delenv("LIBRARY_DIR", raising=False)
    assert app.library_dir() == Path.home() / "MDM Library"
    monkeypatch.setenv("LIBRARY_DIR", str(tmp_path))
    assert app.library_dir() == tmp_path


def test_focus_running(tmp_path):
    assert app.focus_running(tmp_path, wait_s=0.3) is False  # no port file: nobody to focus
    hits = []

    class H(BaseHTTPRequestHandler):
        def do_POST(self):
            hits.append(self.path)
            self.send_response(200)
            self.end_headers()

        def log_message(self, *a):
            pass

    srv = HTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        app.write_port(tmp_path, srv.server_address[1])
        assert app.focus_running(tmp_path, wait_s=5) is True
        assert hits == [app.SHOW_PATH]
    finally:
        srv.shutdown()
        srv.server_close()
