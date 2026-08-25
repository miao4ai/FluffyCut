/* FluffyCut 编辑器前端。
 *
 * 只有一个真相来源：服务端的 project.json。这里持有它的一份副本，改完防抖回写，
 * 服务端再把派生信息（每句时长、起点、节奏、配音是否过期、素材是否缺失）算好送回来。
 * 前端不自己算真时长，只在打字时给一个即时估算，省得等一次往返。
 */

const $ = (sel) => document.querySelector(sel);
const api = async (path, opts = {}) => {
  const res = await fetch(path, {
    headers: { "Content-Type": "application/json" },
    ...opts,
  });
  const body = res.headers.get("content-type")?.includes("json") ? await res.json() : await res.text();
  if (!res.ok) throw new Error(body?.detail || body || res.statusText);
  return body;
};

const state = {
  name: null,
  project: null,
  derived: null,
  caps: {},
  selected: 0,
  shot: 0,
  viewMode: "focus",
  playhead: 0,
  refPlayhead: 0,
  refSelected: 0,
  pxPerSec: 60,
  compareOn: false,
  saveTimer: null,
  playing: false,
};

// 与 core/project.py 的 estimate_duration 保持一致：只用于打字时的即时反馈
const PACE_MIN = 1.5, PACE_MAX = 3.0;
function estimate(text) {
  const n = (text || "").replace(/\s+/g, "").length;
  if (!n) return 1.0;
  return Math.min(20, Math.max(1.0, Math.round((n / 5.5 + 0.35) * 100) / 100));
}
const paceOf = (s) => (s < PACE_MIN ? "fast" : s > PACE_MAX ? "slow" : "ok");

function toast(msg, bad = false, ms = 0) {
  const el = $("#toast");
  el.textContent = msg;
  el.className = "toast" + (bad ? " bad" : "");
  clearTimeout(toast.t);
  toast.t = setTimeout(() => el.classList.add("hidden"), ms || (bad ? 6000 : 2600));
}

/* 通用的后台任务轮询：渲染有自己的进度条，其它任务用这个就够 */
function pollJob(id, onTick) {
  return new Promise((resolve, reject) => {
    const tick = async () => {
      try {
        const job = await api(`/api/jobs/${id}`);
        if (job.state === "running") {
          onTick?.(job);
          return setTimeout(tick, 700);
        }
        job.state === "error" ? reject(new Error(job.error)) : resolve(job);
      } catch (e) {
        reject(e);
      }
    };
    tick();
  });
}

/* ---------------------------------------------------------------- 载入 */

async function loadProjects() {
  const { projects } = await api("/api/projects");
  const sel = $("#project-select");
  sel.innerHTML = projects
    .map((p) => `<option value="${p.name}">${p.name} · ${p.clips}句</option>`)
    .join("");
  if (!projects.length) return null;
  const want = new URLSearchParams(location.search).get("p");
  const name = projects.find((p) => p.name === want)?.name || projects[0].name;
  sel.value = name;
  return name;
}

async function open(name) {
  // 参数要在 replaceState 改写 URL **之前**读完，否则下面全读不到了
  const params = new URLSearchParams(location.search);
  const wanted = +(params.get("clip") ?? 0);
  const wantNew = params.has("new");
  const view = params.get("view");             // ?view=list / focus，方便分享与排查
  if (view === "list" || view === "focus") state.viewMode = view;   // 要在 apply 之前设


  apply(await api(`/api/p/${name}`), name);
  history.replaceState(null, "", `?p=${name}`);
  if (wanted > 0) select(wanted);          // ?clip=2 直接定位到第 3 句，方便分享/排查
  if (wantNew) openNewDialog();
  const panel = params.get("open");            // ?open=music 直接展开某个面板
  if (panel) document.getElementById(`panel-${panel}`)?.setAttribute("open", "");
}

function apply(data, name = state.name) {
  state.name = name;
  state.project = data.project;
  state.derived = data.derived;
  state.caps = data.caps;
  state.selected = Math.min(state.selected, state.project.clips.length - 1);
  paintAll();
}

/* ---------------------------------------------------------------- 绘制 */

function paintAll() {
  const p = state.project;
  $("#title").value = p.title || "";
  $("#st-font").value = p.style.font || "";
  $("#st-title-color").value = (p.style.title_color || "#FFE100").slice(0, 7);
  $("#st-brand").value = p.style.brand_text || "";
  $("#st-avatar").value = p.style.avatar || "";
  paintList();
  paintDerived();
  paintPreview();
  paintMusic();
  paintReference();
  paintTimeline();
  // 导进来的工程默认摆上原片对照 —— 「照着原片改」正是它的用法
  if (state.derived?.compare?.auto && !paintTimeline.autoCompared) {
    paintTimeline.autoCompared = true;
    state.compareOn = true;
    $("#tl-compare").checked = true;
  }
  paintPreviewAt(state.playhead);

  paintCaps();
}

function paintCaps() {
  const c = state.caps;
  $("#caps-note").textContent =
    `ffmpeg ${c.ffmpeg ? "✓" : "✗"} · 系统 TTS ${c.tts ? "✓" : "✗"} · AI ${c.ai ? "✓" : "✗"}` +
    (c.libass ? " · libass ✓" : " · 无 libass（字幕走 PIL 图层，不影响成片）");
  $("#ai-note").textContent = c.ai ? `${c.ai_source} · ${c.ai_model}` : c.ai_note || "";
  $("#ai-body").classList.toggle("off", !c.ai);
  paintAIKey();
}

/* 两种看法：
   focus —— 只显示时间指针所在的那一句。片子一长，把每句的素材、入出点、裁切全摊开
            既看不过来也拖慢渲染；要看哪句就把指针挪过去。
   list  —— 全部列出。通篇改台词、调顺序的时候这个更快。 */
function paintList() {
  const list = $("#clip-list");
  const d = state.derived;
  const clips = state.project.clips;
  const focus = state.viewMode === "focus";

  const rows = focus
    ? (clips[state.selected]
        ? clipRow(clips[state.selected], state.selected, d?.clips?.[state.selected])
          + cutRow(state.selected, d?.clips?.[state.selected])
        : "")
    : clips.map((c, i) => clipRow(c, i, d?.clips?.[i]) + cutRow(i, d?.clips?.[i])).join("");

  list.innerHTML = rows;
  list.querySelectorAll("textarea").forEach(grow);

  $("#focus-pos").textContent = `第 ${state.selected + 1} 句 / 共 ${clips.length}`;
  $("#btn-viewmode").textContent = focus ? "全部列出" : "只看当前句";
  $("#focus-bar").classList.toggle("dim", !focus);
  markSelected();
}

function setViewMode(mode) {
  state.viewMode = mode;
  try {
    localStorage.setItem("fluffycut.view", mode);
  } catch { /* 隐私模式下写不了，无所谓 */ }
  paintList();
  paintDerived();
}

/* 上一句 / 下一句：把指针也带过去，预览和时间轴跟着动 */
function step(delta) {
  const i = Math.max(0, Math.min(state.project.clips.length - 1, state.selected + delta));
  select(i, 0);
  const dc = state.derived?.clips?.[i];
  if (dc) scrub(TRACKS.edit, dc.start + 0.02);
}

