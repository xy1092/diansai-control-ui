// NUEDC PID Web Dashboard – front-end logic
// Pure vanilla JS, no build step.

const CH = ["L", "R", "LINE", "ANG"];
const TRACE_LEN = 600; // ~6s @ 100Hz

// gauge ranges per channel (full-scale)
const GAUGE_RANGE = {
  L:   { min: 0, max: 1.0, unit: "pulse/ms", label: "speed" },
  R:   { min: 0, max: 1.0, unit: "pulse/ms", label: "speed" },
  LINE:{ min: -1.0, max: 1.0, unit: "bias", label: "bias" },
  ANG: { min: -180, max: 180, unit: "°", label: "heading" },
};

const COLORS = {
  L: "#5bd0c5",
  R: "#9c79f2",
  LINE: "#f5c451",
  ANG: "#7ed482",
  sp: "#f87f1a",
  out: "#5bd0c5",
};

const state = {
  ws: null,
  connected: false,
  port: "/dev/ttyACM0",
  baud: 115200,
  rate_hz: 0,
  frames: 0,
  lastFrameTs: 0,
  channels: Object.fromEntries(CH.map(c => [c, {
    sp: [], meas: [], out: [], err: [], t: []
  }])),
  line: { bias: 0, strength: 0, on_line: 0, raw: [0,0,0,0,0,0,0] },
  app: null,
  gains: Object.fromEntries(CH.map(c => [c, { kp: 0, ki: 0, kd: 0 }])),
  params: {},
  csv_path: null,
  blackbox: { count: 0, active: false },
  ai: { results: [], pending: false },
  aiAuto: { running: false, round: 0, stable_rounds: 0, max_rounds: 0, reason: "" },
};

// -------- helpers --------
const $ = sel => document.querySelector(sel);
const $$ = sel => document.querySelectorAll(sel);

function setText(sel, txt) { const el = $(sel); if (el) el.textContent = txt; }

function setStat(card, key, val) {
  const el = card.querySelector(`[data-stat="${key}"]`);
  if (el) el.textContent = val;
}

function fmt(n, digits=2) {
  if (n === null || n === undefined || Number.isNaN(n)) return "--";
  return Number(n).toFixed(digits);
}

function log(text, cls="recv") {
  const out = $("#console-out");
  if (!out) return;
  const span = document.createElement("span");
  span.className = cls;
  const ts = new Date().toLocaleTimeString("zh-CN", { hour12: false });
  span.textContent = `[${ts}] ${text}\n`;
  out.appendChild(span);
  // cap lines
  if (out.children.length > 1000) {
    while (out.children.length > 800) out.removeChild(out.firstChild);
  }
  out.scrollTop = out.scrollHeight;
}

// -------- API --------
async function api(path, body) {
  const opts = { method: body ? "POST" : "GET", headers: { "Content-Type": "application/json" } };
  if (body) opts.body = JSON.stringify(body);
  const res = await fetch(path, opts);
  return await res.json();
}

async function connect() {
  const port = $("#port-input").value.trim() || "/dev/ttyACM0";
  const baud = parseInt($("#baud-input").value, 10) || 115200;
  const r = await api("/api/connect", { port, baud });
  if (!r.ok) log("connect failed: " + (r.error || ""), "err");
}
async function disconnect() { await api("/api/disconnect", {}); }

async function sendCmd(text) {
  if (!text) return;
  if (!text.endsWith("\n")) text += "\r\n";
  const r = await api("/api/cmd", { text });
  if (!r.ok) log("send failed: " + (r.error || ""), "err");
}

async function applyPid(ch) {
  const card = document.querySelector(`.pid-card[data-ch="${ch}"]`);
  const grab = g => {
    const v = card.querySelector(`[data-gain="${ch}:${g}"]`).value.trim();
    return v === "" ? null : parseFloat(v);
  };
  const body = { ch, kp: grab("KP"), ki: grab("KI"), kd: grab("KD") };
  const r = await api("/api/pid/set", body);
  if (!r.ok) log("pid set failed: " + (r.error || ""), "err");
}

