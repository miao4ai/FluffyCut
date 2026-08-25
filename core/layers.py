"""模板层与字幕层：用 PIL 预渲带 alpha 的 PNG，再交给 ffmpeg overlay。

为什么不用 ffmpeg drawtext / ASS：
  - drawtext 对中文换行、描边、圆角底框基本没法控；
  - 本机 ffmpeg 未编译 libass（`ffmpeg -filters | grep subtitles` 为空），ASS 烧不进去。
所以排版全在 PIL 做完，ffmpeg 只负责贴图。好处是界面预览和成片走同一段代码，
所见即所得是结构保证，不是靠对参数。
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

from PIL import Image, ImageDraw, ImageFont

from . import fonts
from .project import Clip, Project, Style, Video, Visual

RGBA = tuple[int, int, int, int]

# 布局默认值，按 1080x1920 设计，其他分辨率按高度线性缩放
DEFAULT_LAYOUT: dict[str, Any] = {
    "margin": 44,
    "title_size": 60,
    "title_top": 60,
    "title_pad": 28,
    "title_radius": 28,
    "title_max_lines": 3,
    "avatar_size": 128,
    "avatar_ring": 5,
    "subtitle_size": 64,
    "subtitle_bottom": 300,      # 字幕底框距画面底部
    "subtitle_pad": 26,
    "subtitle_radius": 20,
    "subtitle_stroke_width": 7,
    "subtitle_line_gap": 1.28,
    "subtitle_max_lines": 4,
    "subtitle_width_ratio": 0.90,
    "brand_size": 38,
    "brand_bottom": 96,
    "brand_pad": 22,
}


def parse_color(value: str | None, default: RGBA = (0, 0, 0, 255)) -> RGBA:
    """#RGB / #RRGGBB / #RRGGBBAA -> (r,g,b,a)。"""
    if not value:
        return default
    s = value.strip().lstrip("#")
    if len(s) == 3:
        s = "".join(ch * 2 for ch in s)
    if len(s) == 6:
        s += "ff"
    if len(s) != 8:
        return default
    try:
        r, g, b, a = (int(s[i : i + 2], 16) for i in (0, 2, 4, 6))
    except ValueError:
        return default
    return (r, g, b, a)


def layout(style: Style, video: Video) -> dict[str, Any]:
    """合并默认布局与 style.layout，并按画面高度等比缩放。"""
    lay = dict(DEFAULT_LAYOUT)
    lay.update(style.layout or {})
    k = video.height / 1920
    if abs(k - 1.0) > 1e-6:
        for key, val in lay.items():
            if isinstance(val, (int, float)) and not key.endswith("_ratio") and not key.endswith("_lines") and key != "subtitle_line_gap":
                lay[key] = type(val)(round(val * k))
    return lay


# ---------------------------------------------------------------- 文本排版


def _is_cjk(ch: str) -> bool:
    o = ord(ch)
    return (
        0x3000 <= o <= 0x303F or 0x3400 <= o <= 0x4DBF or 0x4E00 <= o <= 0x9FFF
        or 0xF900 <= o <= 0xFAFF or 0xFF00 <= o <= 0xFF60
    )


def _tokens(text: str) -> Iterable[str]:
    """按「中文逐字、西文逐词」切分，供换行使用。"""
    buf = ""
    for ch in text:
        if _is_cjk(ch) or ch.isspace():
            if buf:
                yield buf
                buf = ""
            yield ch
        else:
            buf += ch
    if buf:
        yield buf


def measure(font: ImageFont.FreeTypeFont, text: str) -> int:
    return int(font.getlength(text))


def wrap(text: str, font: ImageFont.FreeTypeFont, max_width: int, max_lines: int = 99) -> list[str]:
    """CJK 友好的自动换行。超出 max_lines 时在最后一行结尾加省略号。"""
    text = (text or "").strip()
    if not text:
        return []
    lines: list[str] = []
    cur = ""
    for tok in _tokens(text):
        if tok == "\n":
            lines.append(cur.rstrip())
            cur = ""
            continue
        cand = cur + tok
        if cur and measure(font, cand.rstrip()) > max_width:
            lines.append(cur.rstrip())
            cur = "" if tok.isspace() else tok
        else:
            cur = cand
    if cur.strip():
        lines.append(cur.rstrip())
    if len(lines) > max_lines:
        lines = lines[:max_lines]
        while lines[-1] and measure(font, lines[-1] + "…") > max_width:
            lines[-1] = lines[-1][:-1]
        lines[-1] += "…"
    return lines


