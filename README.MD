# FluffyCut

> **Cut the fluff.**

**一个本地运行的竖屏短视频编辑器 —— 为「拒绝废话」式知识类 Shorts 而做。**

不是「输入主题，AI 帮你生成一个 mp4」。
是「你在编辑器里做片子，AI 在旁边帮你写台词、配图、配音」。

编辑权始终在你手上。

---

## 这是什么

做 YouTube Shorts / 抖音这类快节奏知识短视频时，真正花时间的从来不是创意，而是重复劳动：把脚本切成短句、给每句配画面、对字幕时间轴、套固定模板、导出 9:16。

FluffyCut 把这套流程固化成一个本地工具：

- 一列句子就是一条时间轴，改文字即改片子
- 模板写死在配置里，每期只改内容不改排版
- TTS 生成配音，字幕时间轴自动跟着音频走
- 一键渲染出 1080×1920 成品

所有计算在本机完成，不上传素材，不依赖服务器。

## 特性

| | |
|---|---|
| **句子级编辑** | 时间轴以「一句台词 = 一个片段」为单位，不用拖拽 |
| **节奏可视化** | 每句的时长条一眼看出哪里拖沓（经验值：1.5–3 秒/句） |
| **模板化渲染** | 顶部标题带、底部品牌带、角色头像固定复用 |
| **确定性输出** | 同一份 `project.json` 每次渲染逐帧一致 |
| **AI 辅助（可选）** | 主题拆句、配图 prompt 生成 —— 只给建议，不自动执行 |
| **纯本地** | ffmpeg + 系统 TTS，零服务器成本 |

## 技术栈

- **渲染**：Python + ffmpeg（画面 cover/pad / Ken Burns 推近 / 图层 overlay），全程一次调用，不产中间视频
- **字幕**：PIL 预渲图层烧录，另可导出 `.ass` 供二次剪辑
- **模板层**：PIL 预渲带 alpha 的 PNG，中文排版比 `drawtext` 好控制
- **界面**：FastAPI + 静态前端，浏览器打开 localhost
- **配音**：macOS 系统 `say`，可替换为任意 TTS API
- **AI**：可插拔，自带 API key

## 环境要求

- macOS（Linux 理论可用，模板字体需自行调整）
- Python 3.11+
- ffmpeg：`brew install ffmpeg`

> 字幕不依赖 libass。系统 ffmpeg 没编译 `--enable-libass` 也能正常出片 —— 字幕和模板一样，
> 是 PIL 预渲的透明图层，由 ffmpeg `overlay` 贴上去。好处是中文换行、描边、圆角底框全部可控，
> 而且**界面预览和成片走同一段合成代码**，所见即所得是结构保证，不是靠对参数。

## 快速开始

```bash
git clone https://github.com/<you>/fluffycut.git
cd fluffycut

python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

# 渲染自带的示例工程（无需配音，时长写死在 project.json 里）
python -m core.render projects/demo/project.json -o out.mp4

# 给示例配上系统 TTS 的声音，时长自动回写
python -m core.tts projects/demo/project.json --stale
python -m core.render projects/demo/project.json     # 输出到 projects/demo/out.mp4

# 启动编辑界面
python -m server.main
# 浏览器打开 http://localhost:8000
```

想用 AI 面板就设一个 key（不设也不影响其他功能，面板会自动置灰）：

```bash
export ANTHROPIC_API_KEY=sk-ant-...
```

## 命令行

| 命令 | 作用 |
|---|---|
| `python -m core.render <工程> [-o out.mp4]` | 渲染成片 |
| `python -m core.render <工程> --frame 3.5` | 导出第 3.5 秒的单帧，用来核对排版 |
| `python -m core.render <工程> --dry-run` | 只打印 ffmpeg 命令，方便排查滤镜图 |
| `python -m core.tts <工程> --stale` | 只补配音缺失或与台词不一致的句子 |
| `python -m core.tts <工程> --list-voices` | 列出系统音色 |
| `python -m core.subtitle <工程>` | 导出 `.ass` 字幕 |
| `python -m server.main [--port 8000]` | 启动编辑界面 |
| `python -m unittest discover -s tests` | 跑回归测试（不碰 ffmpeg，一秒跑完） |

