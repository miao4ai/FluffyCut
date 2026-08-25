"""FluffyCut 编辑器后端。

界面只做一件事：编辑 project.json。所有真正的动作（渲染、配音、AI）都是调用 core 里的
纯函数，然后把结果写回同一个文件。所以随时可以关掉浏览器，改用命令行接着干。

跑起来：python -m server.main  然后打开 http://localhost:8000
"""

from __future__ import annotations

import argparse
import os
import tempfile
import threading
import traceback
import uuid
from pathlib import Path
from typing import Any

from fastapi import Body, FastAPI, HTTPException, Query, UploadFile, File
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles

from core import ai, layers, media, placeholder, render, subtitle, tts
from core import project as project_module
from core.project import Clip, Project, ProjectError, Visual, blank

ROOT = Path(__file__).resolve().parent.parent
PROJECTS_DIR = Path(os.environ.get("FLUFFYCUT_PROJECTS", ROOT / "projects")).resolve()
STATIC_DIR = Path(__file__).resolve().parent / "static"

app = FastAPI(title="FluffyCut", docs_url="/api/docs", openapi_url="/api/openapi.json")


# ---------------------------------------------------------------- 基础设施


class Job:
    """一次渲染任务。进度和日志都留在内存里，够用。"""

    def __init__(self, kind: str, project: str) -> None:
        self.id = uuid.uuid4().hex[:12]
        self.kind = kind
        self.project = project
        self.state = "running"          # running | done | error
        self.progress = 0.0
        self.note = ""
        self.error = ""
        self.out: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.id, "kind": self.kind, "project": self.project,
            "state": self.state, "progress": round(self.progress, 4),
            "note": self.note, "error": self.error, "out": self.out,
        }


JOBS: dict[str, Job] = {}


def load(name: str) -> Project:
    """按工程名加载，并挡住 ../ 之类的路径穿越。"""
    safe = Path(name).name
    if not safe or safe != name:
        raise HTTPException(400, "工程名不合法")
    path = PROJECTS_DIR / safe / "project.json"
    if not path.exists():
        raise HTTPException(404, f"没有这个工程：{name}")
    try:
        return Project.load(path)
    except ProjectError as e:
        raise HTTPException(400, str(e)) from e


def inside(project: Project, rel: str) -> Path:
    """把工程内相对路径解析成绝对路径，越界的一律拒绝。"""
    base = project.root.resolve()
    target = (base / rel).resolve()
    if not (target == base or base in target.parents):
        raise HTTPException(403, "只能访问工程目录内的文件")
    return target


def view(project: Project, name: str) -> dict[str, Any]:
    """给界面的完整状态：原始工程 + 派生信息 + 能力探测。"""
    timeline = project.timeline()
    derived = {
        "name": name,
        "duration": project.duration,
        "problems": project.validate(),
        "clips": [
            {
                "id": c.id,
                "start": start,
                "seconds": project.seconds_of(c),
                "pace": c.pace,
                "chars": len(c.text.strip()),
                "has_audio": bool(c.audio and c.audio.path),
                "audio_stale": bool(c.audio and c.audio.path and tts.is_stale(c)),
                "has_visual": all(v.type == "color" or project.exists(v.path) for v in c.visuals),
                "transition": (t.to_dict() if (t := project.transition_after(i)) else None),
                "shots": [
                    {
                        "index": j,
                        "type": v.type,
                        "path": v.path,
                        "seconds": dur,
                        "start": s0,
                        "fixed": v.seconds is not None,
                        "src_in": v.src_in,
                        "src_out": v.src_out,
                        "speed": v.speed,
                        "exists": v.type == "color" or project.exists(v.path),
                    }
                    for j, (v, s0, dur) in enumerate(project.shots_of(c))
                ],
            }
            for i, (c, start, _e) in enumerate(timeline)
        ],
        "transitions": list(project_module.TRANSITIONS),
    }
    ai_ok, ai_why = ai.available()
    return {
        "project": project.to_dict(),
        "derived": derived,
        "caps": {
            "ffmpeg": media.have("ffmpeg"),
            "libass": media.has_libass(),
            "tts": tts.get_engine("say").available(),
            "ai": ai_ok,
            "ai_note": ai_why,
        },
    }