async function pullPid(ch) {
  await api("/api/log/dump_gains", {});
}

function aiSelectedChannels() {
  const raw = $("#ai-channel")?.value || "LINE";
  return raw.split(",").map(s => s.trim()).filter(Boolean);
}

function aiProvider() {
  return $("#ai-provider")?.value || "local";
}

async function runAiTune(apply=false) {
  const seconds = parseFloat($("#ai-seconds").value) || 8;
  const aggressiveness = parseFloat($("#ai-aggr").value) || 0.5;
  const channels = aiSelectedChannels();
  const provider = aiProvider();
  state.ai.pending = true;
  setText("#ai-status", apply ? "正在写入建议..." : `正在询问 ${provider}...`);
  const r = await api("/api/ai/tune", { channels, seconds, aggressiveness, apply, provider });
  state.ai.pending = false;
  if (!r.ok) {
    setText("#ai-status", "分析失败");
    renderAiResults([{ ch: channels.join(","), error: r.error || "unknown error" }]);
    return;
  }
  state.ai.results = r.results || [];
  setText("#ai-status", apply ? `已写入 ${r.sent?.length || 0} 条` : "建议已生成");
  renderAiResults(state.ai.results);
  if (apply && r.sent?.length) log("AI apply: " + r.sent.join(" / "), "send");
}

async function startAiAuto() {
  const seconds = parseFloat($("#ai-seconds").value) || 8;
  const aggressiveness = parseFloat($("#ai-aggr").value) || 0.4;
  const max_rounds = parseInt($("#ai-rounds").value, 10) || 12;
  const channels = aiSelectedChannels();
  const provider = aiProvider();
  setText("#ai-status", "自动调试启动中...");
  const r = await api("/api/ai/auto/start", {
    channels, seconds, aggressiveness, interval: 2.0, max_rounds, provider
  });
  if (!r.ok) {
    setText("#ai-status", "自动调试启动失败");
    log("auto tune failed: " + (r.error || ""), "err");
    return;
  }
  onAiAuto(r.auto);
}

async function stopAiAuto() {
  const r = await api("/api/ai/auto/stop", {});
  if (!r.ok) log("auto stop failed: " + (r.error || ""), "err");
  else onAiAuto(r.auto);
}

// -------- WebSocket --------
function connectWs() {
  const proto = location.protocol === "https:" ? "wss" : "ws";
  const ws = new WebSocket(`${proto}://${location.host}/ws`);
  state.ws = ws;
  ws.onopen = () => log("ws connected", "sys");
  ws.onclose = () => { log("ws closed, retry in 2s", "sys"); setTimeout(connectWs, 2000); };
  ws.onerror = () => {};
  ws.onmessage = ev => {
    try {
      const msg = JSON.parse(ev.data);
      handleEvent(msg);
    } catch (e) { log("ws parse err: " + e, "err"); }
  };
}

function handleEvent(msg) {
  switch (msg.type) {
    case "snapshot": applySnapshot(msg.data); break;
    case "pid":      onPid(msg.data); break;
    case "line":     onLine(msg.data); break;
    case "app":      onApp(msg.data); break;
    case "gain":     onGain(msg.data); break;
    case "cfg":      onCfg(msg.data); break;
    case "blackbox": onBlackbox(msg.data); break;
    case "status":   onStatus(msg.data); break;
    case "ai_tune":  onAiTune(msg.data); break;
    case "ai_auto":  onAiAuto(msg.data); break;
    case "log":      log(msg.data.text, msg.data.text.startsWith(">>") ? "send" : "recv"); break;
  }
}

