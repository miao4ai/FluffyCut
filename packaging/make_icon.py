"""生成 .app 的图标：一把黄色剪刀落在深色圆角方块上，纯 PIL 画，不依赖字体。

用法：python packaging/make_icon.py  ->  packaging/FluffyCut.icns
"""

from __future__ import annotations

import math
import shutil
import subprocess
import sys
from pathlib import Path

from PIL import Image, ImageDraw

OUT = Path(__file__).resolve().parent
BG = (22, 24, 29, 255)
ACCENT = (255, 225, 0, 255)


def draw_icon(size: int = 1024) -> Image.Image:
    S = size
    img = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    # macOS 风格的圆角方块，四周留白
    pad = int(S * 0.08)
    d.rounded_rectangle((pad, pad, S - pad, S - pad), radius=int(S * 0.22), fill=BG)

    cx, cy = S / 2, S * 0.57
    blade = S * 0.285
    ring = S * 0.085
    w = max(2, int(S * 0.028))

    # 两片刀刃交叉于中心偏上，下面是两个握环
    for sign in (-1, 1):
        tip = (cx + sign * blade * 0.62, cy - blade * 0.95)
        pivot = (cx, cy - blade * 0.05)
        hole = (cx - sign * blade * 0.42, cy + blade * 0.66)
        d.line([tip, pivot, hole], fill=ACCENT, width=w, joint="curve")
        d.ellipse(
            (hole[0] - ring, hole[1] - ring, hole[0] + ring, hole[1] + ring),
            outline=ACCENT, width=w,
        )
    # 铆钉
    r = S * 0.022
    d.ellipse((cx - r, cy - blade * 0.05 - r, cx + r, cy - blade * 0.05 + r), fill=BG,
              outline=ACCENT, width=max(2, int(w * 0.7)))

    # 顶部一条虚线，暗示"沿着这里剪"
    y = pad + S * 0.115
    dash, gap = S * 0.045, S * 0.035
    left, right = pad + S * 0.11, S - pad - S * 0.11
    x = left
    while x < right:
        d.line([(x, y), (min(x + dash, right), y)],
               fill=(255, 225, 0, 110), width=max(2, int(w * 0.55)))
        x += dash + gap
    return img


def main() -> int:
    base = draw_icon(1024)
    png = OUT / "icon.png"
    base.save(png)

    if not shutil.which("iconutil"):
        print(f"没有 iconutil（非 macOS？），只生成了 {png}")
        return 0

    iconset = OUT / "FluffyCut.iconset"
    shutil.rmtree(iconset, ignore_errors=True)
    iconset.mkdir()
    for size in (16, 32, 128, 256, 512):
        base.resize((size, size), Image.LANCZOS).save(iconset / f"icon_{size}x{size}.png")
        base.resize((size * 2, size * 2), Image.LANCZOS).save(iconset / f"icon_{size}x{size}@2x.png")
    subprocess.run(["iconutil", "-c", "icns", str(iconset), "-o", str(OUT / "FluffyCut.icns")],
                   check=True)
    shutil.rmtree(iconset, ignore_errors=True)
    print(f"图标已生成：{OUT / 'FluffyCut.icns'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
