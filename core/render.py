"""渲染器：project.json -> 1080x1920 mp4。

这是整个工具的地基，一个纯函数：同一份 project.json + 同样的素材，每次输出逐帧一致。
界面、TTS、AI 都只是在改这份 json，不参与渲染。

管线（一次 ffmpeg 调用完成，不产生中间视频）：

  每个镜头:  素材 -> 入出点/变速 -> cover/contain 适配 -> Ken Burns(zoompan)
  每个片段:  镜头 concat -> 叠字幕 PNG（只在本句时长内显示）
  拼接:      无转场的相邻片段 concat 成段，段与段之间 xfade -> 叠模板 PNG -> yuv420p
  声音:      每句配音 apad/atrim 到该句时长（无配音则静音）-> concat -> 混背景音乐（可闪避）

时间轴不变量：成片总长 = 各句时长之和。转场吃的是上一句画面**额外多渲的那一截**，
不占时间轴，所以加不加转场，字幕和配音的位置一帧都不会动。
"""

from __future__ import annotations

import argparse
import hashlib
import shutil
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from . import layers, media
from .layers import kenburns_for
from .project import Clip, Project, ProjectError, Visual

AUDIO_RATE = 44100
KENBURNS_AMOUNT = 0.12
KENBURNS_OVERSCAN = 1.25   # Ken Burns 前先放大一点，推近时才不糊


@dataclass
class RenderOptions:
    crf: int = 18
    preset: str = "medium"
    audio_bitrate: str = "192k"
    keep_temp: bool = False
    dry_run: bool = False


def _hex_to_ff(color: str, default: str = "0x000000") -> str:
    r, g, b, _a = layers.parse_color(color, (0, 0, 0, 255))
    return f"0x{r:02X}{g:02X}{b:02X}"


def _fit_chain(fit: str, w: int, h: int, bg: str) -> str:
    if fit == "contain":
        return (f"scale=w={w}:h={h}:force_original_aspect_ratio=decrease:flags=lanczos,"
                f"pad={w}:{h}:(ow-iw)/2:(oh-ih)/2:color={_hex_to_ff(bg)}")
    return (f"scale=w={w}:h={h}:force_original_aspect_ratio=increase:flags=lanczos,"
            f"crop={w}:{h}")


def _zoompan_chain(seed: str, kenburns, seconds: float, w: int, h: int, fps: int) -> str:
    """Ken Burns。缩放曲线与 layers.KenBurns.zoom_at 完全一致，预览才对得上。"""
    kb = layers.kenburns_from(kenburns, seed, KENBURNS_AMOUNT)
    if kb is None:
        return ""
    frames = max(1, int(round(seconds * fps)))
    span = max(1, frames - 1)
    a = KENBURNS_AMOUNT
    z = f"1+{a}*on/{span}" if kb.direction == "in" else f"1+{a}-{a}*on/{span}"
    return (f",zoompan=z='{z}':d=1:x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)'"
            f":s={w}x{h}:fps={fps}")


def _subtitle_pngs(project: Project, workdir: Path) -> dict[str, Path]:
    """按台词文本去重生成字幕 PNG，返回 clip.id -> png 路径。"""
    cache: dict[str, Path] = {}
    result: dict[str, Path] = {}
    for clip in project.clips:
        text = clip.text or ""
        key = hashlib.sha1(text.encode("utf-8")).hexdigest()[:12]
        if key not in cache:
            png = workdir / f"sub_{key}.png"
            layers.render_subtitle(project, text).save(png)
            cache[key] = png
        result[clip.id] = cache[key]
    return result


