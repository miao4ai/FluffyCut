"""占位配图生成。

本工具不下载任何第三方素材（见 README）。没配图的句子先用程序生成的渐变占位图顶上，
时间轴就能先跑起来，之后再换成自己的素材。同一个 seed 出同一张图。
"""

from __future__ import annotations

import hashlib
import math
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter

from . import fonts

PALETTES = [
    ((18, 32, 66), (86, 44, 120)),
    ((10, 46, 54), (18, 104, 96)),
    ((52, 20, 32), (140, 58, 44)),
    ((22, 24, 32), (70, 76, 96)),
    ((44, 30, 12), (150, 106, 30)),
    ((16, 36, 28), (44, 96, 60)),
]


def _seed(text: str) -> int:
    return int.from_bytes(hashlib.sha1(text.encode("utf-8")).digest()[:4], "big")


def make(path: str | Path, label: str = "", seed_text: str | None = None,
         size: tuple[int, int] = (1080, 1920)) -> Path:
    """生成一张确定性的渐变占位图。label 会淡淡地印在中间。"""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    s = _seed(seed_text if seed_text is not None else (label or str(path)))
    c0, c1 = PALETTES[s % len(PALETTES)]
    angle = (s >> 8) % 360

    W, H = size
    small = Image.new("RGB", (64, 114))
    px = small.load()
    dx, dy = math.cos(math.radians(angle)), math.sin(math.radians(angle))
    for y in range(small.height):
        for x in range(small.width):
            t = ((x / small.width) * dx + (y / small.height) * dy + 1) / 2
            t = min(1.0, max(0.0, t))
            px[x, y] = tuple(int(a + (b - a) * t) for a, b in zip(c0, c1))
    img = small.resize((W, H), Image.BICUBIC)

    # 几个虚化的光斑，避免纯渐变太平
    blobs = Image.new("RGB", (W, H), (0, 0, 0))
    bd = ImageDraw.Draw(blobs)
    for i in range(3):
        r = (s >> (i * 5)) % (W // 3) + W // 6
        cx = ((s >> (i * 7)) % W)
        cy = ((s >> (i * 3)) % H)
        bd.ellipse((cx - r, cy - r, cx + r, cy + r), fill=(28 + i * 10, 28 + i * 8, 34 + i * 12))
    img = Image.blend(img, blobs.filter(ImageFilter.GaussianBlur(W // 8)), 0.35)

    if label:
        from .layers import wrap  # 复用同一套 CJK 换行

        d = ImageDraw.Draw(img, "RGBA")
        font = fonts.load(None, int(H * 0.028))
        lines = wrap(label, font, int(W * 0.7), max_lines=3)
        lh = int(font.getmetrics()[0] * 1.6)
        y = H // 2 - lh * (len(lines) - 1) // 2
        for line in lines:
            d.text((W // 2, y), line, font=font, fill=(255, 255, 255, 52), anchor="mm")
            y += lh

    img.save(path)
    return path


def make_avatar(path: str | Path, text: str = "拒", size: int = 400) -> Path:
    """一个纯色圆底 + 单字的默认头像。"""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    ss = 4
    img = Image.new("RGBA", (size * ss, size * ss), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.ellipse((0, 0, size * ss - 1, size * ss - 1), fill=(255, 225, 0, 255))
    font = fonts.load(None, int(size * ss * 0.52))
    d.text((size * ss // 2, size * ss // 2), text[:1], font=font, fill=(20, 20, 20, 255), anchor="mm")
    img.resize((size, size), Image.LANCZOS).save(path)
    return path
