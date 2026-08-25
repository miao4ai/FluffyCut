# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller 配置：打成 macOS 的 FluffyCut.app。

    python packaging/build_app.py        # 推荐，会先生成图标
    pyinstaller packaging/FluffyCut.spec # 或者直接跑这个

想把 ffmpeg 一起塞进 .app（用户就不用自己 brew install），设个环境变量再打：
    FLUFFYCUT_BUNDLE_FFMPEG=1 python packaging/build_app.py
注意 ffmpeg 是 GPL，随包分发要遵守它的许可。
"""

import os
import shutil
from pathlib import Path

ROOT = Path(SPECPATH).parent          # noqa: F821 —— SPECPATH 由 PyInstaller 注入
VERSION = "0.2.0"

datas = [
    (str(ROOT / "server" / "static"), "server/static"),
    (str(ROOT / "projects" / "demo"), "projects/demo"),
]

binaries = []
if os.environ.get("FLUFFYCUT_BUNDLE_FFMPEG"):
    for name in ("ffmpeg", "ffprobe"):
        found = shutil.which(name)
        if found:
            binaries.append((found, "bin"))

hiddenimports = [
    # uvicorn 这些是运行时动态 import 的，不写进来打包后会找不到
    "uvicorn.logging", "uvicorn.loops.auto", "uvicorn.loops.asyncio",
    "uvicorn.protocols.http.auto", "uvicorn.protocols.http.h11_impl",
    "uvicorn.protocols.websockets.auto", "uvicorn.protocols.websockets.websockets_impl",
    "uvicorn.lifespan.on", "uvicorn.lifespan.off",
    "webview.platforms.cocoa",
]
try:
    import anthropic  # noqa: F401
    hiddenimports.append("anthropic")
except ImportError:
    pass

a = Analysis(
    [str(ROOT / "app.py")],
    pathex=[str(ROOT)],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    # 语音转写的 ML 栈不打进来：mlx/numba/scipy 那一坨会把 .app 从 100 MB 撑到 1.5 GB。
    # 打包版仍然能拆节奏（那部分只用 ffmpeg），要转台词就装 whisper-cli：
    #     brew install whisper-cpp
    # core.analyze 会自动认出它。从源码跑的话 pip install mlx-whisper 也一样。
    excludes=[
        "tkinter", "matplotlib", "pytest", "PyInstaller", "numpy",
        "mlx", "mlx_whisper", "faster_whisper", "whisper", "torch",
        "numba", "llvmlite", "scipy", "transformers", "huggingface_hub", "tiktoken",
    ],
    noarchive=False,
)
pyz = PYZ(a.pure)                     # noqa: F821

exe = EXE(                            # noqa: F821
    pyz, a.scripts, [],
    exclude_binaries=True,
    name="FluffyCut",
    console=False,
    argv_emulation=False,
    target_arch=None,
)
coll = COLLECT(                       # noqa: F821
    exe, a.binaries, a.datas,
    strip=False, upx=False, name="FluffyCut",
)
app = BUNDLE(                         # noqa: F821
    coll,
    name="FluffyCut.app",
    icon=str(ROOT / "packaging" / "FluffyCut.icns"),
    bundle_identifier="dev.fluffycut.app",
    version=VERSION,
    info_plist={
        "CFBundleName": "FluffyCut",
        "CFBundleDisplayName": "FluffyCut",
        "CFBundleShortVersionString": VERSION,
        "NSHighResolutionCapable": True,
        "LSMinimumSystemVersion": "11.0",
        # 界面跑在 127.0.0.1 上，WKWebView 默认拦 http，这里放行本机
        "NSAppTransportSecurity": {
            "NSAllowsLocalNetworking": True,
            "NSExceptionDomains": {
                "127.0.0.1": {
                    "NSExceptionAllowsInsecureHTTPLoads": True,
                    "NSIncludesSubdomains": True,
                }
            },
        },
    },
)