@app.exception_handler(ProjectError)
async def _project_error(_req, exc: ProjectError):
    return JSONResponse({"detail": str(exc)}, status_code=400)


# ---------------------------------------------------------------- 工程


@app.get("/api/projects")
def list_projects() -> dict[str, Any]:
    items = []
    for d in sorted(PROJECTS_DIR.glob("*/project.json")):
        try:
            p = Project.load(d)
            items.append({"name": d.parent.name, "title": p.title,
                          "clips": len(p.clips), "duration": p.duration})
        except ProjectError:
            items.append({"name": d.parent.name, "title": "(读不出来)", "clips": 0, "duration": 0})
    return {"projects": items}


@app.post("/api/projects")
def create_project(payload: dict = Body(...)) -> dict[str, Any]:
    name = Path(str(payload.get("name", "")).strip()).name
    if not name:
        raise HTTPException(400, "要有工程名")
    target = PROJECTS_DIR / name
    if (target / "project.json").exists():
        raise HTTPException(409, "同名工程已存在")
    p = blank(str(payload.get("title") or name))
    p.style.brand_text = str(payload.get("brand") or "")
    p.save(target / "project.json")
    return view(p, name)


@app.get("/api/p/{name}")
def get_project(name: str) -> dict[str, Any]:
    return view(load(name), name)


@app.put("/api/p/{name}")
def put_project(name: str, payload: dict = Body(...)) -> dict[str, Any]:
    old = load(name)
    data = payload.get("project", payload)
    try:
        p = Project.from_dict(data, old.path)
    except ProjectError as e:
        raise HTTPException(400, str(e)) from e
    p.save()
    return view(p, name)


# ---------------------------------------------------------------- 素材


@app.get("/api/p/{name}/file/{rel:path}")
def get_file(name: str, rel: str) -> FileResponse:
    project = load(name)
    target = inside(project, rel)
    if not target.is_file():
        raise HTTPException(404, f"没有这个文件：{rel}")
    return FileResponse(target, headers={"Cache-Control": "no-cache"})


IMAGE_EXT = (".png", ".jpg", ".jpeg", ".webp")
VIDEO_EXT = (".mp4", ".mov", ".m4v")
AUDIO_EXT = (".mp3", ".m4a", ".aac", ".wav", ".aiff", ".aif", ".caf", ".flac")


@app.post("/api/p/{name}/upload")
async def upload(name: str, clip: str = Query(...), shot: int = Query(0),
                 append: bool = Query(False), file: UploadFile = File(...)) -> dict[str, Any]:
    """给某一句的某个镜头换素材；append=true 则在这句话后面加一个新镜头。"""
    project = load(name)
    target = project.clip(clip)
    ext = Path(file.filename or "").suffix.lower() or ".png"
    if ext not in IMAGE_EXT + VIDEO_EXT:
        raise HTTPException(400, f"不支持的素材格式：{ext}")

    slot = len(target.visuals) if append else max(0, min(shot, len(target.visuals) - 1))
    rel = f"assets/{clip}_{slot}{ext}" if slot or append else f"assets/{clip}{ext}"
    dest = inside(project, rel)
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(await file.read())

    v = Visual(type="video" if ext in VIDEO_EXT else "image", path=rel)
    if append:
        target.visuals.append(v)
    else:
        v.seconds = target.visuals[slot].seconds       # 保留这个镜头原来的时长安排
        target.visuals[slot] = v
    project.save()
    return view(project, name)


