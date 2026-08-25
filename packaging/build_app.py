"""一步打出 FluffyCut.app。

    python packaging/build_app.py            # 产物在 dist/FluffyCut.app
    FLUFFYCUT_BUNDLE_FFMPEG=1 python ...     # 把 ffmpeg 也塞进去
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def run(cmd: list[str]) -> None:
    print("$", " ".join(cmd))
    subprocess.run(cmd, check=True, cwd=ROOT)


def main() -> int:
    if sys.platform != "darwin":
        print("这个打包脚本只处理 macOS 的 .app。", file=sys.stderr)
        return 2
    try:
        import PyInstaller  # noqa: F401
    except ImportError:
        print("先装打包工具：pip install pyinstaller pywebview", file=sys.stderr)
        return 2

    run([sys.executable, "packaging/make_icon.py"])
    shutil.rmtree(ROOT / "build", ignore_errors=True)
    shutil.rmtree(ROOT / "dist" / "FluffyCut.app", ignore_errors=True)
    run([sys.executable, "-m", "PyInstaller", "--noconfirm", "--clean",
         "packaging/FluffyCut.spec"])

    app = ROOT / "dist" / "FluffyCut.app"
    if not app.exists():
        print("打包没产出 .app，看看上面的日志。", file=sys.stderr)
        return 1
    size = sum(f.stat().st_size for f in app.rglob("*") if f.is_file()) / 1e6
    print(f"\n打好了：{app}  （{size:.0f} MB）")
    print("拖进「应用程序」就能双击用。工程默认存在 ~/Movies/FluffyCut。")
    print("首次打开如果被 Gatekeeper 拦下（未签名），右键 -> 打开。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
