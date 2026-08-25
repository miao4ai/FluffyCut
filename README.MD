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

- 读入一个参考片，扒出它的节奏，生成一份同节奏的工程骨架
- 一列句子就是一条时间轴，改文字即改片子
- 一句话里能切多个镜头，视频素材可以裁出入点出点、变速
- 句与句之间可以加转场，但时间轴一帧都不动
- 模板写死在配置里，每期只改内容不改排版
- TTS 生成配音，字幕时间轴自动跟着音频走
- 背景音乐一放，说话时自动压低
- 一键渲染出 1080×1920 成品

所有计算在本机完成，不上传素材，不依赖服务器。macOS 下可以打包成双击就用的 `.app`。

## 特性

| | |
|---|---|
| **拆解参考片** | 读入 mp4，扒出每句多长、几秒切一次、语速多少，并生成同节奏的骨架 |
| **句子级编辑** | 时间轴以「一句台词 = 一个片段」为单位，不用拖拽 |
| **镜头级剪辑** | 一句话内可切多个镜头；视频素材裁入出点、变速、素材不够长自动定格 |
| **转场不动时间轴** | 转场吃的是上一句画面额外多渲的一截，加不加，字幕位置一帧不差 |
| **节奏可视化** | 每句的时长条一眼看出哪里拖沓（经验值：1.5–3 秒/句） |
| **模板化渲染** | 顶部标题带、底部品牌带、角色头像固定复用 |
| **背景音乐 + 闪避** | 有人说话就自动把音乐压下去，说完抬回来 |
| **确定性输出** | 同一份 `project.json` 每次渲染逐字节一致 |
| **AI 辅助（可选）** | 主题拆句、配图 prompt 生成 —— 只给建议，不自动执行 |
| **纯本地** | ffmpeg + 系统 TTS，零服务器成本 |

## 技术栈

- **渲染**：Python + ffmpeg（画面 cover/pad / Ken Burns 推近 / 图层 overlay），全程一次调用，不产中间视频
- **字幕**：PIL 预渲图层烧录，另可导出 `.ass` 供二次剪辑
- **模板层**：PIL 预渲带 alpha 的 PNG，中文排版比 `drawtext` 好控制
- **界面**：FastAPI + 零依赖静态前端；桌面版用 pywebview 套一个原生窗口
- **配音**：macOS 系统 `say`，可替换为任意 TTS API
- **AI**：可插拔，自带 API key
- **打包**：PyInstaller 打成 `FluffyCut.app`

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

