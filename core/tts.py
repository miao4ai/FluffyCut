"""配音：macOS 系统 `say` -> 音频文件 -> 时长回写 project.json。

时间轴跟着音频走：合成完把真实时长写进 audio.duration，并清掉手写的 duration，
让这一句的长度由配音决定。同时记下台词的 text_sha —— 之后改了字，界面就能提示「配音已过期」。

想换成别的 TTS（ElevenLabs / Azure / 本地模型），只需实现 Engine 协议并注册到 ENGINES。
"""

from __future__ import annotations

import argparse
import hashlib
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable, Protocol

from . import media
from .project import Audio, Clip, Project, ProjectError

DEFAULT_VOICE = "Tingting"
DEFAULT_RATE = 190          # 字/分，190 左右接近知识类短视频的语速


def text_sha(text: str) -> str:
    return hashlib.sha1((text or "").strip().encode("utf-8")).hexdigest()[:12]


def is_stale(clip: Clip) -> bool:
    """配音是否跟当前台词对不上（没配音也算 stale）。"""
    if not clip.audio or not clip.audio.path:
        return True
    return clip.audio.text_sha != text_sha(clip.text)


class TTSError(RuntimeError):
    pass


class Engine(Protocol):
    name: str

    def available(self) -> bool: ...
    def voices(self) -> list[dict]: ...
    def synth(self, text: str, out: Path, voice: str | None, rate: int) -> Path: ...


@dataclass
class SayEngine:
    """macOS 内置 TTS。零成本、离线、够用。"""

    name: str = "say"
    # m4a 而不是 aiff：体积小一个数量级，浏览器和 WKWebView 都能直接放。
    # aiff 仍然支持（老工程里就是它），只是不再是默认。
    fmt: str = "m4a"

    def available(self) -> bool:
        return shutil.which("say") is not None

    def voices(self) -> list[dict]:
        if not self.available():
            return []
        out = subprocess.run(["say", "-v", "?"], capture_output=True, text=True).stdout
        voices = []
        for line in out.splitlines():
            parts = line.split("#", 1)[0].split()
            if len(parts) < 2:
                continue
            locale = parts[-1]
            name = " ".join(parts[:-1])
            voices.append({"name": name, "locale": locale, "zh": locale.startswith("zh")})
        # 中文音色排前面
        voices.sort(key=lambda v: (not v["zh"], v["name"]))
        return voices

    def synth(self, text: str, out: Path, voice: str | None, rate: int) -> Path:
        if not self.available():
            raise TTSError("这台机器没有 `say` 命令（非 macOS？换一个 TTS 引擎）")
        text = (text or "").strip()
        if not text:
            raise TTSError("空台词没法配音")
        out.parent.mkdir(parents=True, exist_ok=True)
        cmd = ["say", "-o", str(out)]
        if voice:
            cmd += ["-v", voice]
        if rate:
            cmd += ["-r", str(int(rate))]
        if out.suffix.lower() in (".m4a", ".mp4"):
            cmd += ["--file-format=m4af", "--data-format=aac"]
        # .aiff 走 say 的默认编码：显式指定 data-format 反而会被拒（fmt?）
        cmd += ["--", text]
        r = subprocess.run(cmd, capture_output=True, text=True)
        if r.returncode != 0 or not out.exists():
            raise TTSError(f"say 失败：{r.stderr.strip() or r.returncode}")
        return out


ENGINES: dict[str, Engine] = {"say": SayEngine()}


def get_engine(name: str = "say") -> Engine:
    if name not in ENGINES:
        raise TTSError(f"没有名为 {name!r} 的 TTS 引擎，可用：{', '.join(ENGINES)}")
    return ENGINES[name]


def synth_clip(
    project: Project,
    clip: Clip,
    voice: str | None = DEFAULT_VOICE,
    rate: int = DEFAULT_RATE,
    engine: str = "say",
    adopt_duration: bool = True,
    audio_dir: str = "assets/voice",
) -> Clip:
    """给一句台词配音，并把时长写回 clip。"""
    eng = get_engine(engine)
    ext = getattr(eng, "fmt", "aiff")
    rel = f"{audio_dir}/{clip.id}.{ext}"
    out = project.resolve(rel)
    assert out is not None
    eng.synth(clip.text, out, voice, rate)

    dur = media.duration(out)
    if dur <= 0:
        raise TTSError(f"合成出来的音频读不出时长：{out}")
    clip.audio = Audio(path=rel, duration=dur, voice=voice, text_sha=text_sha(clip.text))
    if adopt_duration:
        clip.duration = None                     # 让音频决定这一句多长
    return clip


def synth_project(
    project: Project,
    clip_ids: Iterable[str] | None = None,
    voice: str | None = DEFAULT_VOICE,
    rate: int = DEFAULT_RATE,
    engine: str = "say",
    only_stale: bool = False,
    on_progress: Callable[[int, int, Clip], None] | None = None,
) -> list[str]:
    """批量配音，返回实际处理了的 clip id。"""
    targets = [c for c in project.clips if clip_ids is None or c.id in set(clip_ids)]
    targets = [c for c in targets if c.text.strip()]
    if only_stale:
        targets = [c for c in targets if is_stale(c)]
    done = []
    for i, clip in enumerate(targets, 1):
        synth_clip(project, clip, voice=voice, rate=rate, engine=engine)
        done.append(clip.id)
        if on_progress:
            on_progress(i, len(targets), clip)
    return done


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m core.tts", description="给工程里的台词配音并回写时长")
    ap.add_argument("project", help="project.json 或工程目录")
    ap.add_argument("--voice", default=DEFAULT_VOICE)
    ap.add_argument("--rate", type=int, default=DEFAULT_RATE)
    ap.add_argument("--only", nargs="*", metavar="CLIP_ID", help="只配这几句")
    ap.add_argument("--stale", action="store_true", help="只补配音缺失或与台词不一致的句子")
    ap.add_argument("--list-voices", action="store_true")
    ap.add_argument("--dry-run", action="store_true", help="不写回 project.json")
    args = ap.parse_args(argv)

    if args.list_voices:
        for v in get_engine("say").voices():
            print(f"{v['name']:<24} {v['locale']}")
        return 0

    try:
        project = Project.load(args.project)
        def prog(i: int, n: int, clip: Clip) -> None:
            print(f"[{i}/{n}] {clip.id}  {clip.text[:24]}  -> {clip.audio.duration:.2f}s")

        done = synth_project(project, args.only, args.voice, args.rate,
                             only_stale=args.stale, on_progress=prog)
    except (ProjectError, TTSError, media.MediaError) as e:
        print(f"错误：{e}", file=sys.stderr)
        return 1

    if not done:
        print("没有需要配音的句子。")
        return 0
    if not args.dry_run:
        project.save()
        print(f"已回写 {project.path}，总时长 {project.duration:.1f} 秒")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