function clipRow(c, i, dc) {
  return `
    <div class="clip" data-i="${i}">
      <div class="idx" data-act="select">${i + 1}</div>
      <div>
        <textarea rows="1" data-act="text" placeholder="这一句要说什么？">${escapeHtml(c.text || "")}</textarea>
        <div class="shots">
          ${(dc?.shots || []).map((sh) => shotChip(c, i, sh)).join("")}
          <button class="shot add" data-act="shot-add" title="在这句里再加一个镜头">＋镜头</button>
        </div>
        ${trimRow(c, i, dc)}
        ${c.note ? `<div class="note">${escapeHtml(c.note)}</div>` : ""}
        <div class="clip-meta">
          <span class="dur-bar" data-bar><i></i></span>
          <span class="dur" data-dur></span>
          <span class="tag hidden" data-tag-audio></span>
          <span class="tag hidden" data-tag-visual>缺配图</span>
          <span class="clip-actions">
            <button data-act="hear" title="试听这一句（空格）">▶</button>
            <button data-act="tts" title="用 TTS 给这句配音">配</button>
            <button data-act="up" title="上移">↑</button>
            <button data-act="down" title="下移">↓</button>
            <button data-act="dup" title="复制">⧉</button>
            <button data-act="del" title="删除">✕</button>
          </span>
        </div>
      </div>
    </div>`;
}

/* 一个镜头 = 一块缩略图。点它选中，双击换素材。 */
function shotChip(c, i, sh) {
  const on = state.selected === i && state.shot === sh.index;
  // 视频镜头要显示自己入点那一帧，不然同一条片子切出来的镜头长得一模一样
  const thumb = sh.exists && sh.path
    ? `<img loading="lazy" src="/api/p/${state.name}/thumb?path=${encodeURIComponent(sh.path)}&w=120&t=${(sh.src_in || 0).toFixed(2)}">`
    : `<span class="ph">${sh.type === "color" ? "纯色" : "缺素材"}</span>`;
  const badge = sh.type === "video"
    ? `<em title="取素材 ${sh.src_in}s 起">✂ ${sh.src_in.toFixed(1)}s${sh.speed !== 1 ? " ×" + sh.speed : ""}</em>`
    : "";
  return `
    <button class="shot${on ? " on" : ""}" data-act="shot" data-j="${sh.index}" title="${escapeHtml(sh.path || sh.type)}">
      ${thumb}${badge}
      <b>${sh.seconds.toFixed(2)}s</b>
      <i data-act="shot-del" data-j="${sh.index}" title="删掉这个镜头">✕</i>
    </button>`;
}

/* 选中的镜头是视频时，露出入出点/倍速 —— 剪辑最核心的三个数字 */
function trimRow(c, i, dc) {
  if (state.selected !== i) return "";
  const sh = (dc?.shots || [])[state.shot];
  if (!sh) return "";
  const isVideo = sh.type === "video";
  return `
    <div class="trim">
      <span class="lab">镜头 ${sh.index + 1}</span>
      ${isVideo ? `
        <label>入点<input type="number" step="0.1" min="0" value="${sh.src_in}" data-act="src-in"></label>
        <label>出点<input type="number" step="0.1" min="0" value="${sh.src_out ?? ""}" placeholder="到结尾" data-act="src-out"></label>
        <label>倍速<input type="number" step="0.25" min="0.25" value="${sh.speed}" data-act="speed"></label>
        <span class="hint" data-srclen>素材 …</span>` : ""}
      <label>占时长<input type="number" step="0.1" min="0.05" value="${sh.fixed ? sh.seconds : ""}" placeholder="平分" data-act="shot-seconds"></label>
      <button class="ghost" data-act="shot-replace">换素材</button>
    </div>
    ${sh.type === "color" ? "" : `
    <div class="trim crop">
      <span class="lab" title="按比例裁掉素材的四边。原片烧死的字幕就是这么去掉的">裁切 %</span>
      ${["top", "bottom", "left", "right"].map((k) => `
        <label>${{ top: "上", bottom: "下", left: "左", right: "右" }[k]}
          <input type="number" step="1" min="0" max="90" data-act="crop-${k}"
                 value="${Math.round((sh.crop?.[k] || 0) * 100) || ""}" placeholder="0"></label>`).join("")}
      <span class="hint">原片自带的字幕是像素，改不了，只能裁掉</span>
    </div>`}`;
}

/* 两句之间的接缝：默认硬切，选了转场就在这里改时长 */
function cutRow(i, dc) {
  if (i >= state.project.clips.length - 1) return "";
  const t = dc?.transition;
  const opts = ["", ...(state.derived?.transitions || [])]
    .map((v) => `<option value="${v}" ${t?.type === v || (!t && !v) ? "selected" : ""}>${transLabel(v)}</option>`)
    .join("");
  return `
    <div class="cut${t ? " on" : ""}" data-i="${i}">
      <select data-act="trans">${opts}</select>
      ${t ? `<input type="number" step="0.05" min="0.1" max="3" value="${t.duration}" data-act="trans-dur"><span>秒</span>` : ""}
    </div>`;
}

const TRANS_LABELS = {
  "": "— 硬切 —", fade: "淡入淡出", fadeblack: "转黑", fadewhite: "转白", dissolve: "叠化",
  slideleft: "左滑", slideright: "右滑", slideup: "上滑", slidedown: "下滑",
  wipeleft: "左擦除", wiperight: "右擦除", circleopen: "圆开", circleclose: "圆合",
  smoothleft: "平滑左移", radial: "径向",
};
const transLabel = (v) => TRANS_LABELS[v] || v;

/* 时长条 / 节奏 / 徽标 —— 这些每次编辑都要刷，但不重建 DOM，免得输入框失焦 */
function paintDerived() {
  const d = state.derived;
  const clips = state.project.clips;
  const seconds = clips.map((c, i) => localSeconds(c, i));
  const total = seconds.reduce((a, b) => a + b, 0);
  const longest = Math.max(3, ...seconds);

  $("#stat-clips").textContent = clips.length;
  $("#stat-duration").textContent = total.toFixed(1);

  document.querySelectorAll(".clip").forEach((row) => {
    const i = +row.dataset.i;
    const s = seconds[i];
    const pace = paceOf(s);
    const bar = row.querySelector("[data-bar]");
    bar.className = `dur-bar pace-${pace}`;
    bar.style.setProperty("--ok-a", `${(PACE_MIN / longest) * 100}%`);
    bar.style.setProperty("--ok-b", `${(PACE_MAX / longest) * 100}%`);
    row.querySelector("[data-bar] i").style.width = `${(s / longest) * 100}%`;
    row.querySelector("[data-dur]").textContent = `${s.toFixed(2)}s`;
    const dc = d?.clips?.[i];
    const tagAudio = row.querySelector("[data-tag-audio]");
    const needsVoice = dc && clips[i].text.trim() && (!dc.has_audio || dc.audio_stale);
    tagAudio.classList.toggle("hidden", !needsVoice);
    tagAudio.classList.toggle("warn", !!(dc && dc.audio_stale));
    tagAudio.textContent = dc && dc.audio_stale ? "配音已过期" : "未配音";
    row.querySelector("[data-tag-visual]").classList.toggle("hidden", !(dc && !dc.has_visual));
  });

  $("#pace-strip").innerHTML = seconds
    .map((s, i) => `<i class="${paceOf(s)}" data-i="${i}" style="flex:${s}" title="${i + 1}. ${s.toFixed(2)}s"></i>`)
    .join("");

  const problems = d?.problems || [];
  $("#problems").classList.toggle("hidden", !problems.length);
  $("#problems").textContent = problems.join("　·　");
  markSelected();
}

function localSeconds(clip, i) {
  if (clip.duration != null) return clip.duration;
  const dc = state.derived?.clips?.[i];
  // 服务端算过、且台词没被改动，就用服务端的（含配音真实时长）
  if (dc && !dc.audio_stale) return dc.seconds;
  if (clip.audio?.duration && !state.derived?.clips?.[i]?.audio_stale) return clip.audio.duration + 0.35;
  return estimate(clip.text);
}

