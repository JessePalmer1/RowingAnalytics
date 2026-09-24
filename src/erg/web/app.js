"use strict";

const COLORS = ["#1f5fbf", "#bf5f1f", "#1f8a4c", "#8a1f8a", "#bf2f3f", "#4a4a45"];
const MAX_PIECES = 6;

const state = { workouts: [], selected: [], data: null, playing: false, t: 0, raf: null, last: null };

const $ = (id) => document.getElementById(id);

const mmss = (s, decimals = 1) => {
  if (s === null || s === undefined || Number.isNaN(s)) return "–";
  const sign = s < 0 ? "-" : "";
  const abs = Math.abs(s);
  const m = Math.floor(abs / 60);
  return `${sign}${m}:${(abs % 60).toFixed(decimals).padStart(decimals ? 3 + decimals : 2, "0")}`;
};

const signed = (s) => (s === null || s === undefined ? "–" : `${s > 0 ? "+" : ""}${s.toFixed(2)}s`);

async function loadWorkouts() {
  const cls = $("class-filter").value;
  const query = cls ? `?class=${encodeURIComponent(cls)}&limit=100` : "?limit=100";
  const rows = await (await fetch(`/workouts${query}`)).json();
  state.workouts = rows;
  state.selected = [];
  renderList();
  syncButton();
}

function renderList() {
  const list = $("workout-list");
  list.innerHTML = "";
  if (!state.workouts.length) {
    list.innerHTML = '<li class="hint">No workouts in this class.</li>';
    return;
  }
  for (const w of state.workouts) {
    const li = document.createElement("li");
    const pace = w.avg_pace_s_500 ? `${mmss(w.avg_pace_s_500)}/500m` : "–";
    // The input stays outside the label: nesting it makes a direct click toggle twice.
    li.innerHTML = `<input type="checkbox" id="w${w.id}" value="${w.id}">
      <label for="w${w.id}">
        <span class="date">${w.date}</span>
        <span class="meta">${(w.work_distance_m / 1000).toFixed(2)}km · ${pace}${w.hr_avg ? ` · ${w.hr_avg}bpm` : ""}</span>
      </label>`;
    li.querySelector("input").addEventListener("change", (e) => toggle(Number(e.target.value), e.target));
    list.appendChild(li);
  }
}

function toggle(id, input) {
  const at = state.selected.indexOf(id);
  if (!input.checked) {
    if (at >= 0) state.selected.splice(at, 1);
  } else if (at < 0) {
    if (state.selected.length >= MAX_PIECES) {
      input.checked = false;
      return;
    }
    state.selected.push(id);
  }
  syncButton();
}

function syncButton() {
  const n = state.selected.length;
  $("compare").disabled = n < 2;
  $("compare").textContent = n < 2 ? "Compare" : `Compare ${n} pieces`;
}

async function compare() {
  const res = await fetch(`/workouts/compare?ids=${state.selected.join(",")}`);
  if (!res.ok) {
    alert((await res.json()).detail || "Comparison failed");
    return;
  }
  state.data = await res.json();
  state.data.pieces.forEach((p, i) => (p.color = COLORS[i % COLORS.length]));
  state.t = 0;
  stop();

  $("results").hidden = false;
  const note = $("note");
  note.hidden = !state.data.note;
  note.textContent = state.data.note || "";
  $("segment-label").textContent = `every ${state.data.segment_m}m`;

  renderSplits();
  drawAll();
  $("results").scrollIntoView({ behavior: "smooth", block: "start" });
}

// ---- charts -------------------------------------------------------------

function setupCanvas(canvas) {
  const ratio = window.devicePixelRatio || 1;
  // Remember the CSS height from the markup: reading canvas.height back would compound
  // the pixel-ratio scaling on every redraw.
  const height = Number(canvas.dataset.baseHeight || canvas.getAttribute("height"));
  canvas.dataset.baseHeight = String(height);
  const width = canvas.clientWidth;
  canvas.style.height = `${height}px`;
  canvas.width = Math.round(width * ratio);
  canvas.height = Math.round(height * ratio);
  const ctx = canvas.getContext("2d");
  ctx.setTransform(ratio, 0, 0, ratio, 0, 0);
  ctx.clearRect(0, 0, width, height);
  return { ctx, width, height };
}

