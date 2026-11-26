"""MDM Agent desktop app: the server on a free loopback port, a native window on it, and a tray icon.
See contracts/settings.md, "Desktop app".

Run from the repo: python desktop/app.py   (packaged by desktop/mdm-agent.spec)
The GUI libraries (pywebview, pystray) are imported in main() only, so the helpers here are testable without them.
"""
import os
import socket
import subprocess
import sys
import threading
import time
import urllib.request
from pathlib import Path

NAME = "MDM Agent"
PORT_FILE = ".desktop-port"
LOCK_FILE = ".desktop.lock"
LOG_FILE = ".desktop.log"
SHOW_PATH = "/desktop/show"  # a second launch POSTs here to bring the running window back


def library_dir() -> Path:
    """LIBRARY_DIR, else ~/MDM Library: the same default as agent/jobs.py."""
    return Path(os.environ.get("LIBRARY_DIR") or Path.home() / "MDM Library")


def bundle_dir() -> Path | None:
    """Where PyInstaller unpacked the app (static/, mdm/), or None when run from source."""
    return Path(sys._MEIPASS) if getattr(sys, "frozen", False) else None


def mdm_cli(base: Path, platform: str = sys.platform) -> Path:
    """The bundled MDM CLI; the app's program (misterdownloadmanager[.exe]) sits next to it."""
    return base / "mdm" / ("misterdownloadmanager-cli.exe" if platform == "win32" else "misterdownloadmanager-cli")


def bind_loopback() -> socket.socket:
    """A socket on 127.0.0.1 with a free port, handed to uvicorn as is (no pick-then-bind race)."""
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("127.0.0.1", 0))
    return s


def acquire_lock(path: Path):
    """An exclusive, non-blocking lock held for the app's life (the OS drops it if the app dies). None if taken."""
    path.parent.mkdir(parents=True, exist_ok=True)
    f = open(path, "a+")
    try:
        if os.name == "nt":
            import msvcrt

            f.seek(0)
            msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl

            fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        f.close()
        return None
    return f


def write_port(lib: Path, port: int) -> None:
    (lib / PORT_FILE).write_text(str(port))


def read_port(lib: Path) -> int | None:
    try:
        port = int((lib / PORT_FILE).read_text().strip())
    except (OSError, ValueError):
        return None
    return port if 0 < port < 65536 else None


def focus_running(lib: Path, wait_s: float = 15) -> bool:
    """Ask the running instance to show its window. It may still be starting, so the port file is retried."""
    end = time.monotonic() + wait_s
    while time.monotonic() < end:
        port = read_port(lib)
        if port:
            try:
                req = urllib.request.Request(f"http://127.0.0.1:{port}{SHOW_PATH}", method="POST")
                urllib.request.urlopen(req, timeout=2).close()
                return True
            except OSError:
                pass
        time.sleep(0.5)
    return False


def open_folder(path: Path) -> None:
    if sys.platform == "win32":
        os.startfile(path)  # noqa: S606 (our own folder)
    else:
        subprocess.Popen(["open" if sys.platform == "darwin" else "xdg-open", str(path)])


def tray_image():
    """A down arrow on a dark disc, drawn here so the build needs no icon file."""
    from PIL import Image, ImageDraw

    img = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.ellipse((2, 2, 62, 62), fill=(32, 32, 36, 255))
    d.rectangle((27, 14, 37, 34), fill="white")
    d.polygon([(16, 32), (48, 32), (32, 50)], fill="white")
    return img


def _prepare_env(lib: Path) -> None:
    os.environ["LIBRARY_DIR"] = str(lib)
    base = bundle_dir()
    if base is not None:
        cli = mdm_cli(base)
        if cli.is_file():
            os.environ.setdefault("MDM_CLI", str(cli))
        # a log, since a windowed app has no terminal (and on Windows no stdout at all: uvicorn needs one)
        log = open(lib / LOG_FILE, "w", buffering=1, encoding="utf-8")
        sys.stdout = sys.stderr = log
    if sys.platform == "darwin":  # Finder starts apps with a bare PATH: find Homebrew's ffmpeg for posters
        extra = [p for p in ("/opt/homebrew/bin", "/usr/local/bin") if p not in os.environ.get("PATH", "").split(":")]
        os.environ["PATH"] = ":".join([os.environ.get("PATH", "/usr/bin:/bin:/usr/sbin:/sbin"), *extra])