function markSelected() {
  document.querySelectorAll(".clip").forEach((r) => r.classList.toggle("on", +r.dataset.i === state.selected));
  document.querySelectorAll("#pace-strip i").forEach((r) => r.classList.toggle("on", +r.dataset.i === state.selected));
}

function paintPreview() {
  const c = state.project.clips[state.selected];
  if (!c) return;
  // 预览定位到当前镜头的起点，切镜头就能立刻看到那一格
  const dc = state.derived?.clips?.[state.selected];
  const sh = dc?.shots?.[state.shot];
  const t = sh ? sh.start + 0.02 : 0;
  if (dc) {
    state.playhead = dc.start + t;      // 编辑轴的播放头跟着选中的镜头走
    moveHead(TRACKS.edit);
  }
  $("#preview").src =
    `/api/p/${state.name}/frame?clip=${encodeURIComponent(c.id)}&t=${t.toFixed(2)}&v=${Date.now()}`;
  $("#preview-label").textContent =
    `第 ${state.selected + 1} 句 · ${c.id}` + (sh && (state.derived.clips[state.selected].shots.length > 1) ? ` · 镜头 ${state.shot + 1}` : "");
  if (sh?.type === "video" && sh.path) probeSource(sh.path);
}

/* 视频素材总长，用来提示入出点别超范围 */
async function probeSource(path) {
  const el = document.querySelector("[data-srclen]");
  if (!el) return;
  try {
    const r = await api(`/api/p/${state.name}/probe?path=${encodeURIComponent(path)}`);
    el.textContent = `素材共 ${r.duration.toFixed(1)}s`;
  } catch {
    el.textContent = "";
  }
}

