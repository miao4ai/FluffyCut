"""project.json 的数据模型：读、校验、补默认值、写回。

约定：project.json 里所有路径都相对于该文件所在目录，方便整个工程目录搬走。
写回时保持 2 空格缩进 / 不转义中文 / 键序稳定 —— 这个文件要能进 git diff。

时间轴的不变量（渲染器和界面都依赖它）：
    一句台词 = 一个片段，片段首尾相接，总时长 = 各句时长之和。
片段内部可以切成多个镜头，片段之间可以加转场，但**都不改变这条时间轴** ——
转场吃的是上一句画面的延长部分，不是它的时长。所以字幕永远卡在台词该在的位置。
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

SCHEMA_VERSION = 1

# 中文语速经验值：约 5.5 字/秒，句尾留 0.35s 换气
CHARS_PER_SECOND = 5.5
TAIL_PAD = 0.35
MIN_DURATION = 1.0
MAX_DURATION = 20.0

# 节奏参考区间（README：1.5–3 秒/句）
PACE_MIN = 1.5
PACE_MAX = 3.0

# 支持的转场，名字直接对应 ffmpeg xfade 的 transition 参数
TRANSITIONS = (
    "fade", "fadeblack", "fadewhite", "dissolve",
    "slideleft", "slideright", "slideup", "slidedown",
    "wipeleft", "wiperight", "circleopen", "circleclose", "smoothleft", "radial",
)
DEFAULT_TRANSITION_SECONDS = 0.35
MIN_SHOT_SECONDS = 0.05          # 再短的镜头 ffmpeg 也接不住


class ProjectError(ValueError):
    pass


def estimate_duration(text: str) -> float:
    """没有配音时，按字数估个时长，让时间轴先跑起来。"""
    n = len(re.sub(r"\s+", "", text or ""))
    if n == 0:
        return MIN_DURATION
    return round(min(MAX_DURATION, max(MIN_DURATION, n / CHARS_PER_SECOND + TAIL_PAD)), 2)


def _crop(value: Any) -> tuple[float, float, float, float]:
    """裁切写成 {"top":0.18} 这种，只写要裁的边。四边都按比例，0.18 = 裁掉 18%。

    烧死在原片里的字幕就是这么去掉的 —— 它是像素，改不了，只能裁走或遮住。
    """
    if not value:
        return (0.0, 0.0, 0.0, 0.0)
    if isinstance(value, (list, tuple)) and len(value) == 4:
        vals = [max(0.0, min(0.9, float(v or 0))) for v in value]
        return (vals[0], vals[1], vals[2], vals[3])
    if not isinstance(value, dict):
        return (0.0, 0.0, 0.0, 0.0)
    return tuple(  # type: ignore[return-value]
        max(0.0, min(0.9, float(value.get(k) or 0))) for k in ("top", "right", "bottom", "left")
    )


def _num(value: Any, default: float | None = None) -> float | None:
    if value is None or value == "":
        return default
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


# ---------------------------------------------------------------- 镜头


@dataclass
class Visual:
    """一个镜头：这句话里某一段时间显示的画面。

    视频素材可以只取其中一段（src_in/src_out），这就是"剪辑"最核心的动作。
    """

    type: str = "color"            # image | video | color
    path: str | None = None
    color: str = "#101014"
    fit: str = "cover"             # cover(裁切铺满) | contain(留边)
    src_in: float = 0.0            # 素材入点（秒），JSON 里写 "in"
    src_out: float | None = None   # 素材出点（秒），JSON 里写 "out"，None = 到结尾
    speed: float = 1.0             # 变速，2.0 = 两倍速
    crop: tuple[float, float, float, float] = (0.0, 0.0, 0.0, 0.0)   # 上右下左，各裁掉多少（比例）
    seconds: float | None = None   # 这个镜头在本句里占多久，不写就和同句其他镜头平分
    prompt: str | None = None      # 配图 prompt，留着复现

    @classmethod
    def from_dict(cls, d: dict[str, Any] | None) -> "Visual":
        d = dict(d or {})
        v = cls(
            type=d.get("type", "image" if d.get("path") else "color"),
            path=d.get("path"),
            color=d.get("color", "#101014"),
            fit=d.get("fit", "cover"),
            src_in=_num(d.get("in"), 0.0) or 0.0,
            src_out=_num(d.get("out"), None),
            speed=_num(d.get("speed"), 1.0) or 1.0,
            crop=_crop(d.get("crop")),
            seconds=_num(d.get("seconds"), None),
            prompt=d.get("prompt"),
        )
        if v.type not in ("image", "video", "color"):
            raise ProjectError(f"visual.type 只能是 image/video/color，收到 {v.type!r}")
        if v.type in ("image", "video") and not v.path:
            raise ProjectError(f"visual.type={v.type} 必须给 path")
        if v.fit not in ("cover", "contain"):
            raise ProjectError(f"visual.fit 只能是 cover/contain，收到 {v.fit!r}")
        if v.speed <= 0:
            raise ProjectError("visual.speed 必须大于 0")
        if v.src_out is not None and v.src_out <= v.src_in:
            raise ProjectError(f"素材出点（{v.src_out}）必须大于入点（{v.src_in}）")
        top, right, bottom, left = v.crop
        if top + bottom >= 0.95 or left + right >= 0.95:
            raise ProjectError("裁切上下（或左右）加起来不能把画面裁没了")
        return v

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {"type": self.type}
        if self.path:
            d["path"] = self.path
        if self.type == "color":
            d["color"] = self.color
        if self.fit != "cover":
            d["fit"] = self.fit
        if self.src_in:
            d["in"] = round(self.src_in, 3)
        if self.src_out is not None:
            d["out"] = round(self.src_out, 3)
        if self.speed != 1.0:
            d["speed"] = round(self.speed, 3)
        if any(self.crop):
            top, right, bottom, left = self.crop
            d["crop"] = {k: round(v, 4) for k, v in
                         (("top", top), ("right", right), ("bottom", bottom), ("left", left)) if v}
        if self.seconds is not None:
            d["seconds"] = round(self.seconds, 3)
        if self.prompt:
            d["prompt"] = self.prompt
        return d

    @property
    def source_seconds(self) -> float | None:
        """入出点之间有多长（按变速折算后）。出点未知时返回 None。"""
        if self.src_out is None:
            return None
        return max(0.01, (self.src_out - self.src_in) / self.speed)


@dataclass
class Transition:
    """到下一句的转场。吃的是上一句画面的延长部分，不占时间轴。"""

    type: str = "fade"
    duration: float = DEFAULT_TRANSITION_SECONDS

    @classmethod
    def from_dict(cls, d: Any) -> "Transition | None":
        if not d:
            return None
        if isinstance(d, str):          # 简写："transition": "fade"
            d = {"type": d}
        t = cls(type=str(d.get("type", "fade")),
                duration=_num(d.get("duration"), DEFAULT_TRANSITION_SECONDS) or DEFAULT_TRANSITION_SECONDS)
        if t.type not in TRANSITIONS:
            raise ProjectError(f"不支持的转场 {t.type!r}，可用：{', '.join(TRANSITIONS)}")
        if t.duration <= 0:
            raise ProjectError("转场时长必须大于 0")
        return t

    def to_dict(self) -> dict[str, Any]:
        return {"type": self.type, "duration": round(self.duration, 3)}


@dataclass
class Overlay:
    """钉在画面上的一段字：标题、注解、花字。

    和 clips[].text 的区别：那一句是整句的字幕，位置和时机都由模板定死；
    这里是你自己往某一帧、某个位置上贴的东西，时间是句内相对时间。
    """

    text: str = ""
    start: float = 0.0              # 句内起点（秒）
    end: float | None = None        # 句内终点，None = 到这一句结束
    x: float = 0.5                  # 0–1，相对画面宽
    y: float = 0.25                 # 0–1，相对画面高
    size: int = 56                  # 字号，按 1080 宽设计，随画面等比缩放
    color: str = "#FFFFFF"
    bg: str = "#00000099"
    stroke: str = "#000000"

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "Overlay":
        o = cls(
            text=str(d.get("text", "")),
            start=_num(d.get("start"), 0.0) or 0.0,
            end=_num(d.get("end"), None),
            x=_num(d.get("x"), 0.5) if d.get("x") is not None else 0.5,
            y=_num(d.get("y"), 0.25) if d.get("y") is not None else 0.25,
            size=int(_num(d.get("size"), 56) or 56),
            color=d.get("color", "#FFFFFF"),
            bg=d.get("bg", "#00000099"),
            stroke=d.get("stroke", "#000000"),
        )
        if o.end is not None and o.end <= o.start:
            raise ProjectError(f"贴字的终点（{o.end}）必须大于起点（{o.start}）")
        o.x = max(0.0, min(1.0, o.x))
        o.y = max(0.0, min(1.0, o.y))
        o.size = max(8, min(300, o.size))
        return o

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {"text": self.text}
        default = Overlay()
        for k in ("start", "end", "x", "y", "size", "color", "bg", "stroke"):
            v = getattr(self, k)
            if v != getattr(default, k):
                d[k] = round(v, 4) if isinstance(v, float) else v
        return d

    def window(self, clip_seconds: float) -> tuple[float, float]:
        """这段字在句内什么时候显示。"""
        a = max(0.0, min(self.start, clip_seconds))
        b = clip_seconds if self.end is None else min(self.end, clip_seconds)
        return (a, max(a + 0.05, b))


@dataclass
class Audio:
    path: str | None = None
    duration: float | None = None
    voice: str | None = None
    gain: float = 1.0                # 这一句配音的音量倍数
    text_sha: str | None = None      # 合成时的台词指纹，用来判断配音是否过期

    @classmethod
    def from_dict(cls, d: dict[str, Any] | None) -> "Audio | None":
        if not d:
            return None
        return cls(path=d.get("path"), duration=_num(d.get("duration"), None),
                   voice=d.get("voice"), gain=_num(d.get("gain"), 1.0) or 1.0,
                   text_sha=d.get("text_sha"))

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {}
        if self.path:
            d["path"] = self.path
        if self.duration is not None:
            d["duration"] = round(float(self.duration), 3)
        if self.voice:
            d["voice"] = self.voice
        if self.gain != 1.0:
            d["gain"] = round(self.gain, 3)
        if self.text_sha:
            d["text_sha"] = self.text_sha
        return d


@dataclass
class Music:
    """整片背景音乐。默认开启闪避：有人说话时自动压低。"""

    path: str | None = None
    volume: float = 0.18
    fade_in: float = 1.0
    fade_out: float = 1.5
    duck: bool = True
    start: float = 0.0               # 从音乐的第几秒开始取

    @classmethod
    def from_dict(cls, d: dict[str, Any] | None) -> "Music | None":
        if not d or not d.get("path"):
            return None
        return cls(
            path=d["path"],
            volume=_num(d.get("volume"), 0.18) or 0.0,
            fade_in=_num(d.get("fade_in"), 1.0) or 0.0,
            fade_out=_num(d.get("fade_out"), 1.5) or 0.0,
            duck=bool(d.get("duck", True)),
            start=_num(d.get("start"), 0.0) or 0.0,
        )

    def to_dict(self) -> dict[str, Any]:
        return {"path": self.path, "volume": round(self.volume, 3),
                "fade_in": round(self.fade_in, 3), "fade_out": round(self.fade_out, 3),
                "duck": self.duck, "start": round(self.start, 3)}


# ---------------------------------------------------------------- 片段


@dataclass
class Clip:
    id: str
    text: str = ""
    visuals: list[Visual] = field(default_factory=lambda: [Visual()])
    audio: Audio | None = None
    duration: float | None = None     # 显式覆盖；否则用音频时长，再否则按字数估
    kenburns: bool | str = False      # True / "in" / "out" / False
    transition: Transition | None = None
    overlays: list[Overlay] = field(default_factory=list)
    note: str = ""

    @classmethod
    def from_dict(cls, d: dict[str, Any], index: int) -> "Clip":
        raw = d.get("visual", d.get("visuals"))
        if isinstance(raw, list):
            visuals = [Visual.from_dict(v) for v in raw] or [Visual()]
        else:
            visuals = [Visual.from_dict(raw)]
        return cls(
            id=str(d.get("id") or f"c{index + 1}"),
            text=d.get("text", ""),
            visuals=visuals,
            audio=Audio.from_dict(d.get("audio")),
            duration=_num(d.get("duration"), None),
            kenburns=d.get("kenburns", False),
            transition=Transition.from_dict(d.get("transition")),
            overlays=[Overlay.from_dict(o) for o in (d.get("overlays") or [])
                      if str(o.get("text", "")).strip()],
            note=d.get("note", ""),
        )

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {"id": self.id, "text": self.text}
        # 单镜头写成对象，多镜头写成数组 —— 手改 JSON 的人不用为了加字段改结构
        d["visual"] = (self.visuals[0].to_dict() if len(self.visuals) == 1
                       else [v.to_dict() for v in self.visuals])
        if self.audio and (self.audio.path or self.audio.duration):
            d["audio"] = self.audio.to_dict()
        if self.duration is not None:
            d["duration"] = round(float(self.duration), 3)
        if self.kenburns:
            d["kenburns"] = self.kenburns
        if self.transition:
            d["transition"] = self.transition.to_dict()
        if self.overlays:
            d["overlays"] = [o.to_dict() for o in self.overlays]
        if self.note:
            d["note"] = self.note
        return d

    @property
    def visual(self) -> Visual:
        """第一个镜头。单镜头的老代码和界面走这条路。"""
        return self.visuals[0]

    @property
    def seconds(self) -> float:
        """这一句在成片里占的时长。显式 duration > 音频时长 > 字数估算。"""
        if self.duration is not None:
            return max(0.1, float(self.duration))
        if self.audio and self.audio.duration:
            return max(0.1, round(float(self.audio.duration) + TAIL_PAD, 3))
        return estimate_duration(self.text)

    @property
    def pace(self) -> str:
        """节奏标签：太快 / 合适 / 拖沓。界面用它给时长条上色。"""
        s = self.seconds
        if s < PACE_MIN:
            return "fast"
        if s > PACE_MAX:
            return "slow"
        return "ok"


@dataclass
class Style:
    font: str = "PingFang SC"
    title_color: str = "#FFE100"
    title_bg: str = "#000000B8"
    brand_text: str = ""
    brand_color: str = "#FFFFFF"
    brand_bg: str = "#000000A0"
    subtitle_color: str = "#FFFFFF"
    subtitle_stroke: str = "#000000"
    subtitle_bg: str = "#00000099"
    avatar: str | None = None
    layout: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, d: dict[str, Any] | None) -> "Style":
        d = dict(d or {})
        known = {f for f in cls.__dataclass_fields__}
        return cls(**{k: v for k, v in d.items() if k in known})

    def to_dict(self) -> dict[str, Any]:
        default = Style()
        d = {}
        for name in self.__dataclass_fields__:
            value = getattr(self, name)
            if value != getattr(default, name) or name in ("font", "title_color"):
                d[name] = value
        return d


@dataclass
class Video:
    width: int = 1080
    height: int = 1920
    fps: int = 30
    bg: str = "#000000"

    @classmethod
    def from_dict(cls, d: dict[str, Any] | None) -> "Video":
        d = dict(d or {})
        known = {f for f in cls.__dataclass_fields__}
        return cls(**{k: v for k, v in d.items() if k in known})

    def to_dict(self) -> dict[str, Any]:
        return {"width": self.width, "height": self.height, "fps": self.fps, "bg": self.bg}


@dataclass
class Project:
    title: str = ""
    style: Style = field(default_factory=Style)
    video: Video = field(default_factory=Video)
    music: Music | None = None
    clips: list[Clip] = field(default_factory=list)
    path: Path | None = None          # project.json 自身路径
    extra: dict[str, Any] = field(default_factory=dict)   # 保留未知字段，不丢用户数据

    # ---------- 读写 ----------

    @classmethod
    def from_dict(cls, data: dict[str, Any], path: Path | None = None) -> "Project":
        if not isinstance(data, dict):
            raise ProjectError("project.json 顶层必须是对象")
        known = {"version", "title", "style", "video", "music", "clips"}
        p = cls(
            title=data.get("title", ""),
            style=Style.from_dict(data.get("style")),
            video=Video.from_dict(data.get("video")),
            music=Music.from_dict(data.get("music")),
            clips=[Clip.from_dict(c, i) for i, c in enumerate(data.get("clips") or [])],
            path=Path(path) if path else None,
            extra={k: v for k, v in data.items() if k not in known},
        )
        seen: set[str] = set()
        for i, c in enumerate(p.clips):
            if c.id in seen:
                c.id = f"{c.id}_{i}"
            seen.add(c.id)
        if p.clips:
            p.clips[-1].transition = None     # 最后一句没有"下一句"可转
        return p

    @classmethod
    def load(cls, path: str | Path) -> "Project":
        path = Path(path)
        if path.is_dir():
            path = path / "project.json"
        if not path.exists():
            raise ProjectError(f"工程文件不存在：{path}")
        try:
            data = json.loads(path.read_text("utf-8"))
        except json.JSONDecodeError as e:
            raise ProjectError(f"{path} 不是合法 JSON：{e}") from e
        return cls.from_dict(data, path)

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {"version": SCHEMA_VERSION, "title": self.title}
        d.update(self.extra)
        d["style"] = self.style.to_dict()
        d["video"] = self.video.to_dict()
        if self.music:
            d["music"] = self.music.to_dict()
        d["clips"] = [c.to_dict() for c in self.clips]
        return d

    def save(self, path: str | Path | None = None) -> Path:
        target = Path(path) if path else self.path
        if target is None:
            raise ProjectError("没有指定保存路径")
        if target.is_dir():
            target = target / "project.json"
        target.parent.mkdir(parents=True, exist_ok=True)
        text = json.dumps(self.to_dict(), ensure_ascii=False, indent=2) + "\n"
        tmp = target.with_suffix(".json.tmp")
        tmp.write_text(text, "utf-8")
        tmp.replace(target)          # 原子写，编辑器崩了也不会留半个文件
        self.path = target
        return target

    # ---------- 派生信息 ----------

    @property
    def root(self) -> Path:
        return self.path.parent if self.path else Path.cwd()

    def resolve(self, rel: str | None) -> Path | None:
        """工程内相对路径 -> 绝对路径。"""
        if not rel:
            return None
        p = Path(rel)
        return p if p.is_absolute() else (self.root / p)

    def exists(self, rel: str | None) -> bool:
        p = self.resolve(rel)
        return bool(p and p.exists())

    def relativize(self, p: str | Path) -> str:
        p = Path(p)
        try:
            return str(p.resolve().relative_to(self.root.resolve()))
        except ValueError:
            return str(p)

    # ---------- 时间轴：一律以帧为单位算 ----------
    #
    # 画面只能按整帧裁，配音却能按任意秒数裁。如果时间轴用秒来累加，每句都会
    # 差半帧上下，逐句累积就成了肉眼可见的音画不同步。所以每句先吸附到整数帧，
    # 音视频都按这个吸附后的长度走 —— 30fps / 44100Hz 下一帧正好 1470 个采样，
    # 两条轨能对到采样级。

    def frames_of(self, clip: Clip) -> int:
        return max(1, int(round(clip.seconds * self.video.fps)))

    def seconds_of(self, clip: Clip) -> float:
        """这一句在成片里真正占的时长（已吸附到整帧）。"""
        return round(self.frames_of(clip) / self.video.fps, 6)

    @property
    def duration(self) -> float:
        return round(sum(self.frames_of(c) for c in self.clips) / self.video.fps, 6)

    def timeline(self) -> list[tuple[Clip, float, float]]:
        """[(clip, start, end)]，帧级确定。转场不改变它。"""
        fps = self.video.fps
        out, f = [], 0
        for c in self.clips:
            n = self.frames_of(c)
            out.append((c, round(f / fps, 6), round((f + n) / fps, 6)))
            f += n
        return out

    def shots_of(self, clip: Clip) -> list[tuple[Visual, float, float]]:
        """把本句时长分给各镜头：[(镜头, 句内起点, 时长)]。

        规则：写了 seconds 的镜头按写的来，剩下的时间由没写的平分。
        边界同样吸附到帧，并且保证首尾相接、加起来正好是本句的帧数。
        """
        fps = self.video.fps
        n = len(clip.visuals)
        total = max(n, self.frames_of(clip))        # 每个镜头至少留 1 帧

        fixed = sum(v.seconds for v in clip.visuals if v.seconds)
        free = [v for v in clip.visuals if not v.seconds]
        each = max(0.0, self.seconds_of(clip) - fixed) / len(free) if free else 0.0

        bounds, t = [0], 0.0
        for i, v in enumerate(clip.visuals, start=1):
            t += v.seconds if v.seconds else each
            # 单调递增、每段至少 1 帧，并给后面的镜头留够位置
            bounds.append(max(bounds[-1] + 1, min(int(round(t * fps)), total - (n - i))))
        bounds[-1] = total

        return [
            (v, round(bounds[i] / fps, 6), round((bounds[i + 1] - bounds[i]) / fps, 6))
            for i, v in enumerate(clip.visuals)
        ]

    def transition_after(self, index: int) -> Transition | None:
        """第 index 句到下一句的转场，时长已按前后两句夹紧。

        转场吃的是上一句画面的延长部分，太长会盖住整句，所以限制在
        min(本句, 下一句) 的一半以内。
        """
        if index >= len(self.clips) - 1:
            return None
        t = self.clips[index].transition
        if not t:
            return None
        fps = self.video.fps
        cap = min(self.seconds_of(self.clips[index]), self.seconds_of(self.clips[index + 1])) / 2
        seconds = min(t.duration, max(1 / fps, cap))
        frames = max(1, int(round(seconds * fps)))       # 转场也按整帧走
        return Transition(t.type, round(frames / fps, 6))

    def clip(self, clip_id: str) -> Clip:
        for c in self.clips:
            if c.id == clip_id:
                return c
        raise ProjectError(f"没有 id 为 {clip_id!r} 的片段")

    def new_clip_id(self) -> str:
        used = {c.id for c in self.clips}
        i = len(self.clips) + 1
        while f"c{i}" in used:
            i += 1
        return f"c{i}"

    def validate(self) -> list[str]:
        """返回问题列表（空 = 可以渲染）。缺素材算错误，节奏问题只在界面提示。"""
        problems: list[str] = []
        if not self.clips:
            problems.append("工程里一个片段都没有")
        for i, c in enumerate(self.clips, 1):
            for j, v in enumerate(c.visuals, 1):
                where = f"第 {i} 句（{c.id}）" + (f"第 {j} 个镜头" if len(c.visuals) > 1 else "")
                if v.type in ("image", "video") and not self.exists(v.path):
                    problems.append(f"{where}的素材找不到：{v.path}")
            fixed = sum(v.seconds for v in c.visuals if v.seconds)
            # 容差给一帧：时长吸附到帧之后，差个零点零几秒是正常的，不该当成错
            if fixed > self.seconds_of(c) + 1 / self.video.fps:
                problems.append(
                    f"第 {i} 句（{c.id}）的镜头时长加起来 {fixed:.2f}s，"
                    f"超过了本句的 {self.seconds_of(c):.2f}s"
                )
            if c.audio and c.audio.path and not self.exists(c.audio.path):
                problems.append(f"第 {i} 句（{c.id}）的配音找不到：{c.audio.path}")
        if self.style.avatar and not self.exists(self.style.avatar):
            problems.append(f"头像找不到：{self.style.avatar}")
        if self.music and not self.exists(self.music.path):
            problems.append(f"背景音乐找不到：{self.music.path}")
        return problems


def blank(title: str = "未命名") -> Project:
    return Project(title=title, clips=[Clip(id="c1", text="")])