# 或者直接开桌面版（原生窗口，端口自动挑）
python app.py
```

想要双击就能用的 `.app`：

```bash
pip install pyinstaller pywebview
python packaging/build_app.py        # 产物在 dist/FluffyCut.app
```

打好的 `.app` 拖进「应用程序」即可。工程默认存在 `~/Movies/FluffyCut`，首次启动会把
示例复制过去。它仍然需要系统里有 ffmpeg（`brew install ffmpeg`）；想把 ffmpeg 一起塞进包里，
用 `FLUFFYCUT_BUNDLE_FFMPEG=1 python packaging/build_app.py`（注意 ffmpeg 是 GPL，
随包分发要遵守它的许可）。未签名的 app 首次打开可能被 Gatekeeper 拦下，右键 →「打开」。

### AI 的 key 填在哪

不填也不影响其他功能，AI 面板会自动置灰。要用就三选一，优先级从高到低：

1. **界面上填** —— 右侧「AI 助手」面板里有个输入框，粘进去点保存。
   存在 `~/.config/fluffycut/config.json`（明文，权限 600），下次自动读。
   **桌面版只有这条路能走**：双击启动的 `.app` 不经过 shell，`.zshrc` 里 export 的
   环境变量它一个字都看不到 —— 和 ffmpeg 找不到是同一类问题。
2. **环境变量** —— 从源码跑的时候方便：

   ```bash
   export ANTHROPIC_API_KEY=sk-ant-...
   ```

3. **`ant auth login`** 落在 `~/.config/anthropic/` 的 profile，SDK 会自己认。

key 永远不会写进 `project.json` —— 那个文件是要进 git 的。界面上也只显示掩码
（`sk-ant-…7f3a`），原文不回传浏览器。想换模型：`export FLUFFYCUT_MODEL=claude-sonnet-5`，
或者往上面那个 config.json 里写 `"model"`。

## 命令行

| 命令 | 作用 |
|---|---|
| `python -m core.render <工程> [-o out.mp4]` | 渲染成片 |
| `python -m core.render <工程> --frame 3.5` | 导出第 3.5 秒的单帧，用来核对排版 |
| `python -m core.render <工程> --dry-run` | 只打印 ffmpeg 命令，方便排查滤镜图 |
| `python -m core.tts <工程> --stale` | 只补配音缺失或与台词不一致的句子 |
| `python -m core.tts <工程> --list-voices` | 列出系统音色 |
| `python -m core.subtitle <工程>` | 导出 `.ass` 字幕 |
| `python -m core.analyze <参考片.mp4>` | 拆解参考片的节奏 |
| `python -m core.analyze <参考片.mp4> --transcribe --project <目录>` | 连台词一起扒，并生成同节奏的骨架 |
| `python -m server.main [--port 8000]` | 启动编辑界面（浏览器） |
| `python app.py [--browser]` | 桌面版；`--browser` 退回系统浏览器 |
| `python scripts/make_demo_assets.py` | 重建示例工程缺的素材（都是程序生成的） |
| `ffmpeg -i 参考片.mp4 -vn -c:a aac bgm.m4a` | 命令行抽音轨（界面里选视频当配乐是同一件事） |
| `python packaging/build_app.py` | 打包成 `dist/FluffyCut.app` |
| `python -m unittest discover -s tests` | 跑回归测试（不碰 ffmpeg，一秒跑完） |

## 编辑界面

一屏两栏：左边是**句子列表即时间轴**，右边是预览与工具。

- 改文字就是改片子，时长条实时跟着变；轨道底纹标出 1.5–3 秒的甜区，条子超出去就是拖沓
- 顶部一条节奏总览，整片哪里堆料一眼看得见
- 每句下面是**镜头条**：缩略图、各自占多久，加镜头 / 换素材 / 删镜头都在这儿；
  选中的镜头是视频时，露出入点、出点、倍速三个数字
- 两句之间是**接缝控件**：默认硬切，选个转场就能直接改时长
- 每句可单独配音、单独调音量；台词改了而配音没重配，会挂「配音已过期」
- 配乐面板管背景音乐：音量、淡入淡出、起点、要不要闪避。
  **选文件时给个视频也行** —— 参考片的配乐常常就藏在视频里，后端自动把音轨抽出来
- 预览帧由服务端用渲染同一套代码合成，不是 CSS 模拟；切到哪个镜头就预览哪一格
- 「通读」按顺序播配音 + 切帧，用来听节奏
- 「＋ 新片子」开一条新的：填标题就自动生成目录名，也可以直接「从参考片开始」
- 编辑防抖自动存盘，⌘S 立刻存；⌘↵ 在当前句下面插一句；Esc 关掉对话框
- AI 面板只给建议，点了「采用」才会写进 `project.json`

## 学习一个参考片

看到一条节奏好的片子，最该抄的不是画面，是**节奏** —— 一句话平均几秒、几秒切一次镜头、
停顿留在哪儿。界面上点「学」，或者：

```bash
python -m core.analyze 参考片.mp4                              # 只看数字
python -m core.analyze 参考片.mp4 --transcribe --project 仿-某片   # 连台词一起扒，生成骨架
```

出来是这样：

```
参考片：reference.mp4
  总长 13.2 秒，5 句（mlx-whisper 转写）
  每句平均 2.51 秒，中位数 2.56 秒 —— 在甜区里
  人声占 95%，其余是停顿和纯画面
  镜头切了 5 次，约 22.7 次/分钟
  语速约 4.6 字/秒
