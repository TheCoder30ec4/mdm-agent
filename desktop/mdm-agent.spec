# PyInstaller spec for the desktop app: one-folder build, "MDM Agent.app" on macOS, "MDM Agent\" on Windows.
#   MDM_BIN_DIR=<folder with misterdownloadmanager-cli and misterdownloadmanager[.exe]> \
#     pyinstaller --noconfirm --distpath <out>/dist --workpath <out>/build desktop/mdm-agent.spec
# See contracts/settings.md, "Desktop app".
import importlib.util
import os
import sys
from pathlib import Path

from PyInstaller.utils.hooks import collect_all, collect_submodules

ROOT = Path(SPECPATH).parent
sys.path.insert(0, str(ROOT))  # so collect_submodules("agent") finds the repo's package
EXE_SUFFIX = ".exe" if sys.platform == "win32" else ""

mdm_dir = ROOT / os.environ.get("MDM_BIN_DIR", "vendor/mdm")  # relative to the repo root (an absolute path wins)
mdm = [mdm_dir / f"misterdownloadmanager-cli{EXE_SUFFIX}", mdm_dir / f"misterdownloadmanager{EXE_SUFFIX}"]
missing = [str(p) for p in mdm if not p.is_file()]
if missing:
    raise SystemExit(f"MDM binaries not found: {missing} (set MDM_BIN_DIR)")

datas = [(str(ROOT / "static"), "static")]
binaries = [(str(p), "mdm") for p in mdm]  # desktop/app.py mdm_cli(): <bundle>/mdm/
hiddenimports = collect_submodules("agent") + collect_submodules("uvicorn")

# The agent stack imports lazily and ships data files (prompts, encodings); optional providers only if installed.
for pkg in ("deepagents", "langchain", "langchain_core", "langgraph", "langchain_groq", "langchain_anthropic",
            "langchain_openai", "langchain_google_genai", "tiktoken", "tiktoken_ext", "firecrawl", "webview", "ddgs", "primp", "lxml"):
    if importlib.util.find_spec(pkg) is not None:
        d, b, h = collect_all(pkg)
        datas += d
        binaries += b
        hiddenimports += h

a = Analysis(
    [str(ROOT / "desktop" / "app.py")],
    pathex=[str(ROOT)],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    excludes=["tkinter", "pytest"],
)
pyz = PYZ(a.pure)
exe = EXE(pyz, a.scripts, [], exclude_binaries=True, name="MDM Agent", console=False)
coll = COLLECT(exe, a.binaries, a.datas, name="MDM Agent")

if sys.platform == "darwin":
    app = BUNDLE(
        coll,
        name="MDM Agent.app",
        bundle_identifier="dev.mdmagent.desktop",
        info_plist={"NSHighResolutionCapable": True, "LSMinimumSystemVersion": "12.0"},
    )