@app.post("/api/p/{name}/upload_music")
async def upload_music(name: str, file: UploadFile = File(...),
                       start: float = Query(0.0), end: float | None = Query(None)) -> dict[str, Any]:
    """设背景音乐。给视频文件也行 —— 自动把里面的音轨抽出来。

    start/end 可以只取素材中间的一段（秒）。
    """
    project = load(name)
    ext = Path(file.filename or "").suffix.lower()
    if ext not in AUDIO_EXT + VIDEO_EXT:
        raise HTTPException(400, f"不支持的格式：{ext}（音频或视频都行）")

    trimming = start > 0 or end is not None
    if ext in VIDEO_EXT or trimming:
        # 视频要抽轨、音频要裁段，都先落到临时文件再交给 ffmpeg
        with tempfile.NamedTemporaryFile(suffix=ext, delete=False) as tmp:
            tmp.write(await file.read())
            raw = Path(tmp.name)
        rel = "assets/bgm.m4a"
        try:
            media.extract_audio(raw, inside(project, rel), start, end)
        except media.MediaError as e:
            # 报错里要出现用户认得的文件名，不是临时文件名
            raise HTTPException(422, str(e).replace(raw.name, file.filename or raw.name)) from e
        finally:
            raw.unlink(missing_ok=True)
    else:
        rel = f"assets/bgm{ext}"
        dest = inside(project, rel)
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(await file.read())

    project.music = project_module.Music.from_dict(
        {**(project.music.to_dict() if project.music else {}), "path": rel, "start": 0.0}
    )
    project.save()
    return view(project, name)


@app.get("/api/p/{name}/thumb")
def thumb(name: str, path: str = Query(...), w: int = Query(160)) -> Response:
    """素材缩略图。图片直接缩，视频抽首帧 —— 界面上的镜头条要靠它。"""
    import io

    project = load(name)
    src = inside(project, path)
    if not src.is_file():
        raise HTTPException(404, "素材不存在")
    from PIL import Image

    if src.suffix.lower() in VIDEO_EXT:
        import subprocess

        exe = media.tool("ffmpeg")
        if not exe:
            raise HTTPException(503, "找不到 ffmpeg，没法给视频抽帧")
        out = subprocess.run(
            [exe, "-v", "error", "-ss", "0", "-i", str(src), "-frames:v", "1",
             "-f", "image2pipe", "-vcodec", "png", "-"],
            capture_output=True, timeout=30,
        ).stdout
        if not out:
            raise HTTPException(422, "这个视频抽不出帧")
        img = Image.open(io.BytesIO(out))
    else:
        img = Image.open(src)
    img = img.convert("RGB")
    img.thumbnail((max(40, min(w, 480)), 2000))
    buf = io.BytesIO()
    img.save(buf, "JPEG", quality=80)
    return Response(buf.getvalue(), media_type="image/jpeg",
                    headers={"Cache-Control": "no-cache"})


@app.get("/api/p/{name}/probe")
def probe_asset(name: str, path: str = Query(...)) -> dict[str, Any]:
    """素材信息。剪视频要知道它到底多长。"""
    project = load(name)
    src = inside(project, path)
    if not src.is_file():
        raise HTTPException(404, "素材不存在")
    return {"path": path, "duration": media.duration(src)}


@app.post("/api/p/{name}/placeholder")
def make_placeholder(name: str, payload: dict = Body(default={})) -> dict[str, Any]:
    """给还没配图的句子生成占位图，让时间轴先跑起来。"""
    project = load(name)
    ids = payload.get("clips")
    targets = [c for c in project.clips if ids is None or c.id in set(ids)]
    if payload.get("only_missing", True):
        targets = [c for c in targets
                   if not (project.resolve(c.visual.path) or Path("/nope")).exists()]
    for c in targets:
        rel = f"assets/{c.id}.png"
        placeholder.make(inside(project, rel), c.visual.prompt or c.text,
                         seed_text=c.id + c.text,
                         size=(project.video.width, project.video.height))
        c.visual.type = "image"
        c.visual.path = rel
    project.save()
    return view(project, name)


@app.get("/api/p/{name}/frame")
def frame(name: str, clip: str = Query(...), t: float = Query(0.0)) -> Response:
    """预览帧：和成片同一套 PIL 合成代码，所见即所得。"""
    import io

    project = load(name)
    img = layers.compose_frame(project, project.clip(clip), t)
    img.thumbnail((540, 960))
    buf = io.BytesIO()
    img.save(buf, "JPEG", quality=88)
    return Response(buf.getvalue(), media_type="image/jpeg",
                    headers={"Cache-Control": "no-store"})


# ---------------------------------------------------------------- 配音


