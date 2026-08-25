"""重新生成两个示例工程的素材。

本工具不提供、不下载任何第三方素材（见 README），示例里的图和视频全是程序画出来的：
    - 配图：core.placeholder 的确定性渐变图
    - 头像：黄底单字
    - 剪辑示例用的视频/音乐：ffmpeg 的 testsrc2 / sine

素材丢了或者想换一批，跑一次就行：
    python scripts/make_demo_assets.py
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core import media, placeholder            # noqa: E402
from core.project import Project               # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
PROJECTS = ROOT / "projects"


def make_images(project: Project) -> None:
    """给每个镜头补一张占位图（已经有真实素材的不动）。"""
    placeholder.make_avatar(project.resolve("assets/avatar.png"), "拒")
    for c in project.clips:
        for j, v in enumerate(c.visuals):
            if v.type != "image" or not v.path:
                continue
            dest = project.resolve(v.path)
            if dest and dest.exists():
                continue
            placeholder.make(dest, v.prompt or c.text, seed_text=f"{c.id}:{j}:{c.text}",
                             size=(project.video.width, project.video.height))
            print(f"  配图 {v.path}")


def make_synthetic_media(project: Project) -> None:
    """剪辑示例需要一段视频和一段音乐 —— 用 ffmpeg 现场合成，别去外面下。"""
    ffmpeg = media.tool("ffmpeg")
    if not ffmpeg:
        print("  没有 ffmpeg，跳过视频/音乐生成")
        return

    clip = project.resolve("assets/clip.mp4")
    if clip and not clip.exists():
        subprocess.run([ffmpeg, "-v", "error", "-y",
                        "-f", "lavfi", "-i", "testsrc2=size=540x960:rate=24:duration=12",
                        "-c:v", "libx264", "-preset", "medium", "-crf", "34",
                        "-pix_fmt", "yuv420p", str(clip)], check=True)
        print(f"  测试视频 assets/clip.mp4（{clip.stat().st_size / 1e3:.0f} KB）")

    bgm = project.resolve("assets/bgm.m4a")
    if bgm and not bgm.exists():
        subprocess.run([ffmpeg, "-v", "error", "-y",
                        "-f", "lavfi", "-i", "sine=frequency=220:duration=6:sample_rate=44100",
                        "-c:a", "aac", "-b:a", "96k", str(bgm)], check=True)
        print("  测试音乐 assets/bgm.m4a")


def main() -> int:
    for path in sorted(PROJECTS.glob("*/project.json")):
        project = Project.load(path)
        print(f"{path.parent.name}:")
        make_images(project)
        if project.music or any(v.type == "video" for c in project.clips for v in c.visuals):
            make_synthetic_media(project)
        problems = project.validate()
        print("  " + ("素材齐了" if not problems else "还缺：" + "；".join(problems)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