def build_command(project: Project, out: Path, workdir: Path, opts: RenderOptions) -> list[str]:
    """把工程翻译成一条 ffmpeg 命令。单独拿出来是为了能直接肉眼检查 / 单测。"""
    v = project.video
    W, H, FPS = v.width, v.height, v.fps

    template_png = workdir / "template.png"
    layers.render_template(project).save(template_png)
    subs = _subtitle_pngs(project, workdir)

    inputs: list[str] = []
    filters: list[str] = []
    idx = 0

    def add_input(args: list[str]) -> int:
        nonlocal idx
        inputs.extend([str(a) for a in args])
        idx += 1
        return idx - 1

    kb_w, kb_h = int(W * KENBURNS_OVERSCAN), int(H * KENBURNS_OVERSCAN)
    seg_labels: list[str] = []      # 每句一个画面段
    alabels: list[str] = []

    for ci, clip in enumerate(project.clips):
        trans = project.transition_after(ci)
        tail = trans.duration if trans else 0.0     # 为转场多渲的那一截
        shots = clip.shots()
        shot_labels: list[str] = []

        for si, (shot, _start, dur) in enumerate(shots):
            # 转场只延长本句的最后一个镜头
            render_dur = dur + (tail if si == len(shots) - 1 else 0.0)
            kb = layers.kenburns_from(clip.kenburns, clip.id, KENBURNS_AMOUNT)
            tw, th = (kb_w, kb_h) if kb else (W, H)
            label = f"s{ci}_{si}"

            if shot.type == "image":
                i = add_input(["-loop", "1", "-framerate", FPS, "-t", f"{render_dur:.3f}",
                               "-i", project.resolve(shot.path)])
                chain = f"[{i}:v]{_fit_chain(shot.fit, tw, th, v.bg)},setsar=1,fps={FPS}"
            elif shot.type == "video":
                need_src = render_dur * shot.speed          # 需要读多少秒原始素材
                have = shot.src_out - shot.src_in if shot.src_out is not None else None
                read = min(need_src, have) if have is not None else need_src
                i = add_input(["-ss", f"{shot.src_in:.3f}", "-t", f"{read:.3f}",
                               "-i", project.resolve(shot.path)])
                chain = f"[{i}:v]{_fit_chain(shot.fit, tw, th, v.bg)},setsar=1"
                if shot.speed != 1.0:
                    chain += f",setpts=PTS/{shot.speed}"
                chain += f",fps={FPS}"
                if read / shot.speed < render_dur - 1e-3:
                    # 素材不够长：定格最后一帧补满，避免黑屏
                    chain += f",tpad=stop_mode=clone:stop_duration={render_dur - read / shot.speed:.3f}"
            else:
                color = _hex_to_ff(shot.color, "0x101014")
                i = add_input(["-f", "lavfi", "-t", f"{render_dur:.3f}",
                               "-i", f"color=c={color}:s={tw}x{th}:r={FPS}"])
                chain = f"[{i}:v]setsar=1,fps={FPS}"

            chain += _zoompan_chain(clip.id, clip.kenburns, render_dur, W, H, FPS)
            chain += f",trim=duration={render_dur:.3f},setpts=PTS-STARTPTS,format=rgb24[{label}]"
            filters.append(chain)
            shot_labels.append(f"[{label}]")

        # 多镜头先拼成这一句的画面
        pic = f"[pic{ci}]"
        if len(shot_labels) == 1:
            pic = shot_labels[0]
        else:
            filters.append("".join(shot_labels) + f"concat=n={len(shot_labels)}:v=1:a=0{pic}")

        # 字幕只在本句时长内显示：转场那一截是上一句画面的余韵，不该还挂着字
        s = add_input(["-loop", "1", "-framerate", FPS, "-i", subs[clip.id]])
        enable = f":enable='lt(t,{clip.seconds:.3f})'" if tail else ""
        # settb 统一时基：xfade 要求两路输入时基一致，各分支经过的滤镜不同会跑偏
        filters.append(
            f"{pic}[{s}:v]overlay=0:0:format=auto:shortest=1{enable},settb=1/{FPS}[v{ci}]"
        )
        seg_labels.append(f"[v{ci}]")

        # 声音：有配音就裁/补到该句时长，没有就补静音
        apath = project.resolve(clip.audio.path) if clip.audio and clip.audio.path else None
        dur = clip.seconds
        if apath and apath.exists():
            a = add_input(["-i", apath])
            gain = clip.audio.gain if clip.audio else 1.0
            vol = f",volume={gain:.3f}" if gain != 1.0 else ""
            filters.append(
                f"[{a}:a]aformat=sample_fmts=fltp:sample_rates={AUDIO_RATE}:channel_layouts=stereo,"
                f"apad,atrim=duration={dur:.3f},asetpts=PTS-STARTPTS{vol}[a{ci}]"
            )
        else:
            a = add_input(["-f", "lavfi", "-t", f"{dur:.3f}",
                           "-i", f"anullsrc=r={AUDIO_RATE}:cl=stereo"])
            filters.append(f"[{a}:a]asetpts=PTS-STARTPTS[a{ci}]")
        alabels.append(f"[a{ci}]")

    filters.append(_video_chain(project, seg_labels))
    filters.append("".join(alabels) + f"concat=n={len(alabels)}:v=0:a=1[voice]")

    t = add_input(["-loop", "1", "-framerate", FPS, "-i", template_png])
    filters.append(f"[vcat][{t}:v]overlay=0:0:format=auto:shortest=1,format=yuv420p[vout]")

    if project.music:
        m = add_input(["-stream_loop", "-1", "-i", project.resolve(project.music.path)])
        filters.append(_music_chain(project, m))
        amap = "[aout]"
    else:
        amap = "[voice]"

    return [
        "ffmpeg", "-hide_banner", "-v", "error", "-nostdin", "-y",
        *inputs,
        "-filter_complex", ";".join(filters),
        "-map", "[vout]", "-map", amap,
        "-c:v", "libx264", "-preset", opts.preset, "-crf", str(opts.crf),
        "-pix_fmt", "yuv420p", "-r", str(FPS), "-fps_mode", "cfr",
        "-c:a", "aac", "-b:a", opts.audio_bitrate, "-ar", str(AUDIO_RATE),
        "-movflags", "+faststart",
        "-progress", "pipe:1",
        str(out),
    ]


