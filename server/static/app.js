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
  const wanted = +(new URLSearchParams(location.search).get("clip") ?? 0);
  apply(await api(`/api/p/${name}`), name);
  history.replaceState(null, "", `?p=${name}`);
  if (wanted > 0) select(wanted);          // ?clip=2 直接定位到第 3 句，方便分享/排查
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

  const c = state.caps;
  $("#caps-note").textContent =
    `ffmpeg ${c.ffmpeg ? "✓" : "✗"} · 系统 TTS ${c.tts ? "✓" : "✗"} · AI ${c.ai ? "✓" : "✗"}` +
    (c.libass ? " · libass ✓" : " · 无 libass（字幕走 PIL 图层，不影响成片）");
  $("#ai-note").textContent = c.ai ? "" : c.ai_note || "";
  $("#ai-body").classList.toggle("off", !c.ai);
}

function paintList() {
  const list = $("#clip-list");
  const d = state.derived;
  list.innerHTML = state.project.clips
    .map((c, i) => clipRow(c, i, d?.clips?.[i]) + cutRow(i, d?.clips?.[i]))
    .join("");
  list.querySelectorAll("textarea").forEach(grow);
  markSelected();
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
            <button data-act="tts" title="给这句配音">音</button>
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
  const thumb = sh.exists && sh.path
    ? `<img loading="lazy" src="/api/p/${state.name}/thumb?path=${encodeURIComponent(sh.path)}&w=120">`
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
    </div>`;
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
  const sh = state.derived?.clips?.[state.selected]?.shots?.[state.shot];
  const t = sh ? sh.start + 0.02 : 0;
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
  if (changed) {
    paintList();                 // 镜头条和入出点行都跟着选中状态走
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
    case "tts":
      return runTTS({ clips: [clips[i].id] });
  }
}

/* 换素材 / 加镜头都走这里：append=true 就是在这句话末尾加一个新镜头 */
function pickMedia(i, shot = 0, append = false) {
  const input = document.createElement("input");
  input.type = "file";
  input.accept = "image/*,video/mp4,video/quicktime";
  input.onchange = async () => {
    if (!input.files?.[0]) return;
    const fd = new FormData();
    fd.append("file", input.files[0]);
    const q = `clip=${encodeURIComponent(state.project.clips[i].id)}&shot=${shot}&append=${append}`;
    try {
      const data = await fetch(`/api/p/${state.name}/upload?${q}`, { method: "POST", body: fd })
        .then(async (r) => {
          if (!r.ok) throw new Error((await r.json()).detail);
          return r.json();
        });
      apply(data);
      select(i, append ? shotsOf(state.project.clips[i]).length - 1 : shot);
      toast(append ? "镜头已加上" : "素材已替换");
    } catch (e) {
      toast(e.message, true);
    }
  };
  input.click();
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

async function setTransition(i, type, duration) {
  const c = state.project.clips[i];
  if (!type) delete c.transition;
  else c.transition = { type, duration: duration ?? c.transition?.duration ?? 0.35 };
  await save();
  paintList();
}

/* ---------------------------------------------------------------- 配音 / 渲染 */

async function runTTS(payload) {
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
    toast(`配好了 ${data.done.length} 句，时长已回写`);
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
    if (key) setShotField(i, state.shot, key, e.target.value);
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
  $("#btn-learn").onclick = learnFromVideo;
  $("#project-select").onchange = (e) => open(e.target.value);

  $("#btn-new").onclick = async () => {
    const name = prompt("新工程名（英文/数字，会作为目录名）");
    if (!name) return;
    try {
      const data = await api("/api/projects", {
        method: "POST",
        body: JSON.stringify({ name, title: "", brand: state.project?.style?.brand_text || "" }),
      });
      await loadProjects();
      $("#project-select").value = name;
      apply(data, name);
    } catch (e) {
      toast(e.message, true);
    }
  };

  document.addEventListener("keydown", (e) => {
    if ((e.metaKey || e.ctrlKey) && e.key === "s") {
      e.preventDefault();
      save();
    }
  });

  window.addEventListener("beforeunload", (e) => {
    if ($("#save-state").classList.contains("dirty")) e.preventDefault();
  });
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
function learnFromVideo() {
  const input = document.createElement("input");
  input.type = "file";
  input.accept = "video/*";
  input.onchange = async () => {
    const f = input.files?.[0];
    if (!f) return;
    const tr = state.caps?.transcriber;
    toast(tr ? `正在拆解 ${f.name}（含转写，可能要一会儿）…` : `正在拆解 ${f.name}（没装 whisper，只扒节奏）…`);
    const fd = new FormData();
    fd.append("file", f);
    try {
      const job = await fetch(`/api/analyze?transcribe=${!!tr}`, { method: "POST", body: fd })
        .then(async (r) => {
          if (!r.ok) throw new Error((await r.json()).detail);
          return r.json();
        });
      const done = await pollJob(job.id, (j) => toast(j.note || "分析中…"));
      await loadProjects();
      await open(done.result.project);
      toast(done.result.summary.split("\n").slice(1).join(" · ").replace(/\s+/g, " "), false, 9000);
    } catch (e) {
      toast(e.message, true);
    }
  };
  input.click();
}

/* ---------------------------------------------------------------- 配乐 */

function paintMusic() {
  const m = state.project.music;
  $("#music-path").textContent = m?.path || "还没选音乐";
  $("#music-controls").classList.toggle("off", !m);
  if (!m) return;
  $("#music-volume").value = m.volume;
  $("#music-volume-label").textContent = Math.round(m.volume * 100) + "%";
  $("#music-fadein").value = m.fade_in;
  $("#music-fadeout").value = m.fade_out;
  $("#music-start").value = m.start;
  $("#music-duck").checked = !!m.duck;
}

function bindMusic() {
  $("#btn-music-pick").onclick = () => {
    const input = document.createElement("input");
    input.type = "file";
    // 视频也收：参考片的配乐常常就藏在视频里，后端会自动抽音轨
    input.accept = "audio/*,video/*";
    input.onchange = async () => {
      if (!input.files?.[0]) return;
      const fd = new FormData();
      fd.append("file", input.files[0]);
      try {
        const data = await fetch(`/api/p/${state.name}/upload_music`, { method: "POST", body: fd })
          .then(async (r) => {
            if (!r.ok) throw new Error((await r.json()).detail);
            return r.json();
          });
        apply(data);
        toast(/\.(mp4|mov|m4v|webm)$/i.test(input.files[0].name) ? "已从视频里抽出音轨" : "配乐已加上");
      } catch (e) {
        toast(e.message, true);
      }
    };
    input.click();
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