function applySnapshot(s) {
  state.connected = s.connected;
  state.port = s.port || state.port;
  state.baud = s.baud || state.baud;
  state.frames = s.frame_count || 0;
  state.csv_path = s.csv_path || null;
  if (s.gains) Object.assign(state.gains, s.gains);
  if (s.params) state.params = s.params;
  if (s.latest_line) state.line = { ...state.line, ...s.latest_line };
  if (s.latest_app) state.app = s.latest_app;
  if (s.ai_auto) onAiAuto(s.ai_auto);
  if (s.channels) {
    for (const ch of CH) {
      if (!s.channels[ch]) continue;
      const src = s.channels[ch];
      state.channels[ch] = {
        t: src.t || [], sp: src.sp || [], meas: src.meas || [],
        out: src.out || [], err: src.err || [],
      };
    }
  }
  refreshConn();
  refreshGainInputs();
  renderParams();
  draw();
}

function onPid(rec) {
  const ch = rec.ch;
  const s = state.channels[ch];
  if (!s) return;
  // ts ms -> normalized seconds (we keep raw ms, draw uses last N)
  s.t.push(rec.ts_ms);
  s.sp.push(rec.sp);
  s.meas.push(rec.meas);
  s.out.push(rec.out);
  s.err.push(rec.err);
  if (s.t.length > TRACE_LEN) {
    s.t.splice(0, s.t.length - TRACE_LEN);
    s.sp.splice(0, s.sp.length - TRACE_LEN);
    s.meas.splice(0, s.meas.length - TRACE_LEN);
    s.out.splice(0, s.out.length - TRACE_LEN);
    s.err.splice(0, s.err.length - TRACE_LEN);
  }
  // per-card stat
  const card = document.querySelector(`.pid-card[data-ch="${ch}"]`);
  if (card) {
    setStat(card, "sp", fmt(rec.sp, 3));
    setStat(card, "meas", fmt(rec.meas, 3));
    setStat(card, "out", fmt(rec.out, 1));
    setStat(card, "err", fmt(rec.err, 3));
  }
  // top stats
  if (ch === "L") setText("#stat-l-meas", fmt(rec.meas, 3));
  if (ch === "R") setText("#stat-r-meas", fmt(rec.meas, 3));
  if (ch === "LINE") setText("#stat-line-bias", fmt(rec.meas, 3));
  state.frames++;
  state.lastFrameTs = performance.now();
}

function onLine(rec) {
  state.line = { ...state.line, ...rec };
  setText("#line-strength", rec.strength);
  setText("#line-on", rec.on_line ? "✓" : "×");
  setText("#stat-line-bias", fmt(rec.bias, 3));
}

function onApp(rec) {
  state.app = rec;
  setText("#stat-mission", `H${rec.mission} ${rec.state_name}`);
  setText("#stat-heading", `${fmt(rec.theta_deg, 0)}°`);
  setText("#stat-runtime", `${fmt(rec.mission_time_ms/1000, 1)} s`);
}

function onGain(rec) {
  const ch = rec.ch.toUpperCase();
  if (!state.gains[ch]) return;
  state.gains[ch] = { kp: rec.kp, ki: rec.ki, kd: rec.kd };
  refreshGainInputs();
}

function onCfg(rec) {
  state.params[rec.param] = rec;
  renderParams();
}

function onBlackbox(d) {
  state.blackbox.count = d.count;
  if (d.done) state.blackbox.active = false;
  setText("#bb-status", `${d.count} 帧${d.done ? "（已完成）" : "（导出中…）"}`);
}

function onStatus(d) {
  state.connected = !!d.connected;
  refreshConn();
  log(d.text, "sys");
}

function onAiTune(d) {
  state.ai.results = d.results || [];
  renderAiResults(state.ai.results);
  setText("#ai-status", d.applied ? "建议已应用" : "建议已生成");
}

