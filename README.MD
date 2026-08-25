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

- **渲染**：Python + ffmpeg（画面 pad / Ken Burns 推近 / 图层 overlay）
- **字幕**：ASS 格式烧录，原生支持描边与背景框
- **模板层**：PIL 预渲带 alpha 的 PNG，中文排版比 `drawtext` 好控制
- **界面**：FastAPI + 静态前端，浏览器打开 localhost
- **配音**：macOS 系统 `say`，可替换为任意 TTS API
- **AI**：可插拔，自带 API key

## 环境要求

- macOS（Linux 理论可用，模板字体需自行调整）
- Python 3.11+
- ffmpeg：`brew install ffmpeg`

## 快速开始

```bash
git clone https://github.com/<you>/fluffycut.git
cd fluffycut
pip install -r requirements.txt

# 渲染自带的示例工程
python -m core.render projects/demo/project.json -o out.mp4

# 启动编辑界面
python -m server.main
# 浏览器打开 http://localhost:8000
```

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

渲染器是 `project.json → mp4` 的纯函数。界面只是这个文件的编辑器，AI 只是往这个文件里写字段的工具之一。这条约束保证了每一层都能单独验证。

## 路线图

- [x] 渲染器：project.json → 1080×1920 mp4
- [x] ASS 字幕烧录
- [ ] 句子列表编辑界面
- [ ] TTS 配音 + 时长回写
- [ ] LLM 主题拆句
- [ ] 配图 prompt 生成（全局风格前缀）
- [ ] 词级字幕高亮（whisper forced alignment）
- [ ] 素材库搜索接入
- [ ] Tauri 打包 `.app`

## 关于素材

本工具不提供、不下载任何第三方素材。请使用你自己拍摄、生成或已获授权的内容。

## License

MIT