def _line_height(font: ImageFont.FreeTypeFont, gap: float = 1.25) -> int:
    ascent, descent = font.getmetrics()
    return int((ascent + descent) * gap)


def _draw_lines(
    draw: ImageDraw.ImageDraw,
    lines: list[str],
    font: ImageFont.FreeTypeFont,
    center_x: int,
    top: int,
    fill: RGBA,
    gap: float = 1.25,
    stroke_width: int = 0,
    stroke_fill: RGBA | None = None,
) -> int:
    lh = _line_height(font, gap)
    y = top
    for line in lines:
        draw.text(
            (center_x, y),
            line,
            font=font,
            fill=fill,
            anchor="ma",
            stroke_width=stroke_width,
            stroke_fill=stroke_fill,
        )
        y += lh
    return y


# ---------------------------------------------------------------- 模板层


def _circle_avatar(path: Path, size: int, ring: int) -> Image.Image:
    src = Image.open(path).convert("RGBA")
    side = min(src.size)
    src = src.crop(
        ((src.width - side) // 2, (src.height - side) // 2,
         (src.width + side) // 2, (src.height + side) // 2)
    ).resize((size, size), Image.LANCZOS)

    ss = 4  # 超采样让圆边不锯齿
    mask = Image.new("L", (size * ss, size * ss), 0)
    ImageDraw.Draw(mask).ellipse((0, 0, size * ss - 1, size * ss - 1), fill=255)
    src.putalpha(mask.resize((size, size), Image.LANCZOS))

    if ring <= 0:
        return src
    out = Image.new("RGBA", (size + ring * 2, size + ring * 2), (0, 0, 0, 0))
    d = ImageDraw.Draw(out)
    d.ellipse((0, 0, out.width - 1, out.height - 1), fill=(255, 255, 255, 235))
    out.alpha_composite(src, (ring, ring))
    return out


def render_template(project: Project) -> Image.Image:
    """整片复用的固定图层：顶部标题带（含头像）+ 底部品牌带。"""
    style, video = project.style, project.video
    lay = layout(style, video)
    W, H = video.width, video.height
    img = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    margin = lay["margin"]

    # ---- 顶部标题带
    if project.title.strip():
        font = fonts.load(style.font, lay["title_size"])
        pad = lay["title_pad"]
        avatar_img = None
        avatar_path = project.resolve(style.avatar)
        if avatar_path and avatar_path.exists():
            avatar_img = _circle_avatar(avatar_path, lay["avatar_size"], lay["avatar_ring"])

        band_x0, band_x1 = margin, W - margin
        text_x0 = band_x0 + pad + (avatar_img.width + pad if avatar_img else 0)
        text_w = band_x1 - pad - text_x0
        lines = wrap(project.title, font, text_w, lay["title_max_lines"])
        lh = _line_height(font, 1.2)
        text_h = lh * max(1, len(lines))
        band_h = max(text_h + pad * 2, (avatar_img.height + pad * 2) if avatar_img else 0)
        band_y0 = lay["title_top"]
        band_y1 = band_y0 + band_h

        draw.rounded_rectangle(
            (band_x0, band_y0, band_x1, band_y1),
            radius=lay["title_radius"],
            fill=parse_color(style.title_bg, (0, 0, 0, 184)),
        )
        if avatar_img:
            img.alpha_composite(
                avatar_img, (band_x0 + pad, band_y0 + (band_h - avatar_img.height) // 2)
            )
        y = band_y0 + (band_h - text_h) // 2
        for line in lines:
            draw.text((text_x0, y), line, font=font, fill=parse_color(style.title_color, (255, 225, 0, 255)), anchor="la")
            y += lh

    # ---- 底部品牌带
    if style.brand_text.strip():
        font = fonts.load(style.font, lay["brand_size"])
        pad = lay["brand_pad"]
        tw = measure(font, style.brand_text)
        th = _line_height(font, 1.1)
        bw, bh = tw + pad * 2, th + int(pad * 0.7)
        x0 = (W - bw) // 2
        y1 = H - lay["brand_bottom"]
        y0 = y1 - bh
        draw.rounded_rectangle((x0, y0, x0 + bw, y1), radius=bh // 2,
                               fill=parse_color(style.brand_bg, (0, 0, 0, 160)))
        draw.text(((x0 + x0 + bw) // 2, (y0 + y1) // 2), style.brand_text, font=font,
                  fill=parse_color(style.brand_color, (255, 255, 255, 255)), anchor="mm")

    return img


# ---------------------------------------------------------------- 字幕层


def render_subtitle(project: Project, text: str) -> Image.Image:
    """一句台词一张全画幅透明 PNG。文本相同则内容相同 —— 渲染器据此去重。"""
    style, video = project.style, project.video
    lay = layout(style, video)
    W, H = video.width, video.height
    img = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    if not (text or "").strip():
        return img
    draw = ImageDraw.Draw(img)

    font = fonts.load(style.font, lay["subtitle_size"])
    max_w = int(W * lay["subtitle_width_ratio"]) - lay["subtitle_pad"] * 2
    lines = wrap(text, font, max_w, lay["subtitle_max_lines"])
    gap = lay["subtitle_line_gap"]
    lh = _line_height(font, gap)
    text_h = lh * len(lines)
    box_w = max(measure(font, ln) for ln in lines) + lay["subtitle_pad"] * 2
    box_h = text_h + int(lay["subtitle_pad"] * 1.2)
    x0 = (W - box_w) // 2
    y1 = H - lay["subtitle_bottom"]
    y0 = y1 - box_h

    bg = parse_color(style.subtitle_bg, (0, 0, 0, 153))
    if bg[3]:
        draw.rounded_rectangle((x0, y0, x0 + box_w, y1), radius=lay["subtitle_radius"], fill=bg)
    _draw_lines(
        draw, lines, font, W // 2, y0 + int(lay["subtitle_pad"] * 0.6),
        fill=parse_color(style.subtitle_color, (255, 255, 255, 255)),
        gap=gap,
        stroke_width=lay["subtitle_stroke_width"],
        stroke_fill=parse_color(style.subtitle_stroke, (0, 0, 0, 255)),
    )
    return img


# ---------------------------------------------------------------- 画面层


@dataclass
class KenBurns:
    """一段推近/拉远。zoom(t) 在这里定义，ffmpeg 与 PIL 预览共用同一公式。"""

    direction: str          # "in" | "out"
    amount: float           # 最大缩放增量，0.12 = 推到 112%

    def zoom_at(self, frame: int, total_frames: int) -> float:
        if total_frames <= 1:
            p = 0.0
        else:
            p = min(1.0, max(0.0, frame / (total_frames - 1)))
        return 1.0 + self.amount * (p if self.direction == "in" else 1.0 - p)


def kenburns_from(kenburns: bool | str, seed: str, amount: float = 0.12) -> KenBurns | None:
    """kenburns 字段 -> 参数。True 时按 seed（片段 id）哈希定方向，同一工程每次一致。"""
    if not kenburns:
        return None
    if isinstance(kenburns, str):
        direction = kenburns.lower()
        if direction not in ("in", "out"):
            direction = "in"
    else:
        h = hashlib.sha1(seed.encode("utf-8")).digest()[0]
        direction = "in" if h % 2 == 0 else "out"
    return KenBurns(direction, amount)


def kenburns_for(clip: Clip, amount: float = 0.12) -> KenBurns | None:
    return kenburns_from(clip.kenburns, clip.id, amount)


def _crop(src: Image.Image, shot: Visual) -> Image.Image:
    """和渲染器 _crop_chain 同一套比例，预览才对得上。"""
    if not any(shot.crop):
        return src
    top, right, bottom, left = shot.crop
    x0, y0 = int(src.width * left), int(src.height * top)
    x1, y1 = int(src.width * (1 - right)), int(src.height * (1 - bottom))
    if x1 - x0 < 2 or y1 - y0 < 2:
        return src
    return src.crop((x0, y0, x1, y1))


def _fit(src: Image.Image, size: tuple[int, int], mode: str, bg: RGBA) -> Image.Image:
    W, H = size
    canvas = Image.new("RGBA", size, bg)
    if mode == "contain":
        k = min(W / src.width, H / src.height)
        im = src.resize((max(1, int(src.width * k)), max(1, int(src.height * k))), Image.LANCZOS)
        canvas.alpha_composite(im, ((W - im.width) // 2, (H - im.height) // 2))
    else:  # cover：铺满后居中裁切
        k = max(W / src.width, H / src.height)
        im = src.resize((max(1, int(src.width * k)), max(1, int(src.height * k))), Image.LANCZOS)
        canvas.alpha_composite(im, ((W - im.width) // 2, (H - im.height) // 2))
    return canvas


def shot_at(project: Project, clip: Clip, t: float) -> tuple[Visual, float, float]:
    """本句第 t 秒落在哪个镜头上：(镜头, 镜头内的时间, 镜头时长)。"""
    spans = project.shots_of(clip)
    for shot, start, dur in spans:
        if start <= t < start + dur:
            return shot, t - start, dur
    shot, start, dur = spans[-1]
    return shot, max(0.0, min(t - start, dur)), dur


def render_visual(project: Project, clip: Clip, t: float) -> Image.Image:
    """clip 在片内第 t 秒的画面底层（不含模板/字幕）。预览用；渲染由 ffmpeg 走同样公式。"""
    video = project.video
    size = (video.width, video.height)
    bg = parse_color(video.bg, (0, 0, 0, 255))
    shot, local_t, shot_seconds = shot_at(project, clip, t)

    if shot.type == "image" and shot.path:
        path = project.resolve(shot.path)
        if path and path.exists():
            base = _fit(_crop(Image.open(path).convert("RGBA"), shot), size, shot.fit, bg)
        else:
            base = Image.new("RGBA", size, parse_color(shot.color, (16, 16, 20, 255)))
    elif shot.type == "video" and shot.path:
        # 预览要看的是素材入点之后、按变速走的那一帧
        base = _extract_video_frame(project, shot, shot.src_in + local_t * shot.speed, size, bg)
    else:
        base = Image.new("RGBA", size, parse_color(shot.color, (16, 16, 20, 255)))

    kb = kenburns_for(clip)
    if kb:
        fps = video.fps
        total = max(1, int(round(shot_seconds * fps)))
        z = kb.zoom_at(int(round(local_t * fps)), total)
        cw, ch = int(base.width / z), int(base.height / z)
        x, y = (base.width - cw) // 2, (base.height - ch) // 2
        base = base.crop((x, y, x + cw, y + ch)).resize(size, Image.LANCZOS)
    return base


def _extract_video_frame(project: Project, shot: Visual, t: float, size, bg) -> Image.Image:
    """预览里给视频素材抽一帧。渲染路径不走这里。"""
    import io
    import subprocess

    path = project.resolve(shot.path)
    if not path or not path.exists():
        return Image.new("RGBA", size, bg)
    from . import media

    exe = media.tool("ffmpeg")
    if not exe:
        return Image.new("RGBA", size, bg)
    cmd = [exe, "-v", "error", "-ss", f"{max(0.0, t):.3f}", "-i", str(path),
           "-frames:v", "1", "-f", "image2pipe", "-vcodec", "png", "-"]
    try:
        out = subprocess.run(cmd, capture_output=True, timeout=30).stdout
        return _fit(_crop(Image.open(io.BytesIO(out)).convert("RGBA"), shot), size, shot.fit, bg)
    except Exception:
        return Image.new("RGBA", size, bg)


def compose_frame(project: Project, clip: Clip, t: float = 0.0,
                  template: Image.Image | None = None) -> Image.Image:
    """完整一帧 = 画面 + 模板 + 字幕。界面预览和 --frame 导出都用它。"""
    frame = render_visual(project, clip, t)
    frame.alpha_composite(template if template is not None else render_template(project))
    frame.alpha_composite(render_subtitle(project, clip.text))
    return frame.convert("RGB")