const escapeHtml = (s) => s.replace(/[&<>]/g, (m) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;" }[m]));
const grow = (ta) => { ta.style.height = "auto"; ta.style.height = ta.scrollHeight + "px"; };

/* ---------------------------------------------------------------- 保存 */

function markDirty() {
  $("#save-state").textContent = "未保存…";
  $("#save-state").classList.add("dirty");
  clearTimeout(state.saveTimer);
  state.saveTimer = setTimeout(save, 700);
}

async function save({ repaintList = false } = {}) {
  clearTimeout(state.saveTimer);
  try {
    const data = await api(`/api/p/${state.name}`, {
      method: "PUT",
      body: JSON.stringify({ project: state.project }),
    });
    state.derived = data.derived;
    state.project = data.project;
    state.caps = data.caps;
    if (repaintList) paintList();
    paintDerived();
    $("#save-state").textContent = "已保存";
    $("#save-state").classList.remove("dirty");
  } catch (e) {
    $("#save-state").textContent = "保存失败";
    toast(e.message, true);
  }
}

/* ---------------------------------------------------------------- 片段操作 */

function select(i, shot = 0) {
  const changed = state.selected !== i || state.shot !== shot;
  state.selected = Math.max(0, Math.min(i, state.project.clips.length - 1));
  state.shot = shot;
  if (changed || state.viewMode === "focus") {
    paintList();                 // 聚焦模式下换的是"显示哪一句"，必须重画
    paintDerived();              // 重建 DOM 后要把时长条重新画上
  }
  markSelected();
  paintPreview();
}

const shotsOf = (c) => (Array.isArray(c.visual) ? c.visual : [c.visual]);
function setShots(c, arr) {
  c.visual = arr.length === 1 ? arr[0] : arr;
}

function newId() {
  const used = new Set(state.project.clips.map((c) => c.id));
  let i = state.project.clips.length + 1;
  while (used.has(`c${i}`)) i++;
  return `c${i}`;
}

async function addClip(at = state.project.clips.length, text = "") {
  state.project.clips.splice(at, 0, { id: newId(), text, visual: { type: "color", color: "#101014" } });
  await save({ repaintList: true });
  select(at);
  document.querySelector(`.clip[data-i="${at}"] textarea`)?.focus();
}

async function clipAction(i, act) {
  const clips = state.project.clips;
  switch (act) {
    case "select":
      return select(i);
    case "up":
      if (i === 0) return;
      [clips[i - 1], clips[i]] = [clips[i], clips[i - 1]];
      state.selected = i - 1;
      return save({ repaintList: true });
    case "down":
      if (i === clips.length - 1) return;
      [clips[i + 1], clips[i]] = [clips[i], clips[i + 1]];
      state.selected = i + 1;
      return save({ repaintList: true });
    case "dup":
      clips.splice(i + 1, 0, { ...structuredClone(clips[i]), id: newId(), audio: undefined });
      return save({ repaintList: true });
    case "del":
      if (clips.length === 1) return toast("至少留一句");
      clips.splice(i, 1);
      state.selected = Math.max(0, i - 1);
      return save({ repaintList: true });
    case "hear":
      return hearClip(i);
    case "tts":
      return runTTS({ clips: [clips[i].id] }, i);
  }
}

/* 换素材 / 加镜头都走这里：append=true 就是在这句话末尾加一个新镜头 */
async function pickMedia(i, shot = 0, append = false) {
  const clip = state.project.clips[i].id;
  const native = await pickNativePath("media");
  const file = native ? null : isDesktop() ? null : await pickUpload("image/*,video/mp4,video/quicktime");
  if (!native && !file) return;
  try {
    const data = native
      ? await api(`/api/p/${state.name}/import_path`, {
          method: "POST", body: JSON.stringify({ path: native, clip, shot, append }),
        })
      : await uploadFile(
          `/api/p/${state.name}/upload?clip=${encodeURIComponent(clip)}&shot=${shot}&append=${append}`,
          file);
    apply(data);
    select(i, append ? shotsOf(state.project.clips[i]).length - 1 : shot);
    toast(append ? "镜头已加上" : "素材已替换");
  } catch (e) {
    toast(e.message, true);
  }
}

async function delShot(i, j) {
  const c = state.project.clips[i];
  const arr = shotsOf(c);
  if (arr.length === 1) return toast("每句至少要有一个镜头");
  arr.splice(j, 1);
  setShots(c, arr);
  await save();
  select(i, Math.max(0, j - 1));
  paintList();
}

/* 改镜头上的某个字段（入点/出点/倍速/占时长），空值 = 删掉这个字段 */
async function setShotField(i, j, key, raw) {
  const arr = shotsOf(state.project.clips[i]);
  const v = arr[j];
  if (!v) return;
  const num = raw === "" || raw === null ? null : Number(raw);
  if (num !== null && Number.isNaN(num)) return;
  if (num === null) delete v[key];
  else v[key] = num;
  setShots(state.project.clips[i], arr);
  markDirty();
}

/* 裁切按百分比填，存成比例；填 0 就把这条边删掉，别在 json 里留一堆 0 */
function setShotCrop(i, j, side, percent) {
  const arr = shotsOf(state.project.clips[i]);
  const v = arr[j];
  if (!v) return;
  const frac = Math.max(0, Math.min(90, Number(percent) || 0)) / 100;
  const crop = { ...(v.crop || {}) };
  if (frac) crop[side] = Math.round(frac * 10000) / 10000;
  else delete crop[side];
  if (Object.keys(crop).length) v.crop = crop;
  else delete v.crop;
  setShots(state.project.clips[i], arr);
  markDirty();
}

async function setTransition(i, type, duration) {
  const c = state.project.clips[i];
  if (!type) delete c.transition;
  else c.transition = { type, duration: duration ?? c.transition?.duration ?? 0.35 };
  await save();
  paintList();
}

/* ---------------------------------------------------------------- 配音 / 渲染 */

async function runTTS(payload, playAfter = null) {
  const btn = $("#btn-tts");
  btn.disabled = true;
  btn.textContent = "配音中…";
  try {
    const data = await api(`/api/p/${state.name}/tts`, {
      method: "POST",
      body: JSON.stringify({
        voice: $("#voice").value,
        rate: +$("#rate").value,
        ...payload,
      }),
    });
    apply(data);
    if (!data.done.length) {
      // 一句都没配成，多半是台词是空的 —— 别回一句"配好了 0 句"糊弄人
      toast("没有可配的句子：台词是空的，先写上字", true);
    } else {
      toast(`配好了 ${data.done.length} 句，时长已回写`);
      if (playAfter !== null) hearClip(playAfter);   // 配完直接放一遍，省得再找按钮
    }
  } catch (e) {
    toast(e.message, true);
  } finally {
    btn.disabled = false;
    btn.textContent = "配音";
  }
}

async function startRender() {
  await save();
  const box = $("#render-box");
  const btn = $("#btn-render");
  try {
    const job = await api(`/api/p/${state.name}/render`, { method: "POST", body: "{}" });
    box.classList.remove("hidden");
    btn.disabled = true;
    $("#render-out").innerHTML = "";
    poll(job.id);
  } catch (e) {
    toast(e.message, true);
  }
}

async function poll(id) {
  try {
    const job = await api(`/api/jobs/${id}`);
    $("#render-bar").style.width = `${job.progress * 100}%`;
    $("#render-note").textContent = job.note || "准备中…";
    if (job.state === "running") return setTimeout(() => poll(id), 500);
    $("#btn-render").disabled = false;
    if (job.state === "error") {
      $("#render-note").textContent = "失败";
      return toast(job.error, true);
    }
    $("#render-note").textContent = "完成";
    $("#render-out").innerHTML =
      `<a href="/api/p/${state.name}/file/${job.out}" download>下载 ${job.out}</a>`;
    toast("渲染完成");
  } catch (e) {
    $("#btn-render").disabled = false;
    toast(e.message, true);
  }
}

/* 试听单句。声音可能来自 TTS，也可能是导入原片时切下来的那一段。 */
function hearClip(i) {
  const c = state.project.clips[i];
  if (!c) return;
  if (!c.audio?.path) {
    return toast(
      c.text.trim() ? "这句还没配音，点旁边的「配」" : "这句还没写台词，写完再点「配」",
      true);
  }
  const audio = $("#audio");
  if (!audio.paused && audio.dataset.clip === c.id) {
    audio.pause();
    return;
  }
  audio.src = `/api/p/${state.name}/file/${c.audio.path}?v=${Date.now()}`;
  audio.dataset.clip = c.id;
  audio.play().catch((e) => toast(`放不出来：${e.message}`, true));
}

/* ---------------------------------------------------------------- 通读播放 */

async function playThrough() {
  if (state.playing) {
    state.playing = false;
    $("#audio").pause();
    $("#btn-play").textContent = "▶ 通读";
    return;
  }
  state.playing = true;
  $("#btn-play").textContent = "■ 停";
  const audio = $("#audio");
  for (let i = state.selected; i < state.project.clips.length && state.playing; i++) {
    select(i);
    const c = state.project.clips[i];
    if (c.audio?.path) {
      audio.src = `/api/p/${state.name}/file/${c.audio.path}`;
      await new Promise((res) => {
        audio.onended = audio.onerror = res;
        audio.play().catch(res);
      });
    } else {
      await new Promise((res) => setTimeout(res, localSeconds(c, i) * 1000));
    }
  }
  state.playing = false;
  $("#btn-play").textContent = "▶ 通读";
}

/* ---------------------------------------------------------------- AI */

function aiBusy(btn, on) {
  btn.disabled = on;
  btn.dataset.label ||= btn.textContent;
  btn.textContent = on ? "…" : btn.dataset.label;
}

async function aiSplit() {
  const btn = $("#btn-ai-split");
  aiBusy(btn, true);
  try {
    const topic = $("#ai-topic").value;
    const r = await api("/api/ai/split", {
      method: "POST",
      body: JSON.stringify({ topic, draft: topic, count: +$("#ai-count").value }),
    });
    showSuggestion(
      `拆出 ${r.items.length} 句${r.title ? "，标题建议：" + r.title : ""}`,
      `<ol>${r.items.map((s) => `<li>${escapeHtml(s)}</li>`).join("")}</ol>`,
      [
        ["替换全部句子", () => applySentences(r.items, r.title, true)],
        ["追加到末尾", () => applySentences(r.items, "", false)],
      ]
    );
  } catch (e) {
    toast(e.message, true);
  } finally {
    aiBusy(btn, false);
  }
}

async function applySentences(items, title, replace) {
  if (replace) {
    state.project.clips = items.map((t, i) => ({
      id: `c${i + 1}`,
      text: t,
      visual: { type: "color", color: "#101014" },
      kenburns: true,
    }));
    if (title && !state.project.title) state.project.title = title;
  } else {
    for (const t of items) {
      state.project.clips.push({ id: newId(), text: t, visual: { type: "color", color: "#101014" }, kenburns: true });
    }
  }
  await save({ repaintList: true });
  paintAll();
  toast("已写入时间轴");
}

async function aiPrompts() {
  const btn = $("#btn-ai-prompts");
  aiBusy(btn, true);
  try {
    const r = await api(`/api/p/${state.name}/ai/prompts`, {
      method: "POST",
      body: JSON.stringify({ style_hint: $("#ai-style").value, only_missing: false }),
    });
    showSuggestion(
      `${r.items.length} 条配图 prompt`,
      `<ol>${r.items.map((x) => `<li>${escapeHtml(x.prompt)}</li>`).join("")}</ol>`,
      [
        ["写入各句", async () => {
          for (const x of r.items) {
            const c = state.project.clips.find((c) => c.id === x.id);
            if (c) c.visual.prompt = x.prompt;
          }
          await save();
          toast("prompt 已写入 project.json，拿去出图");
        }],
      ]
    );
  } catch (e) {
    toast(e.message, true);
  } finally {
    aiBusy(btn, false);
  }
}

async function aiTitles() {
  const btn = $("#btn-ai-titles");
  aiBusy(btn, true);
  try {
    const r = await api(`/api/p/${state.name}/ai/titles`, { method: "POST", body: "{}" });
    showSuggestion(
      "点一个用作标题",
      r.items.map((t) => `<div class="pick" data-title="${escapeHtml(t)}">${escapeHtml(t)}</div>`).join(""),
      []
    );
    $("#ai-out").querySelectorAll("[data-title]").forEach((el) =>
      el.addEventListener("click", () => {
        state.project.title = el.dataset.title;
        $("#title").value = el.dataset.title;
        save().then(paintPreview);
      })
    );
  } catch (e) {
    toast(e.message, true);
  } finally {
    aiBusy(btn, false);
  }
}

function showSuggestion(head, html, actions) {
  const box = $("#ai-out");
  box.innerHTML = `<div class="sug"><b>${escapeHtml(head)}</b>${html}<div class="row"></div></div>`;
  const row = box.querySelector(".row");
  for (const [label, fn] of actions) {
    const b = document.createElement("button");
    b.className = "ghost";
    b.textContent = label;
    b.onclick = fn;
    row.appendChild(b);
  }
}

/* ---------------------------------------------------------------- 事件绑定 */

function bind() {
  $("#clip-list").addEventListener("click", (e) => {
    const btn = e.target.closest("[data-act]");
    const row = e.target.closest(".clip");
    if (!btn || !row || btn.dataset.act === "text") return;
    const i = +row.dataset.i;
    switch (btn.dataset.act) {
      case "shot":         return select(i, +btn.dataset.j);
      case "shot-del":     e.stopPropagation(); return delShot(i, +btn.dataset.j);
      case "shot-add":     return pickMedia(i, 0, true);
      case "shot-replace": return pickMedia(i, state.shot, false);
      default:             return clipAction(i, btn.dataset.act);
    }
  });

  // 剪辑字段：入点/出点/倍速/镜头时长
  $("#clip-list").addEventListener("input", (e) => {
    const act = e.target.dataset.act;
    const row = e.target.closest(".clip");
    if (!row) return;
    const i = +row.dataset.i;
    const key = { "src-in": "in", "src-out": "out", speed: "speed", "shot-seconds": "seconds" }[act];
    if (key) return setShotField(i, state.shot, key, e.target.value);
    if (act?.startsWith("crop-")) setShotCrop(i, state.shot, act.slice(5), e.target.value);
  });

  // 转场：选类型 / 改时长
  $("#clip-list").addEventListener("change", (e) => {
    const cut = e.target.closest(".cut");
    if (!cut) return;
    const i = +cut.dataset.i;
    if (e.target.dataset.act === "trans") setTransition(i, e.target.value);
    if (e.target.dataset.act === "trans-dur")
      setTransition(i, cut.querySelector("[data-act=trans]").value, Number(e.target.value));
  });

  $("#clip-list").addEventListener("input", (e) => {
    if (e.target.dataset.act !== "text") return;
    const i = +e.target.closest(".clip").dataset.i;
    grow(e.target);
    state.project.clips[i].text = e.target.value;
    paintDerived();
    markDirty();
  });

  $("#clip-list").addEventListener("focusin", (e) => {
    const row = e.target.closest(".clip");
    if (row) select(+row.dataset.i);
  });

  $("#clip-list").addEventListener("keydown", (e) => {
    if (e.target.dataset.act !== "text") return;
    const i = +e.target.closest(".clip").dataset.i;
    if ((e.metaKey || e.ctrlKey) && e.key === "Enter") {
      e.preventDefault();
      addClip(i + 1);
    }
  });

  $("#pace-strip").addEventListener("click", (e) => {
    if (e.target.dataset.i) select(+e.target.dataset.i);
  });

  $("#title").addEventListener("input", (e) => {
    state.project.title = e.target.value;
    markDirty();
  });

  const styleField = (sel, key) =>
    $(sel).addEventListener("input", (e) => {
      state.project.style[key] = e.target.value;
      markDirty();
      clearTimeout(styleField.t);
      styleField.t = setTimeout(paintPreview, 900);
    });
  styleField("#st-font", "font");
  styleField("#st-title-color", "title_color");
  styleField("#st-brand", "brand_text");
  styleField("#st-avatar", "avatar");

  $("#btn-add").onclick = () => addClip();
  $("#btn-play").onclick = playThrough;
  $("#btn-render").onclick = startRender;
  $("#btn-tts").onclick = () => runTTS({ only_stale: true });
  $("#btn-tts-stale").onclick = () => runTTS({ only_stale: true });
  $("#btn-tts-all").onclick = () => runTTS({ only_stale: false });
  $("#btn-ai-split").onclick = aiSplit;
  $("#btn-ai-prompts").onclick = aiPrompts;
  $("#btn-ai-titles").onclick = aiTitles;
  $("#rate").oninput = (e) => ($("#rate-label").textContent = e.target.value);

  $("#btn-placeholder").onclick = async () => {
    try {
      apply(await api(`/api/p/${state.name}/placeholder`, {
        method: "POST",
        body: JSON.stringify({ only_missing: true }),
      }));
      toast("占位配图已补上");
    } catch (e) {
      toast(e.message, true);
    }
  };

  $("#btn-ass").onclick = async () => {
    try {
      const r = await api(`/api/p/${state.name}/ass`, { method: "POST" });
      toast(`已导出 ${r.out}`);
    } catch (e) {
      toast(e.message, true);
    }
  };

  bindMusic();
  bindTimeline();
  try {
    state.viewMode = localStorage.getItem("fluffycut.view") || "focus";
  } catch { /* ignore */ }
  $("#btn-prev").onclick = () => step(-1);
  $("#btn-next").onclick = () => step(1);
  $("#btn-viewmode").onclick = () => setViewMode(state.viewMode === "focus" ? "list" : "focus");
  $("#btn-learn").onclick = learnFromVideo;
  $("#project-select").onchange = (e) => open(e.target.value);

  $("#btn-new").onclick = openNewDialog;
  $("#btn-new-cancel").onclick = closeNewDialog;
  $("#btn-new-create").onclick = createProject;
  $("#btn-new-learn").onclick = () => {
    closeNewDialog();
    learnFromVideo();
  };
  $("#btn-ai-key-save").onclick = saveAIKey;
  $("#ai-key").addEventListener("keydown", (e) => e.key === "Enter" && saveAIKey());
  // 标题跟着生成目录名，除非人自己改过
  $("#new-title").addEventListener("input", (e) => {
    const nameEl = $("#new-name");
    if (!nameEl.dataset.touched) nameEl.value = slug(e.target.value);
  });
  $("#new-name").addEventListener("input", (e) => (e.target.dataset.touched = "1"));
  $("#modal").addEventListener("click", (e) => e.target.id === "modal" && closeNewDialog());

  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape" && !$("#modal").classList.contains("hidden")) return closeNewDialog();
    const typing = /^(INPUT|TEXTAREA|SELECT)$/.test(document.activeElement?.tagName || "");
    if (!typing && e.key === " ") {
      e.preventDefault();
      return hearClip(state.selected);
    }
    if (!typing && (e.key === "ArrowLeft" || e.key === "ArrowRight")) {
      e.preventDefault();
      return step(e.key === "ArrowRight" ? 1 : -1);
    }
    if ((e.metaKey || e.ctrlKey) && e.key === "s") {
      e.preventDefault();
      save();
    }
  });

  window.addEventListener("beforeunload", (e) => {
    if ($("#save-state").classList.contains("dirty")) e.preventDefault();
  });
}

