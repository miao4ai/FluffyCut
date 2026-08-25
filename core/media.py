"""ffmpeg / ffprobe 的薄封装。"""

from __future__ import annotations

import functools
import json
import re
import shutil
import subprocess
import sys
import threading
from pathlib import Path
from typing import Callable, Iterable


class MediaError(RuntimeError):
    pass


# 双击启动的 .app 不继承终端的 PATH，homebrew 装的 ffmpeg 在这些地方
_EXTRA_BINS = ("/opt/homebrew/bin", "/usr/local/bin", "/usr/bin", "/opt/local/bin")


@functools.lru_cache(maxsize=8)
def tool(name: str) -> str | None:
    """找 ffmpeg / ffprobe 的绝对路径。找不到返回 None。

    顺序：打包进 .app 的副本 -> PATH -> homebrew 等常见位置。
    """
    bundled = Path(getattr(sys, "_MEIPASS", "")) / "bin" / name if hasattr(sys, "_MEIPASS") else None
    if bundled and bundled.exists():
        return str(bundled)
    found = shutil.which(name)
    if found:
        return found
    for d in _EXTRA_BINS:
        p = Path(d) / name
        if p.exists():
            return str(p)
    return None


def have(name: str) -> bool:
    return tool(name) is not None


def require_ffmpeg() -> None:
    missing = [t for t in ("ffmpeg", "ffprobe") if not have(t)]
    if missing:
        raise MediaError(
            f"找不到 {', '.join(missing)}。装一下：brew install ffmpeg"
            "（装完重开一次 FluffyCut）"
        )


def probe(path: str | Path) -> dict:
    exe = tool("ffprobe")
    if not exe:
        raise MediaError("找不到 ffprobe，装一下：brew install ffmpeg")
    out = subprocess.run(
        [exe, "-v", "error", "-print_format", "json", "-show_format", "-show_streams", str(path)],
        capture_output=True, text=True,
    )
    if out.returncode != 0:
        raise MediaError(f"ffprobe 读不了 {path}：{out.stderr.strip()}")
    return json.loads(out.stdout or "{}")


def duration(path: str | Path) -> float:
    """媒体时长（秒）。读不到返回 0。"""
    try:
        info = probe(path)
    except MediaError:
        return 0.0
    fmt = info.get("format", {})
    if fmt.get("duration"):
        return round(float(fmt["duration"]), 3)
    for s in info.get("streams", []):
        if s.get("duration"):
            return round(float(s["duration"]), 3)
    return 0.0


def has_libass() -> bool:
    """本机 ffmpeg 能不能烧 ASS 字幕。"""
    exe = tool("ffmpeg")
    if not exe:
        return False
    out = subprocess.run([exe, "-hide_banner", "-filters"], capture_output=True, text=True)
    return bool(re.search(r"^\s*\S+\s+subtitles\s", out.stdout, re.M))


ProgressFn = Callable[[float, str], None]


def run(cmd: Iterable[str], total_seconds: float = 0.0, on_progress: ProgressFn | None = None) -> None:
    """跑 ffmpeg，把 -progress 的输出翻译成 0..1 进度。失败时抛出带日志尾巴的异常。"""
    cmd = [str(c) for c in cmd]
    if cmd and cmd[0] in ("ffmpeg", "ffprobe"):
        cmd[0] = tool(cmd[0]) or cmd[0]
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, bufsize=1)
    tail: list[str] = []

    def drain_stderr() -> None:
        assert proc.stderr
        for line in proc.stderr:
            tail.append(line.rstrip())
            del tail[:-40]

    t = threading.Thread(target=drain_stderr, daemon=True)
    t.start()

    assert proc.stdout
    for line in proc.stdout:
        line = line.strip()
        if on_progress and line.startswith("out_time_us=") and total_seconds > 0:
            try:
                secs = int(line.split("=", 1)[1]) / 1_000_000
            except ValueError:
                continue
            on_progress(min(0.999, max(0.0, secs / total_seconds)), f"{secs:.1f}s / {total_seconds:.1f}s")
    proc.wait()
    t.join(timeout=1)
    if proc.returncode != 0:
        raise MediaError("ffmpeg 失败：\n" + "\n".join(tail[-25:]))
    if on_progress:
        on_progress(1.0, "完成")