def _video_chain(project: Project, seg_labels: list[str]) -> str:
    """把各句画面接成整条片子：连着的硬切用 concat 一次接完，有转场的地方用 xfade。

    xfade 的 offset 用的是全局时间轴上的起点，而上一句画面恰好多渲了一个转场的长度，
    所以 xfade 之后总长仍然等于各句时长之和。
    """
    runs: list[list[str]] = [[]]          # 被转场分开的「硬切段」
    trans: list = []
    for i, label in enumerate(seg_labels):
        runs[-1].append(label)
        t = project.transition_after(i)
        if t:
            trans.append((t, i))
            runs.append([])

    parts: list[str] = []
    run_labels: list[str] = []
    for k, run in enumerate(runs):
        if len(run) == 1:
            run_labels.append(run[0])
        else:
            lab = f"[run{k}]"
            parts.append("".join(run) + f"concat=n={len(run)}:v=1:a=0,settb=1/{project.video.fps}{lab}")
            run_labels.append(lab)

    acc = run_labels[0]
    starts = [s for _c, s, _e in project.timeline()]
    for k, (t, clip_index) in enumerate(trans):
        nxt = run_labels[k + 1]
        out = "[vcat]" if k == len(trans) - 1 else f"[x{k}]"
        offset = starts[clip_index + 1]    # 下一句在时间轴上的起点
        parts.append(
            f"{acc}{nxt}xfade=transition={t.type}:duration={t.duration:.3f}"
            f":offset={offset:.3f}{out}"
        )
        acc = out
    if not trans:
        parts.append(f"{acc}null[vcat]")
    return ";".join(parts)


def _music_chain(project: Project, index: int) -> str:
    """背景音乐：裁到片长、淡入淡出、可选闪避（有人说话就自动压低）。"""
    m = project.music
    assert m is not None
    total = project.duration
    fade_out_at = max(0.0, total - m.fade_out)
    chain = (
        f"[{index}:a]aformat=sample_fmts=fltp:sample_rates={AUDIO_RATE}:channel_layouts=stereo,"
        f"atrim=start={m.start:.3f},asetpts=PTS-STARTPTS,"
        f"atrim=duration={total:.3f},volume={m.volume:.3f}"
    )
    if m.fade_in > 0:
        chain += f",afade=t=in:st=0:d={m.fade_in:.3f}"
    if m.fade_out > 0:
        chain += f",afade=t=out:st={fade_out_at:.3f}:d={m.fade_out:.3f}"
    chain += "[music]"

    if not m.duck:
        return chain + ";[voice][music]amix=inputs=2:normalize=0:dropout_transition=0[aout]"
    # 人声作为旁链去压音乐，说话时音乐自动让路
    return (
        chain
        + ";[voice]asplit=2[voice_out][key]"
        + ";[music][key]sidechaincompress=threshold=0.04:ratio=4:attack=20:release=350[ducked]"
        + ";[voice_out][ducked]amix=inputs=2:normalize=0:dropout_transition=0[aout]"
    )


