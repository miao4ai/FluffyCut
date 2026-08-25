"""本机配置：存 API key 这类跟工程无关、又不该进 git 的东西。

为什么需要它：双击启动的 .app 不走 shell 的 rc 文件，`export ANTHROPIC_API_KEY=...`
它一个字都看不到。所以得有个地方让人把 key 填进去、下次还在。

放 ~/.config/fluffycut/config.json，权限 0600。**明文存的** —— 和多数命令行工具一样，
安全边界是你的用户账户。不放进 project.json：那个文件是要进 git 的。
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any


def config_dir() -> Path:
    return Path(os.environ.get("FLUFFYCUT_CONFIG_DIR")
                or Path.home() / ".config" / "fluffycut")


def config_file() -> Path:
    return config_dir() / "config.json"


def load() -> dict[str, Any]:
    path = config_file()
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text("utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}
    return data if isinstance(data, dict) else {}


def save(data: dict[str, Any]) -> Path:
    path = config_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", "utf-8")
    tmp.chmod(0o600)                 # 先改权限再就位，中间不留一个可读的窗口
    tmp.replace(path)
    return path


def get(key: str, default: Any = None) -> Any:
    return load().get(key, default)


def put(key: str, value: Any) -> Path:
    """写一个字段。value 为 None 或空串就是删掉它。"""
    data = load()
    if value in (None, ""):
        data.pop(key, None)
    else:
        data[key] = value
    return save(data)


def mask(secret: str | None) -> str:
    """给界面看的样子：sk-ant-…7f3a。绝不把原文发回浏览器。"""
    if not secret:
        return ""
    s = secret.strip()
    if len(s) <= 12:
        return "…" + s[-4:]
    return f"{s[:7]}…{s[-4:]}"