```

生成的骨架里，**一句 = 一个片段，时长照抄参考片**；参考片在某句话中间切了几刀，
那个片段就带几个镜头，长度也照抄。每句的 `note` 记着它对应参考片的哪一段。
填上自己的内容，就是自己的片子。

分两层，下面一层不需要任何额外依赖：

| 层 | 靠什么 | 装不装得起 |
|---|---|---|
| 镜头切点、语音段、节奏指标 | ffmpeg 的 scene 检测 + `silencedetect` | 有 ffmpeg 就行 |
| 台词文字 | whisper | 可选，没装则骨架里台词是空的，节奏照样对 |

> 切点检测跑两遍：ffmpeg 的 scene 判定看的是亮度，两个镜头亮度接近、只有颜色不同时会
> 整刀漏掉，所以再拿色度平面跑一遍补上。实测纯色切换 5 刀里亮度只抓到 1 刀、色度 5 刀全中，
> 而在一直在动、没有真实切点的素材上两遍都是 0 误报。慢转场（叠化、推拉）仍然会漏。

转写引擎按「装了哪个用哪个」探测：`mlx-whisper`（Apple Silicon 最快）→ `faster-whisper`
→ `openai-whisper` → `whisper-cli` 二进制。**打包成 `.app` 时故意没把 ML 那一坨打进去**
（会让安装包从 100 MB 涨到 1.5 GB），所以 `.app` 里默认只拆节奏；想要台词就
`brew install whisper-cpp`，它会自动认出来 —— 和 ffmpeg 一样是从系统里找。

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
  "music": { "path": "assets/bgm.m4a", "volume": 0.18, "duck": true },
  "clips": [
    {
      "id": "c1",
      "text": "冰雹一般出现在什么季节?",
      "visual": { "type": "image", "path": "assets/1.png" },
      "audio":  { "path": "assets/1.aiff", "duration": 2.4 },
      "kenburns": true,
      "transition": { "type": "fade", "duration": 0.35 }
    },
    {
      "id": "c2",
      "text": "错。冰雹的旺季是夏天。",
      "visual": [
        { "type": "video", "path": "assets/storm.mp4", "in": 12.5, "out": 14.0 },
        { "type": "image", "path": "assets/hail.png", "seconds": 0.8 }
      ]
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
| `clips[].audio.gain` | 这一句配音的音量倍数，默认 1.0 |
| `clips[].audio.text_sha` | 合成时的台词指纹。台词改了对不上，界面就提示配音已过期 |
| `clips[].visual` | 一个对象 = 一个镜头；写成数组就是这句话里连着切几个镜头 |
| `clips[].visual[].type` | `image` / `video` / `color` |
| `clips[].visual[].in` `out` | 视频素材的入点、出点（秒）。**这就是剪辑最核心的两个数字** |
| `clips[].visual[].speed` | 变速，`2.0` = 两倍速。素材不够长就定格最后一帧，不会黑屏 |
| `clips[].visual[].seconds` | 这个镜头在本句里占多久。不写的镜头平分剩下的时间 |
| `clips[].visual[].fit` | `cover`（裁切铺满，默认）或 `contain`（留边） |
| `clips[].visual[].prompt` | 配图 prompt，留档可复现，AI 生成的也写在这里 |
| `clips[].kenburns` | `true` / `"in"` / `"out"`。`true` 时方向由 `id` 哈希决定，同一工程每次一致 |
| `clips[].transition` | 到**下一句**的转场，`"fade"` 或 `{ "type": ..., "duration": ... }` |
| `music` | 背景音乐：`volume` / `fade_in` / `fade_out` / `duck` / `start` |
| `style.layout` | 覆盖模板布局的任意数值（字号、边距、字幕位置…），按画面高度自动等比缩放 |

渲染器是 `project.json → mp4` 的纯函数。界面只是这个文件的编辑器，AI 只是往这个文件里写字段的工具之一。这条约束保证了每一层都能单独验证。

### 时间轴的不变量

**成片总长恒等于各句时长之和。** 转场和多镜头都不改变这一条：

- 转场吃的是上一句画面**额外多渲的那一截**，不占时间轴，所以加不加转场，
  每句字幕和配音的位置一帧都不会动；
- 每句时长先吸附到整数帧再分给各镜头，音视频按同一个长度走。
  30fps / 44100Hz 下一帧正好 1470 个采样，两条轨对得到采样级 ——
  不这么做的话，画面按整帧裁、配音按秒裁，每句差半帧，几十句下来就是肉眼可见的不同步。

同一份 `project.json` 渲两次，输出字节完全一致 —— 没有随机数，Ken Burns 的方向来自 `id` 的哈希，
连音乐闪避这种带状态的滤镜也固定了帧长，把不确定性摁掉了。

## 项目结构

```
core/                  渲染管线，纯函数，不依赖界面
  project.py           project.json 的读写与校验，时间轴与分镜推导
  layers.py            模板层 / 字幕层 / 画面层的 PIL 合成（预览与成片共用）
  render.py            把工程翻译成一条 ffmpeg 命令并执行
  tts.py               配音与时长回写，引擎可替换
  subtitle.py          ASS 导出
  ai.py                拆句 / 配图 prompt / 标题，可插拔
  analyze.py           拆解参考片：切点、语音段、转写、同节奏骨架
  placeholder.py       程序生成的占位配图（不下载任何第三方素材）
  settings.py          本机配置（AI key 等），不进工程文件
  fonts.py media.py    字体解析、ffmpeg 封装
server/                FastAPI 后端 + 静态前端
app.py                 桌面版入口：起服务 + 开原生窗口
packaging/             PyInstaller 配置、图标生成、打包脚本
scripts/               示例素材重建
projects/<名字>/       一个片子一个目录：project.json + assets/
  demo/                最朴素的六句话
  edit-demo/           覆盖多镜头、入出点、变速、定格、转场、背景音乐
tests/                 回归测试
```

## 路线图

- [x] 渲染器：project.json → 1080×1920 mp4
- [x] 字幕烧录（PIL 图层）+ ASS 导出
- [x] 句子列表编辑界面
- [x] TTS 配音 + 时长回写
- [x] LLM 主题拆句
- [x] 配图 prompt 生成（全局风格前缀）
- [x] 视频素材入出点、变速、一句多镜头
- [x] 片段转场（不改变时间轴）
- [x] 背景音乐 + 人声闪避
- [x] 打包 `.app`（PyInstaller + pywebview）
- [x] 读入参考片，拆节奏 + 扒台词 + 生成同节奏骨架
- [x] 配乐可以直接从视频里抽
- [ ] 词级字幕高亮（whisper forced alignment）
- [ ] 素材库搜索接入
- [ ] 波形视图与更细的音频剪辑

## 关于素材

本工具不提供、不下载任何第三方素材。请使用你自己拍摄、生成或已获授权的内容。

从视频里抽音轨这个功能同理：抽出来的音乐**版权不会因为换了个容器就变成你的**。
拿别人片子里的配乐去发布，和直接用原曲是一回事。自己的录像、买断/授权的曲库、
CC0 素材、或者只是抽出来私下研究节奏，这些才是它该用的地方。

## License

MIT
