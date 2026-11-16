"""macOS notification. Text goes in as osascript argv items, never interpolated into script code."""
import subprocess
import sys

_SCRIPT = ["on run argv", "display notification (item 1 of argv) with title (item 2 of argv)", "end run"]


def notify(title: str, message: str) -> None:
    if sys.platform != "darwin":
        return
    try:
        args = ["osascript"]
        for line in _SCRIPT:
            args += ["-e", line]
        args += ["--", str(message)[:250], str(title)[:100]]
        subprocess.run(args, stdin=subprocess.DEVNULL, capture_output=True, timeout=5, check=False)
    except Exception:
        pass