/* ---------------------------------------------------------------- 两条时间轴

   上面是原片（只读），下面是你自己的片子。两条各有各的播放头和滚动位置 ——
   看原片第 30 秒的同时，自己的片子可以停在第 3 秒。

   帧不是一张一个请求：每个素材抽成一条雪碧图（服务端一次 ffmpeg 抽完并缓存），
   轨道上的格子只是把雪碧图挪个偏移，所以划得再快也不掉帧。 */

const TILE_W = 96;                 // 雪碧图每格宽度，和 /strip 的 w 一致
const TILE_H = 170;
const TRACK_H = 76;                // 轨道高度
// 雪碧图整体缩到轨道高度，一格就是这么宽 —— 不缩的话只能看见每帧的上面一截
const TILE_SCALE = TRACK_H / TILE_H;
const CELL_W = Math.round(TILE_W * TILE_SCALE);
// 抽帧密度：目标每 0.4 秒一格，长片封顶 240 格（再多就是给浏览器添堵）。
// 固定格数的话，长素材会稀得没法用。
const STRIP_STEP = 0.4;
const STRIP_MAX = 240;
const stripCount = (seconds) =>
  Math.max(8, Math.min(STRIP_MAX, Math.ceil((seconds || 1) / STRIP_STEP)));

/* 两条轨唯一的区别是数据从哪来、点下去干什么 */
const TRACKS = {
  ref: {
    key: "ref",
    box: () => $("#tl-ref"),
    timeLabel: () => $("#tl-ref-time"),
    clips: () => state.derived?.reference_timeline?.clips || [],
    duration: () => state.derived?.reference_timeline?.duration || 0,
    head: () => state.refPlayhead,
    setHead: (t) => (state.refPlayhead = t),
    onSettle: (t) => paintCompareAt(t),
    onClick: (hit) => paintRefCard(hit ? hit.i : 0),
  },
  edit: {
    key: "edit",
    box: () => $("#tl-edit"),
    timeLabel: () => $("#tl-edit-time"),
    clips: () => state.derived?.clips || [],
    duration: () => state.derived?.duration || 0,
    head: () => state.playhead,
    setHead: (t) => (state.playhead = t),
    onSettle: (t) => paintMainAt(t),
    onClick: (hit) => hit && select(hit.i, hit.sh.index),
  },
};

