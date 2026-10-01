"use strict";

// Pieces tab: every workout with its class, a dropdown to override it, and the
// classification settings. Shares $, api, mmss, tabs and replayStale with app.js.

const CLASS_LABELS = {
  test_2k: "2k test",
  test_6k: "6k test",
  test_10k: "10k test",
  interval: "Interval",
  steady: "Steady",
  short_piece: "Short piece",
  unknown: "Unknown",
};

const pieces = { rows: [], settings: null, highlight: new Set(), loaded: false };

const escapeHtml = (value) =>
  String(value ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]);

function duration(seconds) {
  const s = Math.round(seconds);
  const h = Math.floor(s / 3600);
  const m = Math.floor((s % 3600) / 60);
  const sec = String(s % 60).padStart(2, "0");
  return h ? `${h}:${String(m).padStart(2, "0")}:${sec}` : `${m}:${sec}`;
}

/** "2:04", "2:04.5" or "124.5" -> seconds per 500m. */
function parsePace(text) {
  const value = text.trim();
  if (!value) return null;
  const match = value.match(/^(\d+):(\d{1,2}(?:\.\d+)?)$/);
  if (match) return Number(match[1]) * 60 + Number(match[2]);
  const seconds = Number(value);
  return Number.isFinite(seconds) ? seconds : NaN;
}

function showStatus(message, kind = "ok") {
  const el = $("status");
  el.hidden = false;
  el.className = `status ${kind}`;
  el.textContent = message;
}

function describeChange(result) {
  const others = (result.reclassified || []).filter((id) => id !== result.workout_id);
  return others.length
    ? ` ${others.length} other piece${others.length === 1 ? "" : "s"} moved class because your steady baseline shifted.`
    : "";
}

// ---- settings -------------------------------------------------------------

function renderSettings() {
  const s = pieces.settings;
  $("steady-pace").value = s.steady_pace_override ? mmss(s.steady_pace_override) : "";
  $("steady-pace").placeholder = s.steady_pace_auto ? mmss(s.steady_pace_auto) : "2:05.0";
  $("margin").value = s.interval_margin_s ?? 10;
  $("use-learned").hidden = !s.steady_pace_override;

  const line = $("threshold-line");
  if (s.interval_threshold === null) {
    line.textContent = "Not enough history yet to learn your steady pace (needs 5 sessions of 10+ minutes). Set it by hand above.";
    return;
  }
  const source = s.steady_pace_override
    ? `you set steady to ${mmss(s.steady_pace_override)}${s.steady_pace_auto ? ` (learned: ${mmss(s.steady_pace_auto)})` : ""}`
    : `steady learned from your history: ${mmss(s.steady_pace_auto)}`;
  line.innerHTML = `Threshold <b>${mmss(s.interval_threshold)}/500m</b>: anything faster is interval work. <span class="muted">${escapeHtml(source)}.</span>`;
}

async function saveSettings(event) {
  event.preventDefault();
  const pace = parsePace($("steady-pace").value);
  if (Number.isNaN(pace)) {
    showStatus("Steady pace should look like 2:04 or 2:04.5.", "error");
    return;
  }
  const body = { interval_margin_s: Number($("margin").value) };
  if (pace !== null) body.steady_pace_s_500 = pace;
  await submitSettings(body);
}

async function submitSettings(body) {
  $("save-settings").disabled = true;
  showStatus("Reclassifying and recomputing metrics…");
  try {
    const result = await api("/athletes/me/settings", { method: "PUT", body });
    if (result === null) return;
    pieces.settings = result;
    pieces.highlight = new Set(result.reclassified);
    renderSettings();
    await loadPieces();
    replayStale = true;
    const n = result.reclassified.length;
    showStatus(n ? `Saved. ${n} piece${n === 1 ? "" : "s"} changed class.` : "Saved. No pieces changed class.");
  } catch (err) {
    showStatus(err.message, "error");
  } finally {
    $("save-settings").disabled = false;
  }
}

// ---- table ----------------------------------------------------------------

function pieceShape(p) {
  // The description already says whether it was intervals; this is just the monitor's label.
  return (p.workout_type || "").replace(/([a-z])([A-Z])/g, "$1 $2");
}