function onAiAuto(d) {
  if (!d) return;
  state.aiAuto = { ...state.aiAuto, ...d };
  if (d.last_results?.length) {
    state.ai.results = d.last_results;
    renderAiResults(d.last_results);
  }
  const running = !!d.running;
  setText("#ai-auto-state", running ? "自动调试运行中" : "自动调试已停止");
  setText("#ai-auto-round", `${d.round || 0} / ${d.max_rounds || 0} 轮`);
  setText("#ai-auto-stable", `稳定 ${d.stable_rounds || 0} 轮`);
  setText("#ai-auto-reason", d.reason ? `原因: ${d.reason}` : "");
  setText("#ai-status", running ? "自动闭环调参中" : (d.reason ? `已停止: ${d.reason}` : "等待采样"));
  const startBtn = $("#btn-ai-auto-start");
  const stopBtn = $("#btn-ai-auto-stop");
  if (startBtn) startBtn.disabled = running;
  if (stopBtn) stopBtn.disabled = !running;
}

function refreshConn() {
  const dot = $("#conn-dot");
  const txt = $("#conn-text");
  if (state.connected) {
    dot.classList.add("on"); dot.classList.remove("warn");
    txt.textContent = state.port || "connected";
  } else {
    dot.classList.remove("on", "warn");
    txt.textContent = "未连接";
  }
  setText("#csv-path", state.csv_path ? `日志: ${state.csv_path}` : "尚未连接");
}

function refreshGainInputs() {
  for (const ch of CH) {
    const g = state.gains[ch];
    if (!g) continue;
    for (const k of ["kp", "ki", "kd"]) {
      const el = document.querySelector(`[data-gain="${ch}:${k.toUpperCase()}"]`);
      if (!el) continue;
      if (document.activeElement === el) continue;
      if (g[k] !== null && g[k] !== undefined) {
        el.value = (+g[k]).toFixed(4).replace(/\.?0+$/, "") || "0";
      }
    }
  }
}

function renderParams() {
  const grid = $("#params-grid");
  if (!grid) return;
  const entries = Object.entries(state.params).sort();
  grid.innerHTML = "";
  for (const [name, rec] of entries) {
    const div = document.createElement("div");
    div.className = "param-item";
    div.innerHTML = `
      <div class="pname">${name}</div>
      <div class="prange">范围 [${rec.min}, ${rec.max}]</div>
      <div class="row">
        <input type="number" step="any" value="${rec.value}" data-param="${name}">
        <button class="btn small primary" data-param-apply="${name}">写入</button>
      </div>`;
    grid.appendChild(div);
  }
  grid.querySelectorAll("[data-param-apply]").forEach(btn => {
    btn.addEventListener("click", async () => {
      const name = btn.dataset.paramApply;
      const input = grid.querySelector(`[data-param="${name}"]`);
      const v = parseFloat(input.value);
      if (Number.isNaN(v)) return;
      const r = await api("/api/cfg/set", { name, value: v });
      if (!r.ok) log("cfg set failed: " + (r.error || ""), "err");
    });
  });
}