function stripUrl(sh) {
  const n = sh.type === "video" ? stripCount(sh.src_seconds) : 1;
  return `/api/p/${state.name}/strip?path=${encodeURIComponent(sh.path)}&count=${n}&w=${TILE_W}`;
}

function tileIndex(sh, srcTime) {
  if (sh.type !== "video" || !sh.src_seconds) return 0;
  const n = stripCount(sh.src_seconds);
  return Math.max(0, Math.min(n - 1, Math.floor((srcTime / sh.src_seconds) * n)));
}

/* 轨道上的一格：整条雪碧图缩到轨道高度，再挪到对应偏移 */
function cellStyle(sh, srcTime, w) {
  const n = sh.type === "video" ? stripCount(sh.src_seconds) : 1;
  const i = tileIndex(sh, srcTime);
  return `width:${w}px;height:${TRACK_H}px;background-image:url('${stripUrl(sh)}');` +
         `background-size:${n * CELL_W}px ${TRACK_H}px;background-position:${-i * CELL_W}px 0`;
}

function paintTrack(tr) {
  const box = tr.box();
  const inner = box.querySelector(".tl-inner");
  const px = state.pxPerSec;
  const clips = tr.clips();
  const selected = tr.key === "edit" ? state.selected : state.refSelected;

  inner.innerHTML = clips
    .map((c, i) => {
      const w = Math.max(2, c.seconds * px);
      const shots = (c.shots || [])
        .map((sh) => {
          const sw = Math.max(1, sh.seconds * px);
          if (!sh.exists || !sh.path) {
            return `<div class="tl-shot" style="width:${sw}px"><div class="tl-empty"></div></div>`;
          }
          const n = Math.max(1, Math.ceil(sw / CELL_W));
          const tiles = Array.from({ length: n }, (_, k) => {
            const local = (k * CELL_W) / px;                     // 这一格在镜头内的时间
            const src = (sh.src_in || 0) + local * (sh.speed || 1);
            const tw = Math.min(CELL_W, sw - k * CELL_W);
            return `<div class="tl-tile" style="${cellStyle(sh, src, tw)}"></div>`;
          }).join("");
          return `<div class="tl-shot" style="width:${sw}px">${tiles}</div>`;
        })
        .join("");
      const text = tr.key === "edit" ? state.project.clips[i]?.text || "" : c.text || "";
      return `<div class="tl-clip ${c.pace}${i === selected ? " on" : ""}" data-i="${i}"
                   style="width:${w}px"><span class="cap">${i + 1}. ${escapeHtml(
        text.slice(0, 20))}</span>${shots}</div>`;
    })
    .join("");
  // 编辑轴还是空的时候，别让人对着一个小方块发呆
  const blank = tr.key === "edit" && state.project.clips.every(
    (c) => !c.text.trim() && !shotsOf(c)[0]?.path);
  if (!clips.length || blank) {
    inner.innerHTML = `<div class="tl-blank">${
      tr.key === "edit"
        ? "编辑轴是空的 —— 点右上角「整条复制到编辑轴」照着原片改，或者直接在下面写第一句"
        : "没有原片"}</div>`;
  }
  moveHead(tr);
}

function paintTimeline() {
  $("#tl-zoom").textContent = `${state.pxPerSec} px/秒`;
  const rt = state.derived?.reference_timeline;
  $("#track-ref").classList.toggle("hidden", !rt);
  if (rt) {
    $("#ref-name-inline").textContent = `${rt.name || ""} · ${rt.clips.length} 句`;
    paintTrack(TRACKS.ref);
  }
  paintTrack(TRACKS.edit);
  paintRefCard(state.refSelected);
}

function timeAt(tr, clientX) {
  const box = tr.box();
  const r = box.getBoundingClientRect();
  const x = clientX - r.left + box.scrollLeft;
  return Math.max(0, Math.min(tr.duration(), x / state.pxPerSec));
}

function moveHead(tr) {
  const box = tr.box();
  box.querySelector(".tl-head").style.left =
    `${tr.head() * state.pxPerSec - box.scrollLeft}px`;
}

/* 全片第 t 秒落在哪个片段、对应素材的哪一刻 */
function shotAtTime(tr, t) {
  const clips = tr.clips();
  for (let i = 0; i < clips.length; i++) {
    const c = clips[i];
    if (t < c.start || t >= c.start + c.seconds) continue;
    const local = t - c.start;
    for (const sh of c.shots || []) {
      if (local >= sh.start && local < sh.start + sh.seconds) {
        return { i, sh, srcTime: (sh.src_in || 0) + (local - sh.start) * (sh.speed || 1) };
      }
    }
    return { i, sh: (c.shots || [])[0], srcTime: 0 };
  }
  return null;
}

/* 悬停：先用雪碧图里的那一格给个即时反馈，停下来再去要一张真正的帧 */
function scrub(tr, t, { pop = false } = {}) {
  tr.setHead(t);
  moveHead(tr);
  tr.timeLabel().textContent = `${t.toFixed(2)}s`;

  const hit = shotAtTime(tr, t);
  const popEl = tr.box().querySelector(".tl-pop");
  if (pop && hit?.sh) {
    popEl.classList.remove("hidden");
    popEl.style.left = `${t * state.pxPerSec - tr.box().scrollLeft}px`;
    const cell = popEl.querySelector("i");
    const n = hit.sh.type === "video" ? stripCount(hit.sh.src_seconds) : 1;
    cell.style.backgroundImage = hit.sh.exists && hit.sh.path ? `url('${stripUrl(hit.sh)}')` : "none";
    cell.style.backgroundSize = `${n * TILE_W}px ${TILE_H}px`;
    cell.style.backgroundPosition = `${-tileIndex(hit.sh, hit.srcTime) * TILE_W}px 0`;
    popEl.querySelector("b").textContent = `${t.toFixed(2)}s`;
  }
  clearTimeout(scrub[`t_${tr.key}`]);   // 每条轨自己的防抖，互不打断
  scrub[`t_${tr.key}`] = setTimeout(() => tr.onSettle(t), 180);
}

function bindTrack(tr) {
  const box = tr.box();
  box.addEventListener("mousemove", (e) => scrub(tr, timeAt(tr, e.clientX), { pop: true }));
  box.addEventListener("mouseleave", () => box.querySelector(".tl-pop").classList.add("hidden"));
  box.addEventListener("scroll", () => moveHead(tr));      // 两条轨各自滚动，互不牵连
  box.addEventListener("click", (e) => {
    const t = timeAt(tr, e.clientX);
    const hit = shotAtTime(tr, t);
    scrub(tr, t);
    if (tr.key === "ref" && hit) state.refSelected = hit.i;
    tr.onClick(hit);
    if (tr.key === "ref") paintTrack(TRACKS.ref);
  });
}

function bindTimeline() {
  bindTrack(TRACKS.ref);
  bindTrack(TRACKS.edit);
  document.querySelectorAll("[data-zoom]").forEach((b) => {
    b.onclick = () => {
      state.pxPerSec = Math.max(10, Math.min(400,
        Math.round(state.pxPerSec * (b.dataset.zoom === "in" ? 1.6 : 1 / 1.6))));
      paintTimeline();
    };
  });
  $("#tl-compare").onchange = (e) => {
    state.compareOn = e.target.checked;
    paintCompareAt(state.refPlayhead);
  };
  $("#btn-copy-all").onclick = copyWholeReference;
  $("#btn-compare-pick").onclick = async () => {
    const native = await pickNativePath("video");
    if (!native) return toast("桌面版才能直接选文件；浏览器里请用「学参考片」导入", true);
    try {
      apply(await api(`/api/p/${state.name}/compare`, {
        method: "POST", body: JSON.stringify({ path: native }),
      }));
      $("#tl-compare").checked = state.compareOn = true;
      paintCompareAt(state.refPlayhead);
      toast("对比片已设好");
    } catch (e) {
      toast(e.message, true);
    }
  };
}