def _mac_hooks(show, terminating) -> None:
    """macOS app delegate hooks, added to pywebview's delegate class:
    - reopen (dock click, second double-click: LaunchServices reactivates this process instead of starting
      another) shows the window;
    - terminate (Cmd+Q, the Dock's Quit, logout) really quits: otherwise the closing handler would only hide."""
    try:
        import objc
        from webview.platforms import cocoa

        def applicationShouldHandleReopen_hasVisibleWindows_(self, app, visible):
            show()
            return True

        def applicationShouldTerminate_(self, app):
            terminating()
            return 1  # NSTerminateNow

        objc.classAddMethods(cocoa.BrowserView.AppDelegate, [
            objc.selector(applicationShouldHandleReopen_hasVisibleWindows_,
                          selector=b"applicationShouldHandleReopen:hasVisibleWindows:", signature=b"Z@:@Z"),
            objc.selector(applicationShouldTerminate_, selector=b"applicationShouldTerminate:", signature=b"I@:@"),  # as pywebview declares it
        ])
    except Exception as e:  # the menu bar icon still opens the window and quits
        print(f"macOS app hooks not installed: {e}")


def main() -> int:
    lib = library_dir()
    lock = acquire_lock(lib / LOCK_FILE)
    if lock is None:
        return 0 if focus_running(lib) else 1
    _prepare_env(lib)
    if bundle_dir() is None:  # from source: the repo root holds server.py and agent/
        sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

    import pystray
    import uvicorn
    import webview

    import server

    state = {"quitting": False, "window": None}

    def show():
        w = state["window"]
        if w is not None:
            w.show()
            w.restore()

    server.app.add_api_route(SHOW_PATH, lambda: show() or {"ok": True}, methods=["POST"])

    sock = bind_loopback()
    port = sock.getsockname()[1]
    srv = uvicorn.Server(uvicorn.Config(server.app, log_level="info", timeout_graceful_shutdown=1))  # /api/events never ends by itself
    thread = threading.Thread(target=srv.run, kwargs={"sockets": [sock]}, daemon=True)
    thread.start()
    for _ in range(300):  # first start imports the agent stack: give it up to 30 s
        if srv.started or not thread.is_alive():
            break
        time.sleep(0.1)
    if not srv.started:
        print("server did not start")
        return 1
    write_port(lib, port)
    print(f"{NAME} on http://127.0.0.1:{port}/ (library {lib})")

    webview.settings["ALLOW_DOWNLOADS"] = True
    window = webview.create_window(NAME, f"http://127.0.0.1:{port}/", width=1200, height=820, min_size=(720, 520))
    state["window"] = window

    def on_closing():
        if state["quitting"]:
            return True
        window.hide()
        return False  # closing hides; Quit is in the tray menu

    window.events.closing += on_closing

    def quit_app(*_):
        state["quitting"] = True
        tray.stop()
        window.destroy()

    tray = pystray.Icon("mdm-agent", tray_image(), NAME, menu=pystray.Menu(
        pystray.MenuItem("Open", lambda *_: show(), default=True),
        pystray.MenuItem("Open library folder", lambda *_: open_folder(lib)),
        pystray.MenuItem("Quit", quit_app),
    ))

    def cleanup():
        srv.should_exit = True
        try:
            if read_port(lib) == port:
                (lib / PORT_FILE).unlink()
        except OSError:
            pass

    def terminating():  # macOS ends the process itself right after this (exit(), not back through start())
        state["quitting"] = True
        tray.stop()
        cleanup()
        print("quit")

    if sys.platform == "darwin":
        # Cocoa: one main thread for both. pystray attaches its status item; pywebview runs the NSApplication loop.
        tray.run_detached(setup=lambda icon: None)
        tray.visible = True  # here, on the main thread (the default setup would do it from another thread)
        _mac_hooks(show, terminating)
    else:
        threading.Thread(target=tray.run, daemon=True).start()

    webview.start(private_mode=False, storage_path=str(lib / ".webview"))

    cleanup()
    thread.join(5)
    lock.close()
    print("quit")
    return 0


if __name__ == "__main__":
    sys.exit(main())