function renderAiResults(results) {
  const wrap = $("#ai-results");
  if (!wrap) return;
  wrap.innerHTML = "";
  if (!results || results.length === 0) {
    wrap.innerHTML = `<div class="muted">还没有建议。</div>`;
    return;
  }
  for (const rec of results) {
    const div = document.createElement("div");
    div.className = "ai-result";
    if (rec.error) {
      div.innerHTML = `
        <div class="ai-result-head">
          <strong>${rec.ch}</strong>
          <span class="badge warn">NO DATA</span>
        </div>
        <div class="ai-error">${rec.error}</div>`;
      wrap.appendChild(div);
      continue;
    }
    const m = rec.metrics || {};
    const flags = rec.flags || {};
    const flagText = Object.entries(flags).filter(([, v]) => v).map(([k]) => k).join(" · ") || "stable";
    const current = rec.current || {};
    const suggested = rec.suggested || {};
    const rows = ["kp", "ki", "kd"].map(k => `
      <tr>
        <td>${k.toUpperCase()}</td>
        <td>${fmt(current[k], 5)}</td>
        <td>${fmt(suggested[k], 5)}</td>
        <td class="${suggested[k] > current[k] ? "up" : suggested[k] < current[k] ? "down" : ""}">
          ${suggested[k] > current[k] ? "↑" : suggested[k] < current[k] ? "↓" : "·"}
        </td>
      </tr>`).join("");
    div.innerHTML = `
      <div class="ai-result-head">
        <strong>${rec.ch}</strong>
        <span class="badge ${rec.confidence === "high" ? "ok" : rec.confidence === "medium" ? "line" : "warn"}">${rec.provider || "local"} · ${rec.confidence}</span>
      </div>
      <div class="ai-metrics">
        <span>样本 ${rec.samples}</span>
        <span>MAE ${fmt(m.mae, 5)}</span>
        <span>bias ${fmt(m.bias, 5)}</span>
        <span>σ ${fmt(m.sigma, 5)}</span>
        <span>过零 ${m.sign_changes ?? 0}</span>
      </div>
      <div class="ai-flags">${flagText}</div>
      <table class="ai-table">
        <thead><tr><th></th><th>当前</th><th>建议</th><th></th></tr></thead>
        <tbody>${rows}</tbody>
      </table>
      <ul class="ai-notes">${(rec.notes || []).map(n => `<li>${n}</li>`).join("")}</ul>`;
    wrap.appendChild(div);
  }
}

// -------- drawing --------
function deviceScale(canvas) {
  const dpr = window.devicePixelRatio || 1;
  const rect = canvas.getBoundingClientRect();
  if (canvas.width !== rect.width * dpr || canvas.height !== rect.height * dpr) {
    canvas.width = rect.width * dpr;
    canvas.height = rect.height * dpr;
  }
  const ctx = canvas.getContext("2d");
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  return { ctx, w: rect.width, h: rect.height };
}

function drawGauge(canvas, ch) {
  const { ctx, w, h } = deviceScale(canvas);
  ctx.clearRect(0, 0, w, h);
  const cx = w / 2, cy = h / 2 + 6;
  const r = Math.min(w, h) / 2 - 18;
  const range = GAUGE_RANGE[ch];
  const series = state.channels[ch];
  const last = series.meas.length ? series.meas[series.meas.length - 1] : null;
  const sp   = series.sp.length   ? series.sp[series.sp.length - 1]     : null;

  // bezel
  ctx.strokeStyle = "#1f2532"; ctx.lineWidth = 14;
  ctx.beginPath();
  ctx.arc(cx, cy, r, Math.PI * 0.75, Math.PI * 2.25);
  ctx.stroke();

  // value arc
  if (last !== null) {
    const frac = Math.max(0, Math.min(1, (last - range.min) / (range.max - range.min)));
    const a0 = Math.PI * 0.75;
    const a1 = a0 + Math.PI * 1.5 * frac;
    const grad = ctx.createLinearGradient(0, 0, w, 0);
    grad.addColorStop(0, COLORS[ch]);
    grad.addColorStop(1, "#ffffff22");
    ctx.strokeStyle = COLORS[ch];
    ctx.lineWidth = 14;
    ctx.lineCap = "round";
    ctx.beginPath();
    ctx.arc(cx, cy, r, a0, a1);
    ctx.stroke();
  }

  // sp ticker
  if (sp !== null) {
    const frac = Math.max(0, Math.min(1, (sp - range.min) / (range.max - range.min)));
    const a = Math.PI * 0.75 + Math.PI * 1.5 * frac;
    ctx.strokeStyle = COLORS.sp; ctx.lineWidth = 3;
    ctx.beginPath();
    ctx.moveTo(cx + Math.cos(a) * (r - 16), cy + Math.sin(a) * (r - 16));
    ctx.lineTo(cx + Math.cos(a) * (r + 8), cy + Math.sin(a) * (r + 8));
    ctx.stroke();
  }

  // center text
  ctx.fillStyle = "#e6e9ef";
  ctx.textAlign = "center";
  ctx.font = "600 26px Inter, sans-serif";
  ctx.fillText(last === null ? "--" : (+last).toFixed(2), cx, cy + 2);
  ctx.fillStyle = "#8a93a4";
  ctx.font = "11px Inter, sans-serif";
  ctx.fillText(`${range.label} (${range.unit})`, cx, cy + 22);
  // range labels
  ctx.font = "10px Inter, sans-serif";
  ctx.fillStyle = "#5e6675";
  ctx.textAlign = "left";
  ctx.fillText(String(range.min), 6, h - 6);
  ctx.textAlign = "right";
  ctx.fillText(String(range.max), w - 6, h - 6);
}