def render(
    project: Project,
    out: str | Path,
    opts: RenderOptions | None = None,
    on_progress: Callable[[float, str], None] | None = None,
) -> Path:
    """渲染成片。返回输出路径。"""
    opts = opts or RenderOptions()
    media.require_ffmpeg()
    problems = project.validate()
    if problems:
        raise ProjectError("工程有问题，先修：\n  - " + "\n  - ".join(problems))

    out = Path(out).expanduser()
    out.parent.mkdir(parents=True, exist_ok=True)
    workdir = Path(tempfile.mkdtemp(prefix="fluffycut-"))
    try:
        cmd = build_command(project, out, workdir, opts)
        if opts.dry_run:
            print(" ".join(_quote(c) for c in cmd))
            return out
        media.run(cmd, total_seconds=project.duration, on_progress=on_progress)
        return out
    finally:
        if opts.keep_temp:
            print(f"[临时文件保留] {workdir}", file=sys.stderr)
        else:
            shutil.rmtree(workdir, ignore_errors=True)


def render_frame(project: Project, out: str | Path, at: float = 0.0) -> Path:
    """导出某一时刻的单帧 PNG（走 PIL 合成，和界面预览同一条路）。"""
    out = Path(out)
    out.parent.mkdir(parents=True, exist_ok=True)
    for clip, start, end in project.timeline():
        if start <= at < end or clip is project.clips[-1]:
            layers.compose_frame(project, clip, at - start).save(out)
            return out
    raise ProjectError("工程里没有片段")


def _quote(s: str) -> str:
    return f"'{s}'" if any(c in s for c in " ;[]'\"*?") else s


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        prog="python -m core.render", description="FluffyCut 渲染器：project.json -> mp4"
    )
    ap.add_argument("project", help="project.json 或工程目录")
    ap.add_argument("-o", "--out", default=None, help="输出文件（默认 <工程目录>/out.mp4）")
    ap.add_argument("--frame", type=float, default=None, metavar="秒", help="只导出该时刻的单帧 PNG")
    ap.add_argument("--crf", type=int, default=18)
    ap.add_argument("--preset", default="medium")
    ap.add_argument("--dry-run", action="store_true", help="只打印 ffmpeg 命令")
    ap.add_argument("--keep-temp", action="store_true", help="保留中间 PNG，方便排查排版")
    args = ap.parse_args(argv)

    try:
        project = Project.load(args.project)
    except ProjectError as e:
        print(f"错误：{e}", file=sys.stderr)
        return 2

    if args.frame is not None:
        out = Path(args.out or (project.root / "frame.png"))
        render_frame(project, out, args.frame)
        print(f"单帧已导出：{out}")
        return 0

    out = Path(args.out or (project.root / "out.mp4"))
    opts = RenderOptions(crf=args.crf, preset=args.preset,
                         keep_temp=args.keep_temp, dry_run=args.dry_run)

    last = -1

    def progress(p: float, note: str) -> None:
        nonlocal last
        pct = int(p * 100)
        if pct != last:
            last = pct
            bar = "█" * (pct // 4) + "·" * (25 - pct // 4)
            print(f"\r渲染中 {bar} {pct:3d}%  {note}", end="", flush=True)

    try:
        render(project, out, opts, on_progress=None if args.dry_run else progress)
    except (ProjectError, media.MediaError) as e:
        print(f"\n错误：{e}", file=sys.stderr)
        return 1
    if not args.dry_run:
        print(f"\n完成：{out}  （{len(project.clips)} 句 / {project.duration:.1f} 秒）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
