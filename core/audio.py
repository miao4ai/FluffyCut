"""音频加工：从视频抽音轨之外的活儿，目前主要是去人声。

场景很具体：从参考片里抽出来的音轨常常是「配乐 + 解说」，你只想要配乐。

两条路：
    center  中置抵消（L−R）。只用 ffmpeg，秒出。居中的人声会被完全消掉 ——
            实测残余 −91 dB，等于没有。代价是同样居中的乐器（贝斯、底鼓）
            也一起没了；单声道素材直接不适用（减完是一片静音）。
    demucs  真正的音源分离，质量好得多，但要装 torch（好几个 G），可选。

默认 auto：装了 demucs 就用它，否则退回 center。
"""

from __future__ import annotations

import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Callable

from . import media

SILENT_DB = -60.0        # 处理完低于这个响度，基本可以断定是把整段减没了


def mean_volume(path: str | Path) -> float:
    """整段的平均响度（dBFS）。读不出来返回 -inf。"""
    exe = media.tool("ffmpeg")
    if not exe:
        raise media.MediaError("找不到 ffmpeg")
    r = subprocess.run([exe, "-hide_banner", "-nostdin", "-i", str(path),
                        "-af", "volumedetect", "-f", "null", "-"],
                       capture_output=True, text=True)
    m = re.search(r"mean_volume:\s*(-?[0-9.]+) dB", r.stderr)
    return float(m.group(1)) if m else float("-inf")


def channels(path: str | Path) -> int:
    try:
        info = media.probe(path)
    except media.MediaError:
        return 0
    for st in info.get("streams", []):
        if st.get("codec_type") == "audio":
            return int(st.get("channels") or 0)
    return 0


def has_demucs() -> bool:
    if shutil.which("demucs"):
        return True
    try:
        import demucs  # noqa: F401
        return True
    except ImportError:
        return False


def vocal_remover(method: str = "auto") -> str:
    if method != "auto":
        return method
    return "demucs" if has_demucs() else "center"


def _center_chain(keep_bass: bool) -> str:
    """中置抵消。keep_bass 会把低频从原混音里补回来 —— 低音回来了，
    但人声的低频也跟着回来一点（实测残余从 −91 dB 升到 −40 dB）。"""
    side = "pan=1c|c0=0.5*c0-0.5*c1,aformat=channel_layouts=stereo"
    if not keep_bass:
        return f"[0:a]{side}[out]"
    return (f"[0:a]asplit=2[a][b];[a]{side}[side];"
            f"[b]lowpass=f=120,volume=0.7[bass];"
            f"[side][bass]amix=inputs=2:normalize=0,alimiter=limit=0.95[out]")


def remove_vocals(src: str | Path, dest: str | Path, method: str = "auto",
                  keep_bass: bool = False,
                  on_progress: Callable[[float, str], None] | None = None) -> Path:
    """把人声从一段音频里去掉，结果写到 dest（m4a）。"""
    src, dest = Path(src), Path(dest)
    if not src.exists():
        raise media.MediaError(f"音频不存在：{src}")
    dest.parent.mkdir(parents=True, exist_ok=True)
    chosen = vocal_remover(method)

    if chosen == "demucs":
        return _demucs(src, dest, on_progress)
    if channels(src) < 2:
        raise media.MediaError(
            "这段音频是单声道，中置抵消会把整段变成静音。"
            "要处理单声道得装真正的分离模型：pip install demucs"
        )
    return _center(src, dest, keep_bass, on_progress)


def _center(src: Path, dest: Path, keep_bass: bool,
            on_progress: Callable[[float, str], None] | None) -> Path:
    exe = media.tool("ffmpeg")
    if not exe:
        raise media.MediaError("找不到 ffmpeg")
    if on_progress:
        on_progress(0.2, "中置抵消…")
    with tempfile.TemporaryDirectory() as tmp:
        staged = Path(tmp) / "out.m4a"
        media.run([exe, "-hide_banner", "-v", "error", "-nostdin", "-y", "-i", str(src),
                   "-filter_complex", _center_chain(keep_bass), "-map", "[out]",
                   "-c:a", "aac", "-b:a", "192k", "-ar", "44100", str(staged)])
        level = mean_volume(staged)
        if level < SILENT_DB:
            # 两个声道内容一样（伪立体声）时，相减就什么都不剩了
            raise media.MediaError(
                f"减完几乎是静音（{level:.0f} dB）—— 这段音频左右声道基本一样，"
                "中置抵消对它没用。要处理这种素材得装 demucs。"
            )
        if on_progress:
            on_progress(0.95, "写文件…")
        shutil.copy2(staged, dest)
    return dest


def _demucs(src: Path, dest: Path,
            on_progress: Callable[[float, str], None] | None) -> Path:
    """demucs 的两轨模式，只要伴奏那一轨。慢，但质量是另一个档次。"""
    if on_progress:
        on_progress(0.1, "demucs 分离中，这一步很慢…")
    with tempfile.TemporaryDirectory() as tmp:
        cmd = ([shutil.which("demucs")] if shutil.which("demucs")
               else [sys.executable, "-m", "demucs"])
        r = subprocess.run([*cmd, "--two-stems=vocals", "-o", tmp, str(src)],
                           capture_output=True, text=True)
        if r.returncode != 0:
            raise media.MediaError(f"demucs 失败：{(r.stderr or '').strip()[-400:]}")
        found = list(Path(tmp).rglob("no_vocals.*"))
        if not found:
            raise media.MediaError("demucs 没吐出伴奏轨")
        if on_progress:
            on_progress(0.9, "转成 m4a…")
        exe = media.tool("ffmpeg")
        media.run([exe, "-hide_banner", "-v", "error", "-nostdin", "-y", "-i", str(found[0]),
                   "-c:a", "aac", "-b:a", "192k", "-ar", "44100", "-ac", "2", str(dest)])
    return dest