## 编辑界面

一屏两栏：左边是**句子列表即时间轴**，右边是预览与工具。

- 改文字就是改片子，时长条实时跟着变；轨道底纹标出 1.5–3 秒的甜区，条子超出去就是拖沓
- 顶部一条节奏总览，整片哪里堆料一眼看得见
- 每句可单独换配图、单独配音；台词改了而配音没重配，会挂「配音已过期」
- 预览帧由服务端用渲染同一套代码合成，不是 CSS 模拟
- 「通读」按顺序播配音 + 切帧，用来听节奏
- 编辑防抖自动存盘，⌘S 立刻存；⌘↵ 在当前句下面插一句
- AI 面板只给建议，点了「采用」才会写进 `project.json`

## 工程格式

一个片子就是一个 `project.json`，人类可读、可手改、可进 git：

```json
{
  "title": "没文化到底有多可怕?",
  "style": {
    "font": "PingFang SC",
    "title_color": "#FFE100",
    "brand_text": "拒绝废话",
    "avatar": "assets/avatar.png"
  },
  "clips": [
    {
      "id": "c1",
      "text": "冰雹一般出现在什么季节?",
      "visual": { "type": "image", "path": "assets/1.png" },
      "audio":  { "path": "assets/1.aiff", "duration": 2.4 },
      "kenburns": true
    }
  ]
}
```

字段说明：

| 字段 | 说明 |
|---|---|
| `clips[].text` | 这一句的台词，同时也是字幕内容 |
| `clips[].duration` | 显式时长（秒）。不写就用配音时长，再没有就按字数估（约 5.5 字/秒） |
| `clips[].audio.duration` | 配音真实时长，由 TTS 回写 |
| `clips[].audio.text_sha` | 合成时的台词指纹。台词改了对不上，界面就提示配音已过期 |
| `clips[].visual.fit` | `cover`（裁切铺满，默认）或 `contain`（留边） |
| `clips[].visual.prompt` | 配图 prompt，留档可复现，AI 生成的也写在这里 |
| `clips[].kenburns` | `true` / `"in"` / `"out"`。`true` 时方向由 `id` 哈希决定，同一工程每次一致 |
| `style.layout` | 覆盖模板布局的任意数值（字号、边距、字幕位置…），按画面高度自动等比缩放 |

渲染器是 `project.json → mp4` 的纯函数。界面只是这个文件的编辑器，AI 只是往这个文件里写字段的工具之一。这条约束保证了每一层都能单独验证。

同一份 `project.json` 渲两次，输出字节完全一致 —— 没有随机数，Ken Burns 的方向也来自 `id` 的哈希。

## 项目结构

```
core/                  渲染管线，纯函数，不依赖界面
  project.py           project.json 的读写与校验，时间轴推导
  layers.py            模板层 / 字幕层 / 画面层的 PIL 合成（预览与成片共用）
  render.py            把工程翻译成一条 ffmpeg 命令并执行
  tts.py               配音与时长回写，引擎可替换
  subtitle.py          ASS 导出
  ai.py                拆句 / 配图 prompt / 标题，可插拔
  placeholder.py       程序生成的占位配图（不下载任何第三方素材）
  fonts.py media.py    字体解析、ffmpeg 封装
server/                FastAPI 后端 + 静态前端
projects/<名字>/       一个片子一个目录：project.json + assets/
tests/                 回归测试
```

## 路线图

- [x] 渲染器：project.json → 1080×1920 mp4
- [x] 字幕烧录（PIL 图层）+ ASS 导出
- [x] 句子列表编辑界面
- [x] TTS 配音 + 时长回写
- [x] LLM 主题拆句
- [x] 配图 prompt 生成（全局风格前缀）
- [ ] 转场与背景音乐
- [ ] 词级字幕高亮（whisper forced alignment）
- [ ] 素材库搜索接入
- [ ] Tauri 打包 `.app`

## 关于素材

本工具不提供、不下载任何第三方素材。请使用你自己拍摄、生成或已获授权的内容。

## License

MIT
