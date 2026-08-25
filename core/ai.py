"""AI 辅助：主题拆句 / 配图 prompt 生成 / 标题建议。

三条铁律（对应 README 的「只给建议，不自动执行」）：
  1. 这里的函数只返回建议，写不写进 project.json 由界面上的人决定；
  2. 没配 API key 时整个工具照常可用，AI 面板降级为不可点；
  3. 输出走 structured outputs，拿到的就是能直接用的 JSON，不做正则抠字符串。

换别的模型/服务：实现同样的函数签名即可，调用方只认返回值。
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import settings
from .project import Project

DEFAULT_MODEL = "claude-opus-5"
MAX_TOKENS = 8000

SYSTEM = """你是短视频脚本编辑，服务于一个叫「拒绝废话」的知识类竖屏短视频账号。
风格要求：
- 每句独立成立，短、硬、口语化，能被单独念出来当一个镜头的台词。
- 开头第一句必须是钩子：一个反直觉的问题或断言，不许铺垫。
- 不写"大家好""今天我们来聊聊""废话不多说"这类套话。
- 不用书面语连接词（因此、然而、综上所述），用口语（所以、但是）。
- 每句 8–22 个字，念出来 1.5–3 秒。
- 结尾一句收束，不喊口号，不求关注。"""


class AIError(RuntimeError):
    pass


@dataclass
class Suggestion:
    """一条 AI 建议。界面显示 items，人点了「采用」才会落到工程里。"""

    kind: str
    items: list[Any]
    raw: dict[str, Any]


def model() -> str:
    """用哪个模型：环境变量 > 本机配置 > 默认。"""
    return os.environ.get("FLUFFYCUT_MODEL") or settings.get("model") or DEFAULT_MODEL


def credential() -> tuple[str | None, str]:
    """(key, 来源说明)。key 为 None 表示交给 SDK 自己解析或者根本没有。

    顺序：环境变量 > 界面上填的（~/.config/fluffycut/config.json）> `ant auth login` 的 profile。
    双击启动的 .app 读不到环境变量，中间那条就是为它准备的。
    """
    if os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN"):
        return None, "环境变量"
    stored = settings.get("anthropic_api_key")
    if stored:
        return stored, "本机配置"
    if (Path.home() / ".config" / "anthropic").is_dir():
        return None, "ant 登录态"
    return None, ""


def available() -> tuple[bool, str]:
    """(能不能用, 原因)。界面据此决定 AI 面板是灰的还是亮的。"""
    try:
        import anthropic  # noqa: F401
    except ImportError:
        return False, "没装 anthropic 包：pip install anthropic"
    _key, source = credential()
    if not source:
        return False, "还没填 API key，AI 功能已关闭（其余功能不受影响）"
    return True, model()


def _client():
    ok, why = available()
    if not ok:
        raise AIError(why)
    import anthropic

    key, _source = credential()
    return anthropic.Anthropic(api_key=key) if key else anthropic.Anthropic()


def _ask(prompt: str, schema: dict[str, Any], effort: str = "medium") -> dict[str, Any]:
    """一次结构化请求。schema 保证返回的第一个文本块就是合法 JSON。"""
    client = _client()          # 没装包 / 没 key 会在这里抛 AIError，先于下面的 import
    import anthropic

    try:
        resp = client.messages.create(
            model=model(),
            max_tokens=MAX_TOKENS,
            system=SYSTEM,
            messages=[{"role": "user", "content": prompt}],
            output_config={"format": {"type": "json_schema", "schema": schema}, "effort": effort},
        )
    except anthropic.APIStatusError as e:
        raise AIError(f"Claude API 返回 {e.status_code}：{e.message}") from e
    except anthropic.APIConnectionError as e:
        raise AIError(f"连不上 Claude API：{e}") from e
    if resp.stop_reason == "refusal":
        raise AIError("模型拒绝了这个请求，换个说法试试")
    text = next((b.text for b in resp.content if b.type == "text"), "")
    try:
        return json.loads(text)
    except json.JSONDecodeError as e:
        raise AIError(f"模型返回的不是 JSON：{text[:200]}") from e


# ---------------------------------------------------------------- 拆句

_SPLIT_SCHEMA = {
    "type": "object",
    "properties": {
        "title": {"type": "string", "description": "视频标题，疑问句或反直觉断言，不超过 16 字"},
        "sentences": {
            "type": "array",
            "items": {"type": "string"},
            "description": "按顺序排列的台词，一句一条",
        },
    },
    "required": ["title", "sentences"],
    "additionalProperties": False,
}


def split_script(topic: str, count: int = 8, draft: str = "") -> Suggestion:
    """主题（或一段草稿）-> 一句一条的台词列表 + 标题建议。"""
    if not topic.strip() and not draft.strip():
        raise AIError("主题和草稿不能都是空的")
    parts = [f"主题：{topic.strip()}" if topic.strip() else "",
             f"已有草稿（按同样的意思重写成短句，不要新增事实）：\n{draft.strip()}" if draft.strip() else "",
             f"写 {count} 句左右（可以 ±2 句），保证事实准确，不确定的事实宁可不写。"]
    data = _ask("\n\n".join(p for p in parts if p), _SPLIT_SCHEMA)
    sentences = [s.strip() for s in data.get("sentences", []) if s and s.strip()]
    return Suggestion("split", sentences, data)


# ---------------------------------------------------------------- 配图 prompt

_PROMPT_SCHEMA = {
    "type": "object",
    "properties": {
        "style_prefix": {"type": "string", "description": "整片统一的画面风格前缀"},
        "prompts": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "id": {"type": "string"},
                    "prompt": {"type": "string", "description": "不含风格前缀的画面描述"},
                },
                "required": ["id", "prompt"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["style_prefix", "prompts"],
    "additionalProperties": False,
}


def image_prompts(project: Project, style_hint: str = "", only_missing: bool = True) -> Suggestion:
    """给每句配一条画面 prompt，外加一个整片统一的风格前缀。

    只产出文字 prompt —— 出图交给你自己的工具，本工具不下载任何素材。
    """
    targets = [c for c in project.clips if c.text.strip()
               and not (only_missing and c.visual.prompt)]
    if not targets:
        raise AIError("没有需要生成 prompt 的句子")
    lines = "\n".join(f"{c.id}: {c.text}" for c in targets)
    hint = f"\n风格倾向：{style_hint.strip()}" if style_hint.strip() else ""
    prompt = (
        f"视频标题：{project.title}\n\n台词逐句如下：\n{lines}\n\n"
        f"为每一句写一条竖屏 9:16 的配图 prompt（英文），要求：\n"
        f"- 画面直给，一眼看懂，别用抽象隐喻；\n"
        f"- 不要出现文字、字幕、logo、水印；\n"
        f"- 不要出现真实人物姓名或品牌；\n"
        f"- 另外给一个整片通用的 style_prefix，保证六张图像一套的。{hint}"
    )
    data = _ask(prompt, _PROMPT_SCHEMA)
    prefix = (data.get("style_prefix") or "").strip()
    items = []
    valid = {c.id for c in targets}
    for row in data.get("prompts", []):
        if row.get("id") in valid:
            full = f"{prefix}, {row['prompt']}" if prefix else row["prompt"]
            items.append({"id": row["id"], "prompt": full})
    return Suggestion("image_prompts", items, data)


# ---------------------------------------------------------------- 标题

_TITLE_SCHEMA = {
    "type": "object",
    "properties": {"titles": {"type": "array", "items": {"type": "string"}}},
    "required": ["titles"],
    "additionalProperties": False,
}


def titles(project: Project, count: int = 5) -> Suggestion:
    body = "\n".join(c.text for c in project.clips if c.text.strip())
    if not body:
        raise AIError("工程里还没有台词")
    data = _ask(
        f"下面是一条短视频的全部台词：\n{body}\n\n给 {count} 个标题备选，"
        f"每个不超过 16 字，必须制造好奇或反直觉，不用感叹号堆情绪。",
        _TITLE_SCHEMA, effort="low",
    )
    return Suggestion("titles", [t.strip() for t in data.get("titles", []) if t.strip()], data)
