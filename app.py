"""FluffyCut 桌面入口：双击 .app 走的就是这里。

做三件事：把工程目录准备好 -> 在本机起一个只监听 127.0.0.1 的服务 -> 开一个窗口指过去。
没有 pywebview（或者加了 --browser）就退回用系统默认浏览器打开，功能完全一样。
"""

from __future__ import annotations

import argparse
import os
import shutil
import socket
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

APP_NAME = "FluffyCut"
DEFAULT_PROJECTS = Path.home() / "Movies" / APP_NAME   # 视频工程放 Movies 才合规矩


def resource_dir() -> Path:
    """打包后资源在 _MEIPASS 里，开发时就是仓库根目录。"""
    return Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))


def prepare_projects(projects: Path) -> Path:
    """首次启动时把自带示例复制到用户的工程目录，免得打开是一片空白。"""
    projects.mkdir(parents=True, exist_ok=True)
    if any(projects.glob("*/project.json")):
        return projects
    seed = resource_dir() / "projects" / "demo"
    if seed.is_dir():
        # 渲染产物不用带过去，用户自己渲一次就有了
        shutil.copytree(seed, projects / "demo", dirs_exist_ok=True,
                        ignore=shutil.ignore_patterns("out.mp4", "*.ass", "*.tmp", "frame.png"))
    return projects


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def wait_until_up(url: str, timeout: float = 25.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=1):
                return True
        except (urllib.error.URLError, ConnectionError, OSError):
            time.sleep(0.15)
    return False


def serve(port: int) -> threading.Thread:
    import uvicorn

    from server.main import app as fastapi_app

    def run() -> None:
        uvicorn.run(fastapi_app, host="127.0.0.1", port=port, log_level="warning")

    t = threading.Thread(target=run, daemon=True)
    t.start()
    return t


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog=APP_NAME, description="FluffyCut 桌面版")
    ap.add_argument("--projects", default=os.environ.get("FLUFFYCUT_PROJECTS"),
                    help=f"工程目录（默认 {DEFAULT_PROJECTS}）")
    ap.add_argument("--port", type=int, default=0, help="0 = 自动挑一个空闲端口")
    ap.add_argument("--browser", action="store_true", help="用系统浏览器打开，不开原生窗口")
    ap.add_argument("--headless", action="store_true", help="只起服务不开窗口（调试用）")
    args = ap.parse_args(argv)

    projects = prepare_projects(Path(args.projects).expanduser() if args.projects else DEFAULT_PROJECTS)
    os.environ["FLUFFYCUT_PROJECTS"] = str(projects)     # server.main 在 import 时读它

    port = args.port or free_port()
    url = f"http://127.0.0.1:{port}/"
    serve(port)
    if not wait_until_up(url + "api/projects"):
        print("服务没起来，看看上面的报错。", file=sys.stderr)
        return 1

    print(f"{APP_NAME} 已启动：{url}\n工程目录：{projects}")

    if args.headless:
        threading.Event().wait()
        return 0

    if not args.browser:
        try:
            import webview

            window = webview.create_window(
                APP_NAME, url, width=1440, height=920, min_size=(1080, 680),
                background_color="#0e0f12",
            )
            webview.start()          # 窗口关掉就返回，守护线程里的服务跟着退出
            return 0
        except Exception as e:       # noqa: BLE001 —— 开不出窗口就退回浏览器，别让人卡在这
            print(f"开不出原生窗口（{e}），改用浏览器。", file=sys.stderr)

    import webbrowser

    webbrowser.open(url)
    print("窗口已交给浏览器。关掉这个终端就会退出。")
    threading.Event().wait()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