function lineChart(canvasId, key, { invert = false, format = (v) => v.toFixed(0) } = {}) {
  const canvas = $(canvasId);
  const { ctx, width, height } = setupCanvas(canvas);
  const pad = { left: 52, right: 12, top: 10, bottom: 26 };
  const pieces = state.data.pieces;
  const values = pieces.flatMap((p) => p.series[key]).filter((v) => v !== null);
  if (!values.length) {
    ctx.fillStyle = "#6b6b66";
    ctx.fillText("no data", pad.left, height / 2);
    return;
  }

  // Percentile bounds: the first strokes of a piece are far slower than the rest and
  // would otherwise squash the whole race into a sliver of the chart.
  const sorted = [...values].sort((a, b) => a - b);
  const at = (q) => sorted[Math.min(sorted.length - 1, Math.max(0, Math.round(q * (sorted.length - 1))))];
  let min = at(0.02);
  let max = at(0.98);
  if (min === max) {
    min -= 1;
    max += 1;
  }
  const margin = (max - min) * 0.08;
  min -= margin;
  max += margin;
  const maxD = state.data.aligned_distance_m;
  const x = (d) => pad.left + (d / maxD) * (width - pad.left - pad.right);
  const y = (v) => {
    const clamped = Math.min(max, Math.max(min, v));
    const frac = (clamped - min) / (max - min);
    return invert
      ? pad.top + frac * (height - pad.top - pad.bottom)
      : height - pad.bottom - frac * (height - pad.top - pad.bottom);
  };

  drawAxes(ctx, { width, height, pad, min, max, maxD, y, x, format });

  for (const piece of pieces) {
    ctx.strokeStyle = piece.color;
    ctx.lineWidth = 1.6;
    ctx.beginPath();
    let open = false;
    piece.series[key].forEach((v, i) => {
      if (v === null) {
        open = false;
        return;
      }
      const px = x(piece.series.distance_m[i]);
      const py = y(v);
      if (!open) {
        ctx.moveTo(px, py);
        open = true;
      } else ctx.lineTo(px, py);
    });
    ctx.stroke();
  }
  markCursor(ctx, x, pad, height);
}

function drawAxes(ctx, { width, height, pad, min, max, maxD, y, x, format }) {
  ctx.strokeStyle = "#e0e0da";
  ctx.fillStyle = "#6b6b66";
  ctx.lineWidth = 1;
  ctx.font = "11px system-ui, sans-serif";
  ctx.textAlign = "right";
  ctx.textBaseline = "middle";
  for (let i = 0; i <= 4; i++) {
    const v = min + ((max - min) * i) / 4;
    const py = Math.round(y(v)) + 0.5;
    ctx.beginPath();
    ctx.moveTo(pad.left, py);
    ctx.lineTo(width - pad.right, py);
    ctx.stroke();
    ctx.fillText(format(v), pad.left - 8, py);
  }
  ctx.textAlign = "center";
  ctx.textBaseline = "top";
  for (let i = 0; i <= 4; i++) {
    const d = (maxD * i) / 4;
    ctx.fillText(`${Math.round(d)}m`, x(d), height - pad.bottom + 8);
  }
}

function markCursor(ctx, x, pad, height) {
  const d = currentDistance();
  if (d === null) return;
  ctx.strokeStyle = "rgba(0,0,0,0.25)";
  ctx.lineWidth = 1;
  ctx.beginPath();
  ctx.moveTo(x(d), pad.top);
  ctx.lineTo(x(d), height - pad.bottom);
  ctx.stroke();
}