/* ---------------------------------------------------------------- 原片当前句 */

function paintRefCard(i) {
  const rt = state.derived?.reference_timeline;
  const card = $("#ref-card");
  card.classList.toggle("hidden", !rt);
  if (!rt) return;
  state.refSelected = Math.max(0, Math.min(i ?? 0, rt.clips.length - 1));
  const c = rt.clips[state.refSelected];
  if (!c) return;
  const sh = c.shots[0];
  const bg = sh
    ? `background-image:url('${stripUrl(sh)}');background-size:${stripCount(sh.src_seconds) * 44}px 78px;` +
      `background-position:${-tileIndex(sh, sh.src_in) * 44}px 0`
    : "";
  card.innerHTML = `
    <div class="thumb" style="${bg}"></div>
    <div class="body">
      <div class="who">原片 · 第 ${state.refSelected + 1} 句 / 共 ${rt.clips.length}</div>
      <div class="txt ${c.text ? "" : "empty"}">${escapeHtml(c.text || "（这一句没扒到台词）")}</div>
      <div class="meta">${c.start.toFixed(2)}–${(c.start + c.seconds).toFixed(2)}s · ${c.seconds.toFixed(2)} 秒 · ${c.shots.length} 个镜头</div>
    </div>
    <button class="ghost" id="btn-copy-one">复制这一句</button>`;
  $("#btn-copy-one").onclick = () => copyOneReference(state.refSelected);
}

async function copyWholeReference() {
  const n = state.project.clips.filter((c) => c.text.trim() || shotsOf(c)[0]?.path).length;
  if (n && !window.confirm?.(`编辑轴上已经有 ${n} 句，整条复制会覆盖掉，继续？`)) return;
  try {
    apply(await api(`/api/p/${state.name}/reference/copy`, { method: "POST", body: "{}" }));
    select(0, 0);
    toast(`已把原片整条复制过来（${state.project.clips.length} 句）`);
  } catch (e) {
    toast(e.message, true);
  }
}

async function copyOneReference(index) {
  try {
    apply(await api(`/api/p/${state.name}/reference/copy`, {
      method: "POST", body: JSON.stringify({ index, after: state.selected }),
    }));
    select(Math.min(state.selected + 1, state.project.clips.length - 1), 0);
    toast("这一句已复制到编辑轴");
  } catch (e) {
    toast(e.message, true);
  }
}

/* ---------------------------------------------------------------- 预览 */

/* 上半：你正在改的片子，跟着编辑轴的播放头 */
function paintMainAt(t) {
  if (!state.derived) return;
  $("#preview").src = `/api/p/${state.name}/frame?at=${t.toFixed(2)}&v=${Date.now()}`;
  const hit = shotAtTime(TRACKS.edit, t);
  $("#preview-label").textContent = hit
    ? `${t.toFixed(2)}s · 第 ${hit.i + 1} 句 · 镜头 ${(hit.sh?.index ?? 0) + 1}`
    : `${t.toFixed(2)}s`;
}

/* 下半：原片，跟着原片轨的播放头 —— 和上面完全独立 */
function paintCompareAt(t) {
  const rt = state.derived?.reference_timeline;
  const cmp = state.derived?.compare;
  const path = rt?.path || cmp?.path;
  const box = $("#compare-box");
  const show = state.compareOn && !!path;
  box.classList.toggle("hidden", !show);
  if (!show) return;
  const ct = Math.max(0, t + (rt ? 0 : cmp?.offset || 0));
  $("#compare-img").src =
    `/api/p/${state.name}/thumb?path=${encodeURIComponent(path)}&t=${ct.toFixed(2)}&w=480`;
}

function paintPreviewAt(t) {
  paintMainAt(t);
  paintCompareAt(state.refPlayhead);
}

/* ---------------------------------------------------------------- 选文件 */

/* 桌面版走原生面板：<input type="file"> 在 WKWebView 里未必弹得出来（「学参考片」
   按下去没反应就是这么来的），而且本地 app 没必要把几百 MB 的视频塞进 HTTP 上传。
   浏览器里没有 pywebview，自动退回上传。 */
async function pickNativePath(kind) {
  const api = window.pywebview?.api;
  if (!api?.pick) return null;
  try {
    return (await api.pick(kind)) || null;
  } catch {
    return null;
  }
}

const isDesktop = () => !!window.pywebview?.api?.pick;

function pickUpload(accept) {
  return new Promise((resolve) => {
    const input = document.createElement("input");
    input.type = "file";
    input.accept = accept;
    input.onchange = () => resolve(input.files?.[0] || null);
    input.click();
  });
}

/* ---------------------------------------------------------------- 新建工程 */

/* 目录名：中文原样留着（文件系统认得），只把空白和标点换成短横 */
const slug = (s) =>
  (s || "").trim().replace(/[^\w\u4e00-\u9fff]+/g, "-").replace(/^-+|-+$/g, "").slice(0, 40);

function openNewDialog() {
  $("#modal").classList.remove("hidden");
  $("#new-title").value = "";
  $("#new-name").value = "";
  $("#new-name").dataset.touched = "";
  $("#new-brand").value = state.project?.style?.brand_text || "";
  setTimeout(() => $("#new-title").focus(), 30);
}

function closeNewDialog() {
  $("#modal").classList.add("hidden");
}

async function createProject() {
  const title = $("#new-title").value.trim();
  const name = slug($("#new-name").value || title) || `片子-${Date.now().toString(36).slice(-4)}`;
  try {
    const data = await api("/api/projects", {
      method: "POST",
      body: JSON.stringify({ name, title, brand: $("#new-brand").value.trim() }),
    });
    closeNewDialog();
    await loadProjects();
    $("#project-select").value = name;
    apply(data, name);
    history.replaceState(null, "", `?p=${name}`);
    select(0);
    toast("新片子建好了，先把第一句写上");
    setTimeout(() => document.querySelector(".clip textarea")?.focus(), 60);
  } catch (e) {
    toast(e.message, true);
  }
}

/* ---------------------------------------------------------------- AI 凭据 */

/* 双击启动的 .app 读不到 shell 里 export 的环境变量，所以 key 得能在界面上填。
   原文只往服务端走一次，回来的永远是掩码。 */
function paintAIKey() {
  const c = state.caps || {};
  const box = $("#ai-key-box");
  const fromEnv = c.ai_source === "环境变量" || c.ai_source === "ant 登录态";
  box.classList.toggle("hidden", fromEnv);
  $("#ai-key").value = "";
  $("#ai-key").placeholder = c.ai_key_masked ? `已保存 ${c.ai_key_masked}` : "粘贴 sk-ant-…";
  $("#ai-key-hint").textContent = fromEnv
    ? `凭据来自${c.ai_source}，界面上不用填`
    : c.ai_key_masked
      ? "存在 ~/.config/fluffycut/config.json（明文，权限 600）。留空保存即清除。"
      : "去 console.anthropic.com 拿一个，粘进来即可。存在本机，不会进工程文件。";
}

async function saveAIKey() {
  const key = $("#ai-key").value.trim();
  try {
    await api("/api/settings", { method: "POST", body: JSON.stringify({ api_key: key }) });
    const data = await api(`/api/p/${state.name}`);
    state.caps = data.caps;
    paintCaps();
    toast(key ? "key 已保存" : "key 已清除");
  } catch (e) {
    toast(e.message, true);
  }
}

