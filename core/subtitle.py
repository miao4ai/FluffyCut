"""ASS 字幕导出。

成片里的字幕是 PIL 预渲图层（见 layers.py），不依赖 libass。
这里额外导出一份 .ass，用途是：拿去 Premiere/达芬奇二次剪辑、做多语字幕、
或者在装了 libass 的 ffmpeg 上直接烧录。时间轴与成片完全一致。
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from . import layers, media
from .project import Project, ProjectError


def _ass_color(hex_color: str, default=(255, 255, 255, 255)) -> str:
    """#RRGGBB[AA] -> &HAABBGGRR（ASS 的 AA 是透明度，与 alpha 相反）。"""
    r, g, b, a = layers.parse_color(hex_color, default)
    return f"&H{255 - a:02X}{b:02X}{g:02X}{r:02X}"


def _ts(seconds: float) -> str:
    seconds = max(0.0, seconds)
    h = int(seconds // 3600)
    m = int(seconds % 3600 // 60)
    s = seconds % 60
    return f"{h:d}:{m:02d}:{s:05.2f}"


def to_ass(project: Project) -> str:
    style, video = project.style, project.video
    lay = layers.layout(style, video)
    font_family = style.font or "PingFang SC"
    size = lay["subtitle_size"]
    margin_v = lay["subtitle_bottom"]
    margin_h = int(video.width * (1 - lay["subtitle_width_ratio"]) / 2)

    head = f"""[Script Info]
; 由 FluffyCut 生成 —— 时间轴与 project.json 一致
ScriptType: v4.00+
PlayResX: {video.width}
PlayResY: {video.height}
WrapStyle: 2
ScaledBorderAndShadow: yes

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Default,{font_family},{size},{_ass_color(style.subtitle_color)},{_ass_color(style.subtitle_color)},{_ass_color(style.subtitle_stroke, (0, 0, 0, 255))},{_ass_color(style.subtitle_bg, (0, 0, 0, 153))},0,0,0,0,100,100,0,0,3,{lay["subtitle_stroke_width"] // 2},0,2,{margin_h},{margin_h},{margin_v},1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""
    lines = [head]
    font = None
    for clip, start, end in project.timeline():
        text = (clip.text or "").strip()
        if not text:
            continue
        if font is None:
            font = layers.fonts.load(style.font, size)
        max_w = int(video.width * lay["subtitle_width_ratio"]) - lay["subtitle_pad"] * 2
        wrapped = "\\N".join(layers.wrap(text, font, max_w, lay["subtitle_max_lines"]))
        lines.append(f"Dialogue: 0,{_ts(start)},{_ts(end)},Default,,0,0,0,,{wrapped}\n")
    return "".join(lines)


def write_ass(project: Project, path: str | Path) -> Path:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(to_ass(project), "utf-8")
    return path


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m core.subtitle", description="导出 ASS 字幕")
    ap.add_argument("project")
    ap.add_argument("-o", "--out", default=None)
    args = ap.parse_args(argv)
    try:
        project = Project.load(args.project)
    except ProjectError as e:
        print(f"错误：{e}", file=sys.stderr)
        return 2
    out = write_ass(project, args.out or (project.root / "subtitles.ass"))
    print(f"已导出：{out}")
    if not media.has_libass():
        print("提示：本机 ffmpeg 没编译 libass，这份 .ass 烧不进视频；"
              "成片字幕走的是 PIL 图层，不受影响。", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