function deltaChart() {
  const canvas = $("delta-chart");
  const { ctx, width, height } = setupCanvas(canvas);
  const pad = { left: 52, right: 12, top: 10, bottom: 26 };
  const pieces = state.data.pieces;
  const reference = pieces[0];
  const deltas = pieces.map((p) =>
    p.series.time_s.map((t, i) => (t === null || reference.series.time_s[i] === null ? null : t - reference.series.time_s[i]))
  );
  const values = deltas.flat().filter((v) => v !== null);
  const bound = Math.max(1, ...values.map(Math.abs)) * 1.15;
  const maxD = state.data.aligned_distance_m;
  const x = (d) => pad.left + (d / maxD) * (width - pad.left - pad.right);
  const y = (v) => height - pad.bottom - ((v + bound) / (2 * bound)) * (height - pad.top - pad.bottom);

  drawAxes(ctx, { width, height, pad, min: -bound, max: bound, maxD, y, x, format: (v) => `${v > 0 ? "+" : ""}${v.toFixed(1)}s` });
  ctx.strokeStyle = "#9a9a94";
  ctx.beginPath();
  ctx.moveTo(pad.left, Math.round(y(0)) + 0.5);
  ctx.lineTo(width - pad.right, Math.round(y(0)) + 0.5);
  ctx.stroke();

  pieces.forEach((piece, idx) => {
    if (idx === 0) return;
    ctx.strokeStyle = piece.color;
    ctx.lineWidth = 1.8;
    ctx.beginPath();
    let open = false;
    deltas[idx].forEach((v, i) => {
      if (v === null) {
        open = false;
        return;
      }
      const px = x(piece.series.distance_m[i]);
      const py = y(v);
      if (!open) {
        ctx.moveTo(px, py);
        open = true;
      } else ctx.lineTo(px, py);
    });
    ctx.stroke();
  });
  markCursor(ctx, x, pad, height);
}

// ---- ghost race ---------------------------------------------------------

function distanceAt(piece, time) {
  const times = piece.series.time_s;
  const dist = piece.series.distance_m;
  if (time <= 0) return 0;
  if (time >= times[times.length - 1]) return dist[dist.length - 1];
  let lo = 0;
  let hi = times.length - 1;
  while (lo < hi - 1) {
    const mid = (lo + hi) >> 1;
    if (times[mid] <= time) lo = mid;
    else hi = mid;
  }
  const span = times[hi] - times[lo];
  const weight = span > 0 ? (time - times[lo]) / span : 0;
  return dist[lo] + (dist[hi] - dist[lo]) * weight;
}

function valueAt(piece, key, distance) {
  const dist = piece.series.distance_m;
  let lo = 0;
  let hi = dist.length - 1;
  while (lo < hi - 1) {
    const mid = (lo + hi) >> 1;
    if (dist[mid] <= distance) lo = mid;
    else hi = mid;
  }
  return piece.series[key][hi] ?? piece.series[key][lo];
}

function currentDistance() {
  if (!state.data) return null;
  return distanceAt(state.data.pieces[0], state.t);
}

function raceDuration() {
  return Math.max(...state.data.pieces.map((p) => p.aligned_time_s || 0));
}

function drawRace() {
  const canvas = $("race");
  const { ctx, width, height } = setupCanvas(canvas);
  const pieces = state.data.pieces;
  const pad = { left: 12, right: 12, top: 28, bottom: 24 };
  const laneHeight = (height - pad.top - pad.bottom) / pieces.length;
  const maxD = state.data.aligned_distance_m;
  const x = (d) => pad.left + (d / maxD) * (width - pad.left - pad.right);

  ctx.font = "11px system-ui, sans-serif";
  ctx.fillStyle = "#6b6b66";
  ctx.textAlign = "center";
  ctx.textBaseline = "top";
  for (let i = 0; i <= 4; i++) {
    const d = (maxD * i) / 4;
    ctx.strokeStyle = "#ececE6";
    ctx.beginPath();
    ctx.moveTo(Math.round(x(d)) + 0.5, pad.top - 6);
    ctx.lineTo(Math.round(x(d)) + 0.5, height - pad.bottom);
    ctx.stroke();
    ctx.fillText(`${Math.round(d)}m`, x(d), height - pad.bottom + 6);
  }

  pieces.forEach((piece, i) => {
    const laneY = pad.top + laneHeight * i + laneHeight / 2;
    ctx.strokeStyle = "#e6e6e0";
    ctx.lineWidth = 8;
    ctx.lineCap = "round";
    ctx.beginPath();
    ctx.moveTo(x(0), laneY);
    ctx.lineTo(x(maxD), laneY);
    ctx.stroke();

    const d = distanceAt(piece, state.t);
    ctx.strokeStyle = piece.color;
    ctx.beginPath();
    ctx.moveTo(x(0), laneY);
    ctx.lineTo(x(d), laneY);
    ctx.stroke();

    ctx.fillStyle = piece.color;
    ctx.beginPath();
    ctx.arc(x(d), laneY, 7, 0, Math.PI * 2);
    ctx.fill();

    ctx.fillStyle = "#1b1b1a";
    ctx.textAlign = "left";
    ctx.textBaseline = "bottom";
    ctx.fillText(piece.date, x(0), laneY - 10);
  });

  $("clock").textContent = mmss(state.t);
  renderLive();
}

