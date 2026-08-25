"""字体解析：把「PingFang SC」这种家族名解析成 PIL 能用的文件路径 + face index。

PIL 不认字体家族名，只认文件。macOS 的中文字体多为 .ttc 集合，需要 index。
解析结果带缓存，渲染上千帧也只查一次盘。
"""

from __future__ import annotations

import functools
import os
from pathlib import Path

from PIL import ImageFont

# 家族名 -> 候选 (路径, face index)。按优先级排列，取第一个存在的。
_FAMILIES: dict[str, list[tuple[str, int]]] = {
    "pingfang sc": [
        ("/System/Library/Fonts/PingFang.ttc", 2),
        ("/System/Library/Fonts/Hiragino Sans GB.ttc", 1),
        ("/System/Library/Fonts/STHeiti Medium.ttc", 0),
    ],
    "pingfang": [("/System/Library/Fonts/PingFang.ttc", 2)],
    "hiragino sans gb": [("/System/Library/Fonts/Hiragino Sans GB.ttc", 0)],
    "heiti sc": [("/System/Library/Fonts/STHeiti Medium.ttc", 0)],
    "songti sc": [("/System/Library/Fonts/Supplemental/Songti.ttc", 0)],
    "arial unicode ms": [("/Library/Fonts/Arial Unicode.ttf", 0)],
    "helvetica": [("/System/Library/Fonts/Helvetica.ttc", 0)],
}

# 家族名解析不出来时的兜底顺序（必须覆盖中文）
_FALLBACK: list[tuple[str, int]] = [
    ("/System/Library/Fonts/PingFang.ttc", 2),
    ("/System/Library/Fonts/Hiragino Sans GB.ttc", 1),
    ("/System/Library/Fonts/STHeiti Medium.ttc", 0),
    ("/Library/Fonts/Arial Unicode.ttf", 0),
    ("/System/Library/Fonts/Supplemental/Arial Unicode.ttf", 0),
    ("/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc", 0),
    ("/usr/share/fonts/truetype/noto/NotoSansCJK-Regular.ttc", 0),
]


class FontError(RuntimeError):
    pass


@functools.lru_cache(maxsize=64)
def resolve(family: str | None) -> tuple[str, int]:
    """家族名 -> (字体文件路径, face index)。"""
    candidates: list[tuple[str, int]] = []
    if family:
        # 直接给文件路径也支持
        if os.path.sep in family and Path(family).exists():
            return (family, 0)
        candidates += _FAMILIES.get(family.strip().lower(), [])
    candidates += _FALLBACK
    for path, index in candidates:
        if Path(path).exists():
            return (path, index)
    raise FontError(
        f"找不到可用字体（family={family!r}）。请在 style.font 里直接填字体文件路径。"
    )


@functools.lru_cache(maxsize=256)
def load(family: str | None, size: int) -> ImageFont.FreeTypeFont:
    path, index = resolve(family)
    try:
        return ImageFont.truetype(path, size=size, index=index)
    except OSError:
        # .ttc 里 index 不存在时退回 0
        return ImageFont.truetype(path, size=size, index=0)


def available() -> bool:
    try:
        resolve(None)
        return True
    except FontError:
        return False