/* ---------------------------------------------------------------- 参考片 */

function paintReference() {
  const r = state.project.reference;
  $("#ref-panel").classList.toggle("hidden", !r);
  if (!r) return;
  if (!paintReference.opened) {          // 刚学完一个片子，数字直接摊开给你看
    $("#ref-panel").open = true;
    paintReference.opened = true;
  }
  $("#ref-name").textContent = r.name || "";
  const st = r.stats || {};
  const rows = [
    ["总长", `${(r.duration || 0).toFixed(1)} 秒`],
    ["句数", st.sentences],
    ["每句平均", `${st.seconds_per_sentence} 秒`],
    ["中位数", `${st.median_sentence} 秒`],
    ["人声占比", `${Math.round((st.speech_ratio || 0) * 100)}%`],
    ["镜头切点", `${st.cuts} 次 · ${st.cuts_per_minute}/分钟`],
  ];
  if (st.chars_per_second) rows.push(["语速", `${st.chars_per_second} 字/秒`]);
  rows.push(["台词来源", r.source === "silence" ? "按停顿切分（无转写）" : r.source]);
  $("#ref-stats").innerHTML = rows
    .map(([k, v]) => `<span>${k}</span><b>${v ?? "—"}</b>`)
    .join("");
}

/* 读入一个参考片：上传 -> 后台分析 -> 打开生成的骨架 */
async function learnFromVideo() {
  const tr = state.caps?.transcriber;
  const native = await pickNativePath("video");
  const file = native ? null : isDesktop() ? null : await pickUpload("video/*");
  if (!native && !file) return;
  const label = native ? native.split("/").pop() : file.name;

  toast(`正在拆解 ${label}${tr ? "（含转写，可能要一会儿）" : "（没装 whisper，只扒节奏）"}…`);
  try {
    const job = native
      ? await api("/api/analyze_path", {
          method: "POST", body: JSON.stringify({ path: native, transcribe: !!tr }),
        })
      : await uploadFile(`/api/analyze?transcribe=${!!tr}`, file);
    const done = await pollJob(job.id, (j) => toast(j.note || "分析中…"));
    await loadProjects();
    await open(done.result.project);
    select(0);
    toast(done.result.summary.split("\n").slice(1).join(" · ").replace(/\s+/g, " "), false, 9000);
  } catch (e) {
    toast(e.message, true);
  }
}

/* 浏览器里的上传通道 */
async function uploadFile(url, file) {
  const fd = new FormData();
  fd.append("file", file);
  const r = await fetch(url, { method: "POST", body: fd });
  if (!r.ok) throw new Error((await r.json()).detail);
  return r.json();
}

/* ---------------------------------------------------------------- 配乐 */

function paintMusic() {
  const m = state.project.music;
  const info = state.derived?.music;
  $("#music-path").textContent = m?.path || "还没选音乐";
  $("#music-controls").classList.toggle("off", !m);
  if (!m) return;

  // 让人看见这段音乐到底多长、够不够铺满片子 —— 不然「起点」就是个瞎填的数字
  const player = $("#music-player");
  const url = `/api/p/${state.name}/file/${m.path}`;
  if (player.dataset.src !== url) {
    player.src = url;
    player.dataset.src = url;
  }
  if (info) {
    const loops = info.loops > 0 ? `，会循环 ${info.loops} 遍铺满` : "，够铺满";
    const mono = info.channels < 2 ? " · 单声道" : "";
    $("#music-info").textContent =
      `音乐 ${info.duration}s · 片子要 ${info.needed}s · 从 ${m.start}s 起用${loops}${mono}`;
  }
  $("#music-devocal-note").textContent = state.caps?.demucs
    ? "用 demucs 分离，质量好但慢"
    : "中置抵消：居中的人声会被完全消掉，同样居中的贝斯/底鼓也会一起没";
  $("#music-volume").value = m.volume;
  $("#music-volume-label").textContent = Math.round(m.volume * 100) + "%";
  $("#music-fadein").value = m.fade_in;
  $("#music-fadeout").value = m.fade_out;
  $("#music-start").value = m.start;
  $("#music-duck").checked = !!m.duck;
}

function bindMusic() {
  $("#btn-music-pick").onclick = async () => {
    // 视频也收：参考片的配乐常常就藏在视频里，后端会自动抽音轨
    const native = await pickNativePath("audio");
    const file = native ? null : isDesktop() ? null : await pickUpload("audio/*,video/*");
    if (!native && !file) return;
    const label = native ? native.split("/").pop() : file.name;
    try {
      const data = native
        ? await api(`/api/p/${state.name}/music_path`, {
            method: "POST", body: JSON.stringify({ path: native }),
          })
        : await uploadFile(`/api/p/${state.name}/upload_music`, file);
      apply(data);
      toast(/\.(mp4|mov|m4v|webm|mkv)$/i.test(label) ? "已从视频里抽出音轨" : "配乐已加上");
    } catch (e) {
      toast(e.message, true);
    }
  };

  // 一边听一边定起点：听到副歌开始，点一下就写进去
  $("#btn-music-mark").onclick = () => {
    const t = Math.max(0, Math.round($("#music-player").currentTime * 10) / 10);
    if (!state.project.music) return;
    state.project.music.start = t;
    $("#music-start").value = t;
    markDirty();
    toast(`起点设成 ${t}s`);
  };

  $("#btn-music-devocal").onclick = async () => {
    if (!state.project.music) return toast("还没有配乐", true);
    const keep_bass = $("#music-keepbass").checked;
    try {
      const job = await api(`/api/p/${state.name}/music/remove_vocals`, {
        method: "POST", body: JSON.stringify({ keep_bass }),
      });
      toast("处理中…");
      const done = await pollJob(job.id, (j) => toast(j.note || "处理中…"));
      apply(await api(`/api/p/${state.name}`));
      toast(`人声已去掉（${done.result.method}），不满意可以点还原`);
    } catch (e) {
      toast(e.message, true);
    }
  };

  $("#btn-music-restore").onclick = async () => {
    try {
      apply(await api(`/api/p/${state.name}/music/restore`, { method: "POST" }));
      toast("已还原成处理前那一版");
    } catch (e) {
      toast(e.message, true);
    }
  };

  $("#btn-music-clear").onclick = async () => {
    delete state.project.music;
    await save();
    paintMusic();
  };

  const field = (sel, key, cast = Number) =>
    $(sel).addEventListener("input", (e) => {
      if (!state.project.music) return;
      state.project.music[key] = cast(e.target.type === "checkbox" ? e.target.checked : e.target.value);
      if (key === "volume") $("#music-volume-label").textContent = Math.round(e.target.value * 100) + "%";
      markDirty();
    });
  field("#music-volume", "volume");
  field("#music-fadein", "fade_in");
  field("#music-fadeout", "fade_out");
  field("#music-start", "start");
  field("#music-duck", "duck", Boolean);
}

/* ---------------------------------------------------------------- 启动 */

(async function boot() {
  bind();
  try {
    const { voices, default: def } = await api("/api/voices");
    $("#voice").innerHTML = voices
      .map((v) => `<option ${v.name === def ? "selected" : ""}>${v.name}${v.zh ? "（中文）" : ""}</option>`)
      .join("");
    // <option> 文本里带了说明，取值时只要音色名
    $("#voice").querySelectorAll("option").forEach((o, i) => (o.value = voices[i].name));
  } catch { /* 没有 say 就算了，配音按钮会报错提示 */ }

  const name = await loadProjects();
  if (!name) return toast("projects/ 下还没有工程，点 ＋ 新建一个");
  await open(name);
})();