function renderLive() {
  const pieces = state.data.pieces;
  const reference = pieces[0];
  const refD = distanceAt(reference, state.t);
  const rows = pieces
    .map((p) => {
      const d = distanceAt(p, state.t);
      const behind = d - refD;
      const pace = valueAt(p, "pace_s_500", d);
      const spm = valueAt(p, "spm", d);
      const hr = valueAt(p, "hr", d);
      const gap = p === reference ? "reference" : `${behind >= 0 ? "+" : ""}${behind.toFixed(1)}m`;
      const cls = p === reference ? "" : behind >= 0 ? "gain" : "loss";
      return `<tr>
        <td><span class="swatch" style="background:${p.color}"></span>${p.date}</td>
        <td>${d.toFixed(0)}m</td>
        <td>${pace ? mmss(pace) : "–"}</td>
        <td>${spm ? Math.round(spm) + " spm" : "–"}</td>
        <td>${hr ? Math.round(hr) + " bpm" : "–"}</td>
        <td class="${cls}">${gap}</td>
      </tr>`;
    })
    .join("");
  $("live").innerHTML = `<thead><tr>
      <th>Piece</th><th>Distance</th><th>Pace</th><th>Rate</th><th>HR</th><th>Gap</th>
    </tr></thead><tbody>${rows}</tbody>`;
}

function renderSplits() {
  const pieces = state.data.pieces;
  const reference = pieces[0];
  const head = reference.splits
    .map((s) => `<th>${s.from_m}–${s.to_m}m</th>`)
    .join("");
  const rows = pieces
    .map((p) => {
      const cells = p.splits
        .map((s, i) => {
          const time = p.splits[i].other_s;
          if (p === reference) return `<td>${mmss(time)}</td>`;
          const delta = s.delta_s;
          const cls = delta === null ? "" : delta > 0 ? "loss" : "gain";
          return `<td>${mmss(time)}<br><span class="${cls}">${signed(delta)}</span></td>`;
        })
        .join("");
      const total = p.is_reference ? mmss(p.aligned_time_s) : `${mmss(p.aligned_time_s)}<br><span class="${p.total_delta_s > 0 ? "loss" : "gain"}">${signed(p.total_delta_s)}</span>`;
      return `<tr><td><span class="swatch" style="background:${p.color}"></span>${p.date}</td>${cells}<td>${total}</td></tr>`;
    })
    .join("");
  $("splits").innerHTML = `<thead><tr><th>Piece</th>${head}<th>Total</th></tr></thead><tbody>${rows}</tbody>`;
}

function drawAll() {
  drawRace();
  lineChart("pace-chart", "pace_s_500", { invert: true, format: (v) => mmss(v, 0) });
  deltaChart();
  lineChart("hr-chart", "hr", { format: (v) => v.toFixed(0) });
  lineChart("dps-chart", "dps_m", { format: (v) => v.toFixed(1) });
  $("scrub").value = String((state.t / raceDuration()) * 1000);
}

function tick(now) {
  if (!state.playing) return;
  const speed = Number($("speed").value);
  if (state.last !== null) state.t += ((now - state.last) / 1000) * speed;
  state.last = now;
  if (state.t >= raceDuration()) {
    state.t = raceDuration();
    stop();
  }
  drawAll();
  if (state.playing) state.raf = requestAnimationFrame(tick);
}

function play() {
  if (!state.data) return;
  if (state.t >= raceDuration()) state.t = 0;
  state.playing = true;
  state.last = null;
  $("play").textContent = "Pause";
  state.raf = requestAnimationFrame(tick);
}

function stop() {
  state.playing = false;
  if (state.raf) cancelAnimationFrame(state.raf);
  state.raf = null;
  $("play").textContent = "Play";
}

$("class-filter").addEventListener("change", loadWorkouts);
$("compare").addEventListener("click", compare);
$("play").addEventListener("click", () => (state.playing ? stop() : play()));
$("scrub").addEventListener("input", (e) => {
  if (!state.data) return;
  stop();
  state.t = (Number(e.target.value) / 1000) * raceDuration();
  drawAll();
});
window.addEventListener("resize", () => state.data && drawAll());

loadWorkouts();
