"""拆解一个参考片：它的节奏是什么样的。

做知识类短视频，最该抄的从来不是画面，是**节奏** —— 一句话平均几秒、
几秒切一次镜头、停顿留在哪里。这个模块把一个 mp4 拆成这些数字，
并且能直接生成一份同节奏的 project.json 骨架，让你在人家的节奏上填自己的内容。

两层，下面一层不需要任何额外依赖：

    ffmpeg 就能给出的：镜头切点（scene 检测）、语音段（silencedetect 反推静音）
    需要 whisper 的：把话转成文字

没装 whisper 也能用，只是生成的骨架里台词是空的 —— 节奏照样是对的。
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import statistics
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

from . import media
from .project import Audio, Clip, Project, Style, Video, Visual

# silencedetect 的判定：低于 -32dB 且持续 0.35s 以上算停顿
SILENCE_DB = -32
SILENCE_MIN = 0.35
# 短于这个的"语音段"多半是杂音，不当成一句话
MIN_SPEECH = 0.35
# 一句话最长到这儿；再长就按镜头切点切开，还长就等分。
# 不这么做的话，一条从头到尾都有声音的片子会变成"一句 50 秒"，等于没法编辑。
MAX_SEGMENT = 6.0
TARGET_SEGMENT = 3.5
# 场景切换阈值，越小越敏感。跑两遍：
# ffmpeg 的 scene 判定看的是亮度，两个镜头亮度接近、只有颜色不同时会整刀漏掉，
# 所以再拿色度平面（U）跑一遍补上。实测纯色切换 5 刀里亮度只抓到 1 刀，色度 5 刀全中；
# 反过来在一直在动、没有真实切点的素材上，两遍都是 0 误报。
SCENE_THRESHOLD = 0.3
CHROMA_THRESHOLD = 0.15
CUT_MERGE = 0.25            # 两遍抓到的同一刀，时间差在这以内就算一刀


@dataclass
class Segment:
    """一句话（或一段连续人声）。"""

    start: float
    end: float
    text: str = ""

    @property
    def duration(self) -> float:
        return round(self.end - self.start, 3)

    def to_dict(self) -> dict:
        return {"start": round(self.start, 3), "end": round(self.end, 3),
                "duration": self.duration, "text": self.text}


@dataclass
class Report:
    path: str
    duration: float
    segments: list[Segment] = field(default_factory=list)
    cuts: list[float] = field(default_factory=list)
    source: str = "silence"          # 句子是怎么来的：silence / <whisper 引擎名>

    # ---------- 派生指标 ----------

    @property
    def speech_seconds(self) -> float:
        return round(sum(s.duration for s in self.segments), 2)

    @property
    def speech_ratio(self) -> float:
        return round(self.speech_seconds / self.duration, 3) if self.duration else 0.0

    @property
    def seconds_per_sentence(self) -> float:
        return round(statistics.mean([s.duration for s in self.segments]), 2) if self.segments else 0.0

    @property
    def median_sentence(self) -> float:
        return round(statistics.median([s.duration for s in self.segments]), 2) if self.segments else 0.0

    @property
    def cuts_per_minute(self) -> float:
        return round(len(self.cuts) / self.duration * 60, 1) if self.duration else 0.0

    @property
    def chars_per_second(self) -> float:
        chars = sum(len(re.sub(r"\s+", "", s.text)) for s in self.segments)
        return round(chars / self.speech_seconds, 2) if chars and self.speech_seconds else 0.0

    def cuts_in(self, seg: Segment) -> list[float]:
        return [c for c in self.cuts if seg.start < c < seg.end]

    def to_dict(self) -> dict:
        return {
            "path": self.path,
            "duration": self.duration,
            "source": self.source,
            "stats": {
                "sentences": len(self.segments),
                "seconds_per_sentence": self.seconds_per_sentence,
                "median_sentence": self.median_sentence,
                "speech_ratio": self.speech_ratio,
                "cuts": len(self.cuts),
                "cuts_per_minute": self.cuts_per_minute,
                "chars_per_second": self.chars_per_second,
            },
            "segments": [s.to_dict() for s in self.segments],
            "cuts": [round(c, 3) for c in self.cuts],
        }

    def summary(self) -> str:
        pace = "偏快" if self.seconds_per_sentence < 1.5 else (
            "偏慢" if self.seconds_per_sentence > 3.0 else "在甜区里")
        lines = [
            f"参考片：{Path(self.path).name}",
            f"  总长 {self.duration:.1f} 秒，{len(self.segments)} 句"
            + {"silence": "（按停顿切分，没有台词）",
               "cuts": "（按镜头切点切分，没有人声）"}.get(self.source, f"（{self.source} 转写）"),
            f"  每句平均 {self.seconds_per_sentence:.2f} 秒，中位数 {self.median_sentence:.2f} 秒 —— {pace}",
            f"  人声占 {self.speech_ratio * 100:.0f}%，其余是停顿和纯画面",
            f"  镜头切了 {len(self.cuts)} 次，约 {self.cuts_per_minute:.1f} 次/分钟",
        ]
        if self.chars_per_second:
            lines.append(f"  语速约 {self.chars_per_second:.1f} 字/秒")
        return "\n".join(lines)


# ---------------------------------------------------------------- ffmpeg 侧


def _run_stderr(args: list[str]) -> str:
    """跑一条只看 stderr 的 ffmpeg 分析命令（silencedetect / showinfo 都往那儿写）。"""
    exe = media.tool("ffmpeg")
    if not exe:
        raise media.MediaError("找不到 ffmpeg，装一下：brew install ffmpeg")
    r = subprocess.run([exe, "-hide_banner", "-nostdin", *args],
                       capture_output=True, text=True)
    return r.stderr


def _scene_times(path: str | Path, chain: str) -> list[float]:
    out = _run_stderr(["-i", str(path), "-filter:v", chain, "-f", "null", "-"])
    return [float(m) for m in re.findall(r"pts_time:([0-9.]+)", out)]


def merge_cuts(times: list[float], window: float = CUT_MERGE) -> list[float]:
    """两遍检测抓到的时间点合成一份：挨得很近的算同一刀。"""
    out: list[float] = []
    for t in sorted(times):
        if not out or t - out[-1] > window:
            out.append(round(t, 3))
    return out


def detect_cuts(path: str | Path, threshold: float = SCENE_THRESHOLD,
                chroma: float = CHROMA_THRESHOLD) -> list[float]:
    """镜头切点（秒）。硬切基本都能抓到，慢转场（叠化、推拉）会漏。"""
    times = _scene_times(path, f"select='gt(scene,{threshold})',showinfo")
    times += _scene_times(
        path, f"format=yuv420p,extractplanes=u,select='gt(scene,{chroma})',showinfo")
    return merge_cuts(times)


def detect_speech(path: str | Path, total: float,
                  noise_db: int = SILENCE_DB, min_silence: float = SILENCE_MIN) -> list[Segment]:
    """人声段。silencedetect 给的是静音区间，反过来就是说话的地方。"""
    out = _run_stderr(["-i", str(path), "-af",
                       f"silencedetect=noise={noise_db}dB:d={min_silence}",
                       "-f", "null", "-"])
    starts = [float(m) for m in re.findall(r"silence_start:\s*(-?[0-9.]+)", out)]
    ends = [float(m) for m in re.findall(r"silence_end:\s*([0-9.]+)", out)]
    return _invert_silence(starts, ends, total)


def _invert_silence(starts: list[float], ends: list[float], total: float) -> list[Segment]:
    """静音区间 -> 人声区间。单独拆出来是为了能拿固定输入做单测。"""
    silences: list[tuple[float, float]] = []
    for i, s in enumerate(starts):
        e = ends[i] if i < len(ends) else total     # 结尾那段静音没有 silence_end
        silences.append((max(0.0, s), min(total, e)))
    silences.sort()

    speech, cursor = [], 0.0
    for s, e in silences:
        if s - cursor >= MIN_SPEECH:
            speech.append(Segment(round(cursor, 3), round(s, 3)))
        cursor = max(cursor, e)
    if total - cursor >= MIN_SPEECH:
        speech.append(Segment(round(cursor, 3), round(total, 3)))
    return speech


# ---------------------------------------------------------------- 转写（可选）


def transcribe(path: str | Path, language: str = "zh") -> tuple[list[Segment], str] | None:
    """把话转成文字。没有任何可用引擎就返回 None —— 节奏分析不受影响。

    引擎按「装了哪个用哪个」的顺序探测，都是可选依赖：
        mlx-whisper（Apple Silicon 最快）> faster-whisper > openai-whisper > whisper-cli 二进制
    """
    for probe in (_try_mlx, _try_faster, _try_openai, _try_binary):
        got = probe(Path(path), language)
        if got is not None:
            return got
    return None


def transcriber_name() -> str | None:
    """当前能用的转写引擎名，界面用它决定要不要给按钮。"""
    for name, mod in (("mlx-whisper", "mlx_whisper"), ("faster-whisper", "faster_whisper"),
                      ("openai-whisper", "whisper")):
        try:
            __import__(mod)
            return name
        except ImportError:
            continue
    import shutil

    return "whisper-cli" if shutil.which("whisper-cli") or shutil.which("whisper") else None


def _clean(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "").strip())


def _try_mlx(path: Path, language: str):
    try:
        import mlx_whisper
    except ImportError:
        return None
    r = mlx_whisper.transcribe(str(path), language=language,
                               path_or_hf_repo="mlx-community/whisper-small-mlx")
    return ([Segment(float(s["start"]), float(s["end"]), _clean(s["text"]))
             for s in r.get("segments", [])], "mlx-whisper")


def _try_faster(path: Path, language: str):
    try:
        from faster_whisper import WhisperModel
    except ImportError:
        return None
    model = WhisperModel("small", device="cpu", compute_type="int8")
    segs, _info = model.transcribe(str(path), language=language)
    return ([Segment(float(s.start), float(s.end), _clean(s.text)) for s in segs],
            "faster-whisper")


def _try_openai(path: Path, language: str):
    try:
        import whisper
    except ImportError:
        return None
    r = whisper.load_model("small").transcribe(str(path), language=language)
    return ([Segment(float(s["start"]), float(s["end"]), _clean(s["text"]))
             for s in r.get("segments", [])], "openai-whisper")


def _try_binary(path: Path, language: str):
    """whisper.cpp 的命令行（brew install whisper-cpp）。要先转成 16k 单声道 wav。"""
    import shutil
    import tempfile

    exe = shutil.which("whisper-cli") or shutil.which("whisper")
    if not exe:
        return None
    ff = media.tool("ffmpeg")
    if not ff:
        return None
    with tempfile.TemporaryDirectory() as tmp:
        wav = Path(tmp) / "a.wav"
        subprocess.run([ff, "-v", "error", "-y", "-i", str(path), "-ac", "1",
                        "-ar", "16000", str(wav)], check=True)
        r = subprocess.run([exe, "-f", str(wav), "-l", language, "-oj", "-of",
                            str(Path(tmp) / "out")], capture_output=True, text=True)
        data = Path(tmp) / "out.json"
        if r.returncode != 0 or not data.exists():
            return None
        raw = json.loads(data.read_text("utf-8"))
    segs = [Segment(_ts(s["offsets"]["from"]), _ts(s["offsets"]["to"]), _clean(s["text"]))
            for s in raw.get("transcription", [])]
    return (segs, "whisper-cli")


def _ts(ms: float) -> float:
    return round(float(ms) / 1000.0, 3)


# ---------------------------------------------------------------- 主流程


def split_long(segments: list[Segment], cuts: list[float],
               max_len: float = MAX_SEGMENT, target: float = TARGET_SEGMENT) -> list[Segment]:
    """把过长的段切碎：优先切在镜头切点上，实在没有就等分。

    台词留在第一块上 —— 与其把一句话胡乱劈成几截，不如让人自己去分。
    """
    out: list[Segment] = []
    for seg in segments:
        if seg.duration <= max_len:
            out.append(seg)
            continue
        inner = [c for c in cuts if seg.start + 0.4 < c < seg.end - 0.4]
        pieces: list[tuple[float, float]] = []
        for a, b in zip([seg.start, *inner], [*inner, seg.end]):
            if b - a <= max_len:
                pieces.append((a, b))
                continue
            n = max(2, round((b - a) / target))
            step = (b - a) / n
            pieces += [(a + i * step, a + (i + 1) * step) for i in range(n)]
        for i, (a, b) in enumerate(pieces):
            out.append(Segment(round(a, 3), round(b, 3), seg.text if i == 0 else ""))
    return out


def segments_from_cuts(cuts: list[float], total: float,
                       target: float = TARGET_SEGMENT) -> list[Segment]:
    """完全没有人声时，按镜头切点分段；连切点都没有就按目标长度等分。"""
    edges = [0.0, *[c for c in cuts if 0.2 < c < total - 0.2], total]
    return split_long([Segment(round(a, 3), round(b, 3)) for a, b in zip(edges, edges[1:])],
                      cuts, MAX_SEGMENT, target)


def analyze(path: str | Path, with_text: bool = False) -> Report:
    """拆一个参考片。with_text=True 且装了 whisper 才会转写台词。"""
    path = Path(path)
    if not path.exists():
        raise media.MediaError(f"文件不存在：{path}")
    total = media.duration(path)
    if total <= 0:
        raise media.MediaError(f"读不出时长，这可能不是个视频：{path.name}")

    report = Report(path=str(path), duration=round(total, 3))
    report.cuts = detect_cuts(path)

    got = transcribe(path) if with_text else None
    if got:
        segments, engine = got
        report.segments = [s for s in segments if s.duration >= MIN_SPEECH]
        report.source = engine
    elif media.has_audio(path):
        report.segments = detect_speech(path, total)

    # 一整条都有声音（音乐垫底的片子很常见）会得到"一句 50 秒"，那不是时间轴，
    # 是一坨。切开它，让人拿到手就能编辑。
    report.segments = split_long(report.segments, report.cuts)
    if not report.segments:
        report.segments = segments_from_cuts(report.cuts, total)
        report.source = "cuts"
    return report


def to_project(report: Report, title: str = "", keep_text: bool = True,
               source: str | None = None) -> Project:
    """把分析结果变成一份可以直接开剪的工程。

    一句话 = 一个片段，时长照抄参考片；参考片在这句话中间切了几刀，
    这个片段就带几个镜头，各自的长度也照抄。

    给了 source（工程内的视频相对路径）就是「把这个片子打开来剪」：每个镜头指向
    原片对应的那一段（in/out），渲染出来基本还原原片，然后你可以一句一句替换。
    不给就是「只要节奏」：镜头全是纯色占位，等你填自己的素材。
    """
    project = Project(
        title=title or f"仿：{Path(report.path).stem}",
        style=Style(brand_text=""),
        video=Video(),
    )
    if not report.segments:
        project.clips = [Clip(id="c1", text="", duration=round(report.duration, 3))]
        return project

    # 片段首尾相接：相邻两句之间的停顿一人一半，总长仍等于参考片
    bounds = [0.0]
    for a, b in zip(report.segments, report.segments[1:]):
        bounds.append(round((a.end + b.start) / 2, 3))
    bounds.append(round(report.duration, 3))

    for i, seg in enumerate(report.segments):
        start, end = bounds[i], bounds[i + 1]
        cuts = [c for c in report.cuts if start < c < end]
        edges = [start, *cuts, end]
        if source:
            # 每个镜头指回原片的那一段，于是这份工程是「原片的可编辑副本」
            shots = [
                Visual(type="video", path=source, src_in=round(a, 3), src_out=round(b, 3),
                       seconds=round(b - a, 3))
                for a, b in zip(edges, edges[1:])
            ]
        else:
            shots = [Visual(type="color", seconds=round(b - a, 3))
                     for a, b in zip(edges, edges[1:])]
        # 最后一个镜头不写死时长：让它去吃剩下的时间。写死的话，各镜头之和会比
        # 吸附到整帧之后的句长多出小半帧，工程一打开就报"镜头时长超过本句"。
        shots[-1].seconds = None
        project.clips.append(Clip(
            id=f"c{i + 1}",
            text=seg.text if keep_text else "",
            visuals=shots,
            duration=round(end - start, 3),
            note=f"参考片 {seg.start:.2f}–{seg.end:.2f}s"
                 + (f"，中间切了 {len(cuts)} 刀" if cuts else ""),
        ))
    return project


def slice_audio(report: Report, project: Project, source: Path,
                rel_dir: str = "assets/voice") -> int:
    """把原片的声音按句切开，挂到各个片段上 —— 打开就能听出原来的样子。

    走的是普通的 clip.audio 字段，所以之后想重新配音，「配音」按钮直接覆盖掉即可。
    """
    from . import tts

    made = 0
    for clip, seg in zip(project.clips, report.segments):
        rel = f"{rel_dir}/{clip.id}.m4a"
        dest = project.resolve(rel)
        assert dest is not None
        try:
            media.extract_audio(source, dest, start=seg.start, end=seg.end)
        except media.MediaError:
            continue
        clip.audio = Audio(path=rel, duration=media.duration(dest),
                           text_sha=tts.text_sha(clip.text))
        clip.duration = round(project.seconds_of(clip), 3)   # 时长仍以原片为准
        made += 1
    return made


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        prog="python -m core.analyze",
        description="拆解一个参考片的节奏，并可生成同节奏的工程骨架",
    )
    ap.add_argument("video", help="参考片 mp4/mov")
    ap.add_argument("--transcribe", action="store_true", help="连台词一起扒（需要 whisper）")
    ap.add_argument("--project", metavar="目录", help="生成同节奏的工程骨架到这个目录")
    ap.add_argument("--title", default="", help="骨架的标题")
    ap.add_argument("--json", metavar="文件", help="把完整分析结果写成 json")
    ap.add_argument("--no-text", action="store_true", help="骨架里不带扒下来的台词")
    ap.add_argument("--rhythm-only", action="store_true",
                    help="只要节奏：镜头留成纯色占位，不把原片放进工程")
    args = ap.parse_args(argv)

    try:
        report = analyze(args.video, with_text=args.transcribe)
    except media.MediaError as e:
        print(f"错误：{e}", file=sys.stderr)
        return 1

    print(report.summary())
    if args.transcribe and report.source == "silence":
        print("  （没有可用的转写引擎，台词是空的：pip install mlx-whisper 或 faster-whisper）",
              file=sys.stderr)

    if report.segments:
        print("\n  逐句：")
        for i, s in enumerate(report.segments[:40], 1):
            bar = "█" * max(1, int(s.duration * 4))
            print(f"    {i:>2}. {s.duration:>5.2f}s {bar} {s.text[:28]}")
        if len(report.segments) > 40:
            print(f"    …… 还有 {len(report.segments) - 40} 句")

    if args.json:
        Path(args.json).write_text(
            json.dumps(report.to_dict(), ensure_ascii=False, indent=2) + "\n", "utf-8")
        print(f"\n分析结果已写入 {args.json}")

    if args.project:
        out = Path(args.project)
        (out / "assets").mkdir(parents=True, exist_ok=True)
        rel = None
        if not args.rhythm_only:
            src = out / "assets" / f"source{Path(args.video).suffix.lower()}"
            shutil.copy2(args.video, src)
            rel = f"assets/{src.name}"
        project = to_project(report, args.title, keep_text=not args.no_text, source=rel)
        project.save(out / "project.json")
        if rel:
            n = slice_audio(report, project, out / rel)
            project.save()
            print(f"原片已放进工程，{n} 句带上了原声")
        print(f"工程已生成：{out / 'project.json'}"
              f"（{len(project.clips)} 句 / {project.duration:.1f} 秒）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