@app.get("/api/voices")
def voices() -> dict[str, Any]:
    return {"voices": tts.get_engine("say").voices(), "default": tts.DEFAULT_VOICE}


@app.post("/api/p/{name}/tts")
def do_tts(name: str, payload: dict = Body(default={})) -> dict[str, Any]:
    project = load(name)
    try:
        done = tts.synth_project(
            project,
            clip_ids=payload.get("clips"),
            voice=payload.get("voice") or tts.DEFAULT_VOICE,
            rate=int(payload.get("rate") or tts.DEFAULT_RATE),
            only_stale=bool(payload.get("only_stale", False)),
        )
    except tts.TTSError as e:
        raise HTTPException(400, str(e)) from e
    project.save()
    out = view(project, name)
    out["done"] = done
    return out


# ---------------------------------------------------------------- 渲染


@app.post("/api/p/{name}/render")
def start_render(name: str, payload: dict = Body(default={})) -> dict[str, Any]:
    project = load(name)
    problems = project.validate()
    if problems:
        raise HTTPException(400, "；".join(problems))
    job = Job("render", name)
    JOBS[job.id] = job
    out = project.root / str(payload.get("out") or "out.mp4")
    opts = render.RenderOptions(crf=int(payload.get("crf") or 18),
                                preset=str(payload.get("preset") or "medium"))

    def work() -> None:
        try:
            render.render(project, out, opts,
                          on_progress=lambda p, note: (setattr(job, "progress", p),
                                                       setattr(job, "note", note)))
            job.out = project.relativize(out)
            job.state = "done"
            job.progress = 1.0
        except Exception as e:                      # noqa: BLE001 —— 任何失败都要能在界面上看到
            job.state = "error"
            job.error = str(e)
            traceback.print_exc()

    threading.Thread(target=work, daemon=True).start()
    return job.as_dict()


@app.get("/api/jobs/{job_id}")
def job_status(job_id: str) -> dict[str, Any]:
    job = JOBS.get(job_id)
    if not job:
        raise HTTPException(404, "没有这个任务")
    return job.as_dict()


@app.post("/api/p/{name}/ass")
def export_ass(name: str) -> dict[str, Any]:
    project = load(name)
    out = subtitle.write_ass(project, project.root / "subtitles.ass")
    return {"out": project.relativize(out)}


# ---------------------------------------------------------------- AI（只给建议）


@app.post("/api/ai/split")
def ai_split(payload: dict = Body(...)) -> dict[str, Any]:
    try:
        s = ai.split_script(str(payload.get("topic", "")),
                            int(payload.get("count") or 8),
                            str(payload.get("draft", "")))
    except ai.AIError as e:
        raise HTTPException(400, str(e)) from e
    return {"kind": s.kind, "items": s.items, "title": s.raw.get("title", "")}


@app.post("/api/p/{name}/ai/prompts")
def ai_prompts(name: str, payload: dict = Body(default={})) -> dict[str, Any]:
    project = load(name)
    try:
        s = ai.image_prompts(project, str(payload.get("style_hint", "")),
                             bool(payload.get("only_missing", True)))
    except ai.AIError as e:
        raise HTTPException(400, str(e)) from e
    return {"kind": s.kind, "items": s.items, "style_prefix": s.raw.get("style_prefix", "")}


@app.post("/api/p/{name}/ai/titles")
def ai_titles(name: str) -> dict[str, Any]:
    project = load(name)
    try:
        s = ai.titles(project)
    except ai.AIError as e:
        raise HTTPException(400, str(e)) from e
    return {"kind": s.kind, "items": s.items}


# ---------------------------------------------------------------- 静态界面

app.mount("/", StaticFiles(directory=STATIC_DIR, html=True), name="static")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m server.main", description="FluffyCut 编辑器")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8000)
    ap.add_argument("--reload", action="store_true")
    args = ap.parse_args(argv)

    import uvicorn

    PROJECTS_DIR.mkdir(parents=True, exist_ok=True)
    print(f"工程目录：{PROJECTS_DIR}")
    print(f"打开 http://{args.host}:{args.port}")
    uvicorn.run("server.main:app" if args.reload else app,
                host=args.host, port=args.port, reload=args.reload, log_level="warning")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