function drawTrace(canvas, ch) {
  const { ctx, w, h } = deviceScale(canvas);
  ctx.clearRect(0, 0, w, h);
  const s = state.channels[ch];
  const n = s.t.length;
  if (n < 2) {
    ctx.fillStyle = "#5e6675"; ctx.font = "11px Inter";
    ctx.fillText("等待遥测…", 10, 18);
    return;
  }
  const pad = 8;
  // y range from sp + meas
  let ymin = Infinity, ymax = -Infinity;
  for (let i = 0; i < n; i++) {
    ymin = Math.min(ymin, s.sp[i], s.meas[i]);
    ymax = Math.max(ymax, s.sp[i], s.meas[i]);
  }
  if (ymin === ymax) { ymin -= 1; ymax += 1; }
  const yspan = ymax - ymin;
  ymin -= yspan * 0.08; ymax += yspan * 0.08;
  // grid
  ctx.strokeStyle = "#1d2330"; ctx.lineWidth = 1;
  for (let i = 1; i < 4; i++) {
    const y = pad + (h - 2 * pad) * (i / 4);
    ctx.beginPath(); ctx.moveTo(pad, y); ctx.lineTo(w - pad, y); ctx.stroke();
  }
  const xAt = i => pad + (w - 2 * pad) * (i / (n - 1));
  const yAt = v => h - pad - (h - 2 * pad) * ((v - ymin) / (ymax - ymin));
  // meas
  ctx.strokeStyle = COLORS[ch]; ctx.lineWidth = 1.6;
  ctx.beginPath();
  for (let i = 0; i < n; i++) {
    const x = xAt(i), y = yAt(s.meas[i]);
    if (i === 0) ctx.moveTo(x, y); else ctx.lineTo(x, y);
  }
  ctx.stroke();
  // sp dashed
  ctx.strokeStyle = COLORS.sp; ctx.setLineDash([4, 4]); ctx.lineWidth = 1.4;
  ctx.beginPath();
  for (let i = 0; i < n; i++) {
    const x = xAt(i), y = yAt(s.sp[i]);
    if (i === 0) ctx.moveTo(x, y); else ctx.lineTo(x, y);
  }
  ctx.stroke();
  ctx.setLineDash([]);

  // legend
  ctx.font = "11px Inter";
  ctx.fillStyle = COLORS[ch]; ctx.fillRect(w - 78, 8, 10, 3);
  ctx.fillStyle = "#cbd3e0"; ctx.fillText("meas", w - 64, 13);
  ctx.fillStyle = COLORS.sp; ctx.fillRect(w - 38, 8, 10, 3);
  ctx.fillStyle = "#cbd3e0"; ctx.fillText("sp", w - 22, 13);
}

function drawLineBars(canvas) {
  const { ctx, w, h } = deviceScale(canvas);
  ctx.clearRect(0, 0, w, h);
  const raw = state.line.raw || [];
  const n = raw.length || 7;
  const pad = 18;
  const bw = (w - pad * 2) / n - 6;
  const max = Math.max(1, ...raw, 100);
  const peakIdx = raw.indexOf(Math.max(...raw));
  for (let i = 0; i < n; i++) {
    const v = raw[i] || 0;
    const x = pad + i * (bw + 6);
    const bh = (h - pad * 2) * (v / max);
    const y = h - pad - bh;
    ctx.fillStyle = i === peakIdx ? COLORS.LINE : "#3a4255";
    ctx.fillRect(x, y, bw, bh);
    ctx.fillStyle = "#5e6675";
    ctx.font = "10px Inter";
    ctx.textAlign = "center";
    ctx.fillText("S" + i, x + bw / 2, h - 4);
    ctx.fillStyle = i === peakIdx ? "#fff" : "#8a93a4";
    ctx.fillText(String(v), x + bw / 2, y - 4);
  }
}