function renderPieces() {
  const filter = $("pieces-filter").value;
  const visible = pieces.rows.filter((p) => (filter === "manual" ? p.overridden : !filter || p.class === filter));

  const counts = {};
  for (const p of pieces.rows) counts[p.class] = (counts[p.class] || 0) + 1;
  $("class-counts").textContent = Object.keys(CLASS_LABELS)
    .filter((c) => counts[c])
    .map((c) => `${CLASS_LABELS[c]} ${counts[c]}`)
    .join(" · ");

  const options = (selected) =>
    Object.entries(CLASS_LABELS)
      .map(([value, label]) => `<option value="${value}"${value === selected ? " selected" : ""}>${label}</option>`)
      .join("");

  const rows = visible
    .map((p) => {
      const why = p.overridden
        ? `<span class="badge">manual</span> ${escapeHtml(p.override_note || "")} <span class="muted">classifier said ${escapeHtml(CLASS_LABELS[p.classifier_class] || "–")}</span>`
        : escapeHtml(p.classifier_reason || "");
      const classes = [p.overridden ? "manual" : "", pieces.highlight.has(p.id) ? "changed" : ""].join(" ");
      return `<tr class="${classes}" data-id="${p.id}">
        <td>${escapeHtml(p.date)}</td>
        <td class="workout"><div class="desc">${escapeHtml(p.description || "")}</div><div class="shape">${escapeHtml(pieceShape(p))}</div></td>
        <td>${(p.work_distance_m / 1000).toFixed(2)}km</td>
        <td>${duration(p.work_time_s)}</td>
        <td>${p.avg_pace_s_500 ? mmss(p.avg_pace_s_500) : "–"}</td>
        <td>${p.avg_spm ?? "–"}</td>
        <td>${p.hr_avg ?? "–"}</td>
        <td><select class="class-select" aria-label="Class for ${escapeHtml(p.date)}">${options(p.class)}</select></td>
        <td class="why">${why}</td>
        <td>${p.overridden ? '<button class="reset link" type="button">reset</button>' : ""}</td>
      </tr>`;
    })
    .join("");

  $("pieces").innerHTML = `<thead><tr>
      <th>Date</th><th>Workout</th><th>Distance</th><th>Time</th><th>Pace</th><th>Rate</th><th>HR</th>
      <th>Class</th><th>Why</th><th></th>
    </tr></thead><tbody>${rows || '<tr><td colspan="10" class="hint">Nothing in this class.</td></tr>'}</tbody>`;
}

async function loadPieces() {
  const rows = await api("/workouts?limit=1000");
  if (rows === null) return;
  pieces.rows = rows;
  renderPieces();
}

async function changeClass(row, workoutClass) {
  const id = Number(row.dataset.id);
  row.querySelectorAll("select, button").forEach((el) => (el.disabled = true));
  showStatus("Saving and reclassifying…");
  try {
    const result = await api(`/workouts/${id}/classification`, { method: "POST", body: { workout_class: workoutClass } });
    if (result === null) return;
    pieces.highlight = new Set(result.reclassified);
    await loadPieces();
    replayStale = true;
    showStatus(`Set to ${CLASS_LABELS[result.class]}.${describeChange(result)}`);
  } catch (err) {
    showStatus(err.message, "error");
    renderPieces();
  }
}

async function resetClass(row) {
  const id = Number(row.dataset.id);
  row.querySelectorAll("select, button").forEach((el) => (el.disabled = true));
  showStatus("Resetting and reclassifying…");
  try {
    const result = await api(`/workouts/${id}/classification`, { method: "DELETE" });
    if (result === null) return;
    pieces.highlight = new Set(result.reclassified);
    await loadPieces();
    replayStale = true;
    showStatus(`Back to the classifier: ${CLASS_LABELS[result.class]}.${describeChange(result)}`);
  } catch (err) {
    showStatus(err.message, "error");
    renderPieces();
  }
}

// ---- wiring ---------------------------------------------------------------

tabs.pieces = async () => {
  if (pieces.loaded) return;
  pieces.loaded = true;
  try {
    const settings = await api("/athletes/me/settings");
    if (settings === null) return;
    pieces.settings = settings;
    renderSettings();
    await loadPieces();
  } catch (err) {
    pieces.loaded = false;
    showStatus(err.message, "error");
  }
};

$("settings").addEventListener("submit", saveSettings);
$("use-learned").addEventListener("click", () => submitSettings({ auto: true, interval_margin_s: Number($("margin").value) }));
$("pieces-filter").addEventListener("change", renderPieces);
$("pieces").addEventListener("change", (e) => {
  if (e.target.classList.contains("class-select")) changeClass(e.target.closest("tr"), e.target.value);
});
$("pieces").addEventListener("click", (e) => {
  if (e.target.classList.contains("reset")) resetClass(e.target.closest("tr"));
});

// ---- Connect Claude (MCP) ----------------------------------------------------

$("mcp-create").addEventListener("click", async () => {
  try {
    const result = await api("/athletes/me/mcp-token", { method: "POST" });
    if (result === null) return;
    $("mcp-url").value = result.connector_url;
    $("mcp-cmd").value = result.claude_code_command;
    $("mcp-details").hidden = false;
    $("mcp-create").textContent = "Create a new link (revokes this one)";
  } catch (err) {
    showStatus(err.message, "error");
  }
});
document.querySelectorAll(".copy").forEach((input) => input.addEventListener("focus", () => input.select()));