function draw() {
  document.querySelectorAll("canvas[data-gauge]").forEach(c => drawGauge(c, c.dataset.gauge));
  document.querySelectorAll("canvas[data-trace]").forEach(c => drawTrace(c, c.dataset.trace));
  const bars = $("#line-bars");
  if (bars) drawLineBars(bars);
}

// frame rate display
setInterval(() => {
  const now = performance.now();
  const dt = (now - (state.lastRateCheck || now)) / 1000;
  const rate = dt > 0 ? Math.round((state.frames - (state.lastFrames || 0)) / dt) : 0;
  state.lastFrames = state.frames;
  state.lastRateCheck = now;
  setText("#rate-text", rate + " Hz");
  setText("#frames-text", state.frames + " 帧");
}, 1000);

// animation loop
function tick() { draw(); requestAnimationFrame(tick); }

// -------- UI wiring --------
function wireUi() {
  $$(".nav-item").forEach(btn => {
    btn.addEventListener("click", () => {
      const v = btn.dataset.view;
      $$(".nav-item").forEach(b => b.classList.toggle("active", b === btn));
      $$(".view").forEach(s => s.classList.toggle("active", s.dataset.view === v));
    });
  });
  $("#btn-connect").addEventListener("click", connect);
  $("#btn-disconnect").addEventListener("click", disconnect);
  $("#btn-resume").addEventListener("click", () => api("/api/log/resume", {}));
  $("#btn-pause").addEventListener("click", () => api("/api/log/pause", {}));
  $("#btn-reset-pid").addEventListener("click", () => api("/api/log/reset_pid", {}));
  $("#btn-cfgdump").addEventListener("click", () => api("/api/log/dump_cfg", {}));
  $("#btn-clear-log").addEventListener("click", () => { $("#console-out").innerHTML = ""; });
  $("#btn-ai-analyze").addEventListener("click", () => runAiTune(false));
  $("#btn-ai-apply").addEventListener("click", () => runAiTune(true));
  $("#btn-ai-auto-start").addEventListener("click", startAiAuto);
  $("#btn-ai-auto-stop").addEventListener("click", stopAiAuto);

  $$("[data-apply]").forEach(b => b.addEventListener("click", () => applyPid(b.dataset.apply)));
  $$("[data-pull]").forEach(b => b.addEventListener("click", () => pullPid(b.dataset.pull)));
  $$("[data-log]").forEach(b => b.addEventListener("click", () => api(`/api/log/${b.dataset.log}`, {})));
  $("#btn-bb-save").addEventListener("click", async () => {
    const r = await api("/api/blackbox/save", {});
    if (r.ok) log(`已保存 ${r.rows} 帧到 ${r.path}`, "sys");
    else log("保存失败: " + (r.error || ""), "err");
  });

  const form = $("#cmd-form");
  form.addEventListener("submit", e => {
    e.preventDefault();
    const v = $("#cmd-input").value.trim();
    if (!v) return;
    sendCmd(v);
    $("#cmd-input").value = "";
  });
}

async function loadDefaults() {
  try {
    const d = await fetch("/api/defaults").then(r => r.json());
    if (d.port) $("#port-input").value = d.port;
    if (d.baud) $("#baud-input").value = d.baud;
    if (d.autoconnect) connect();
  } catch (e) {}
}

document.addEventListener("DOMContentLoaded", () => {
  wireUi();
  loadDefaults();
  connectWs();
  requestAnimationFrame(tick);
});
