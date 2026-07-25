/* ============================================================
   Bioreactor Dashboard  —  frontend application
   ============================================================ */
"use strict";

const App = (() => {
  // ── state ──────────────────────────────────────────────────
  let CONFIG = null;          // from /api/config
  let latestData = {};        // bioreactor_id -> {param -> {value,quality,timestamp}}
  let activeBR = null;        // currently selected bioreactor id (null = overview)
  let activeView = "bioreactor"; // 'bioreactor' | 'analytical' | 'nova' | 'vicell' | 'mast-status' | 'sample-history'
  let timeRangeHours = 24;
  let ws = null;
  let wsRetryDelay = 2000;
  let charts = {};            // chart instances keyed by canvas id
  let historyCache = {};      // key: `${br}/${param}/${hours}` -> [{timestamp,value}]
  let selectedParam = null;   // for detail view KPI highlight
  let _novaRowsCache = [];     // text+date filtered rows (used by chart)
  let _novaHistFull  = [];     // full date-range rows before text filter
  let _novaSampFull  = [];     // full sample list before text filter
  let _biohtRowsCache = [];
  let _biohtHistFull  = [];
  let _biohtSampFull  = [];
  let _biohtAnalyteMap = {};   // display label -> [test_abbrev, ...]
  let _biohtDebounceTimer = null;
  let _novaDebounceTimer  = null;

  // ── DOM refs ───────────────────────────────────────────────
  const content   = document.getElementById("content");
  const brNav     = document.getElementById("br-nav");
  const opcBadge  = document.getElementById("opc-badge");
  const clockEl   = document.getElementById("clock");
  const trSelect  = document.getElementById("time-range");

  // ── init ───────────────────────────────────────────────────
  async function init() {
    startClock();
    CONFIG = await fetch("/api/config").then(r => r.json());
    buildNav();
    latestData = await fetch("/api/latest").then(r => r.json());
    renderView();
    connectWS();
    fetch("/api/health").catch(() => {}); // pre-warm SQL connections silently
    trSelect.addEventListener("change", () => {
      timeRangeHours = +trSelect.value;
      historyCache = {};
      renderView();
    });
  }

  function startClock() {
    const update = () => { clockEl.textContent = new Date().toLocaleTimeString(); };
    update();
    setInterval(update, 1000);
  }

  // ── navigation ─────────────────────────────────────────────
  function buildNav() {
    brNav.innerHTML = "";
    const all = makeNavItem("All Bioreactors", null, null, activeBR === null);
    brNav.appendChild(all);

    // Group bioreactors by their group field
    const groups = {};
    CONFIG.bioreactors.forEach(br => {
      const g = br.group || "Other";
      (groups[g] = groups[g] || []).push(br);
    });

    Object.entries(groups).forEach(([groupName, brs]) => {
      const label = document.createElement("p");
      label.style.cssText = "font-size:10px;font-weight:700;letter-spacing:.1em;color:var(--muted);text-transform:uppercase;padding:12px 10px 4px;";
      label.textContent = groupName;
      brNav.appendChild(label);
      brs.forEach(br => {
        brNav.appendChild(makeNavItem(br.name, br.id, br.id, activeBR === br.id));
      });
    });
  }

  function makeNavItem(label, brId, dotId, active) {
    const btn = document.createElement("button");
    btn.className = "nav-item" + (active ? " active" : "");
    if (dotId) {
      const dot = document.createElement("span");
      dot.className = "nav-dot";
      dot.id = "nav-dot-" + dotId;
      btn.appendChild(dot);
    }
    const span = document.createElement("span");
    span.textContent = label;
    btn.appendChild(span);
    btn.addEventListener("click", () => { activeBR = brId; buildNav(); renderView(); });
    return btn;
  }

  function updateNavDots() {
    CONFIG.bioreactors.forEach(br => {
      const dot = document.getElementById("nav-dot-" + br.id);
      if (!dot) return;
      const d = latestData[br.id];
      dot.className = "nav-dot" + (d ? "" : " offline");
    });
  }

  // ── WebSocket ──────────────────────────────────────────────
  function connectWS() {
    const proto = location.protocol === "https:" ? "wss" : "ws";
    ws = new WebSocket(`${proto}://${location.host}/ws`);

    ws.onopen = () => {
      wsRetryDelay = 2000;
      setOpcBadge("connected");
    };

    ws.onmessage = (e) => {
      const msg = JSON.parse(e.data);
      if (msg.type === "snapshot") {
        latestData = msg.data;
        updateView();
      } else if (msg.type === "readings") {
        applyReadings(msg.data);
      }
    };

    ws.onclose = () => {
      setOpcBadge("disconnected");
      setTimeout(connectWS, wsRetryDelay);
      wsRetryDelay = Math.min(wsRetryDelay * 1.5, 30000);
    };

    ws.onerror = () => ws.close();

    // Keep-alive ping
    setInterval(() => { if (ws && ws.readyState === 1) ws.send("ping"); }, 20000);
  }

  function applyReadings(readings) {
    readings.forEach(r => {
      latestData[r.bioreactor] = latestData[r.bioreactor] || {};
      latestData[r.bioreactor][r.parameter] = {
        value: r.value, quality: r.quality, timestamp: r.timestamp
      };
      // Append to chart live if detail view is open
      const cacheKey = `${r.bioreactor}/${r.parameter}/${timeRangeHours}`;
      if (historyCache[cacheKey]) {
        historyCache[cacheKey].push({ timestamp: r.timestamp, value: r.value });
      }
    });
    updateView();
  }

  function setOpcBadge(state) {
    const protocol = CONFIG ? CONFIG.opc_protocol : "OPC";
    if (state === "connected") {
      const isSim = protocol === "SIMULATE";
      opcBadge.className = "badge " + (isSim ? "badge-sim" : "badge-good");
      opcBadge.textContent = isSim ? "OPC: Simulated" : "OPC: Connected";
    } else if (state === "disconnected") {
      opcBadge.className = "badge badge-bad pulse";
      opcBadge.textContent = "OPC: Disconnected";
    } else {
      opcBadge.className = "badge badge-pending";
      opcBadge.textContent = "OPC: Connecting…";
    }
  }

  // ── rendering ──────────────────────────────────────────────
  function renderView() {
    destroyCharts();
    document.getElementById("nav-analytical").classList.toggle("active", activeView === "analytical");
    document.getElementById("nav-nova").classList.toggle("active", activeView === "nova");
    document.getElementById("nav-vicell").classList.toggle("active", activeView === "vicell");
    document.getElementById("nav-mast-status").classList.toggle("active", activeView === "mast-status");
    document.getElementById("nav-sample-history").classList.toggle("active", activeView === "sample-history");
    if (activeView === "analytical")    { renderAnalytical();   return; }
    if (activeView === "nova")          { renderNova();          return; }
    if (activeView === "vicell")        { renderVicell();        return; }
    if (activeView === "mast-status")   { renderMastStatus();   return; }
    if (activeView === "sample-history"){ renderSampleHistory(); return; }
    if (activeBR === null) renderOverview();
    else renderDetail(activeBR);
    updateNavDots();
  }

  function updateView() {
    if (activeView === "analytical") return;
    if (activeView === "nova") return;
    if (activeView === "vicell") return;
    if (activeBR === null) updateOverviewValues();
    else updateDetailKPIs(activeBR);
    updateCharts();
    updateNavDots();
  }

  // ---- Overview ----
  function renderOverview() {
    content.innerHTML = `<h2 class="section-title">All Bioreactors</h2><div class="overview-grid" id="overview-grid"></div>`;
    const grid = document.getElementById("overview-grid");
    CONFIG.bioreactors.forEach(br => {
      grid.appendChild(makeBRCard(br));
    });
  }

  function makeBRCard(br) {
    const card = document.createElement("div");
    card.className = "br-card";
    card.id = "br-card-" + br.id;
    card.addEventListener("click", () => { activeBR = br.id; buildNav(); renderView(); });

    const header = document.createElement("div");
    header.className = "br-card-header";
    header.innerHTML = `<span class="br-card-title">${br.name}</span><span class="badge badge-pending" id="br-badge-${br.id}">–</span>`;
    card.appendChild(header);

    const grid = document.createElement("div");
    grid.className = "br-params";
    grid.id = "br-params-" + br.id;
    card.appendChild(grid);

    fillBRCardParams(br.id);
    return card;
  }

  function fillBRCardParams(brId) {
    const grid = document.getElementById("br-params-" + brId);
    if (!grid) return;
    grid.innerHTML = "";
    const d = latestData[brId] || {};
    Object.entries(CONFIG.parameters).forEach(([key, cfg]) => {
      const val = d[key];
      const div = document.createElement("div");
      div.className = "param-mini";
      div.id = `mini-${brId}-${key}`;
      div.innerHTML = `
        <div class="param-mini-label">${cfg.label}</div>
        <div class="param-mini-value" style="color:${cfg.color}">
          ${val ? fmtVal(val.value) : "–"}<span class="param-mini-unit">${cfg.unit}</span>
        </div>`;
      grid.appendChild(div);
    });
    updateBadge(brId);
  }

  function updateOverviewValues() {
    CONFIG.bioreactors.forEach(br => {
      const d = latestData[br.id] || {};
      Object.entries(CONFIG.parameters).forEach(([key, cfg]) => {
        const el = document.getElementById(`mini-${br.id}-${key}`);
        if (!el) return;
        const val = d[key];
        el.querySelector(".param-mini-value").innerHTML =
          `${val ? fmtVal(val.value) : "–"}<span class="param-mini-unit">${cfg.unit}</span>`;
      });
      updateBadge(br.id);
    });
  }

  function updateBadge(brId) {
    const badge = document.getElementById(`br-badge-${brId}`);
    if (!badge) return;
    const d = latestData[brId] || {};
    const qualities = Object.values(d).map(v => v.quality);
    if (qualities.length === 0) { badge.className = "badge badge-bad"; badge.textContent = "Offline"; return; }
    const bad = qualities.some(q => q === "Bad");
    const sim = qualities.some(q => q === "Simulated");
    badge.className = "badge " + (bad ? "badge-bad" : sim ? "badge-sim" : "badge-good");
    badge.textContent = bad ? "Fault" : sim ? "Simulated" : "Live";
  }

  // ---- Detail view ----
  function renderDetail(brId) {
    const br = CONFIG.bioreactors.find(b => b.id === brId);
    if (!br) return;

    content.innerHTML = `
      <div class="detail-header">
        <button class="back-btn" onclick="App._backToOverview()">← Back</button>
        <h2 class="section-title" style="margin:0">${br.name}</h2>
        <span class="badge badge-pending" id="detail-badge-${brId}">–</span>
      </div>
      <div class="kpi-row" id="kpi-row-${brId}"></div>
      <div class="charts-grid" id="charts-grid-${brId}"></div>`;

    renderKPIs(brId);
    renderCharts(brId);
    updateBadge(brId);
  }

  function renderKPIs(brId) {
    const row = document.getElementById(`kpi-row-${brId}`);
    if (!row) return;
    row.innerHTML = "";
    const d = latestData[brId] || {};
    Object.entries(CONFIG.parameters).forEach(([key, cfg]) => {
      const val = d[key];
      const card = document.createElement("div");
      card.className = "kpi-card" + (selectedParam === key ? " selected" : "");
      card.id = `kpi-${brId}-${key}`;
      card.style.borderTop = `3px solid ${cfg.color}`;
      card.innerHTML = `
        <div class="kpi-label">${cfg.label}</div>
        <div class="kpi-value" style="color:${cfg.color}">
          ${val ? fmtVal(val.value) : "–"}<span class="kpi-unit">${cfg.unit}</span>
        </div>
        <div class="kpi-quality" style="color:${qualityColor(val?.quality)}">${val?.quality || "No data"}</div>`;
      card.addEventListener("click", () => openParamModal(brId, key));
      row.appendChild(card);
    });
  }

  function updateDetailKPIs(brId) {
    const d = latestData[brId] || {};
    Object.entries(CONFIG.parameters).forEach(([key, cfg]) => {
      const card = document.getElementById(`kpi-${brId}-${key}`);
      if (!card) return;
      const val = d[key];
      card.querySelector(".kpi-value").innerHTML =
        `${val ? fmtVal(val.value) : "–"}<span class="kpi-unit">${cfg.unit}</span>`;
      card.querySelector(".kpi-quality").textContent = val?.quality || "No data";
      card.querySelector(".kpi-quality").style.color = qualityColor(val?.quality);
    });
    updateBadge(brId);
  }

  function renderCharts(brId) {
    const grid = document.getElementById(`charts-grid-${brId}`);
    if (!grid) return;
    grid.innerHTML = "";
    Object.entries(CONFIG.parameters).forEach(([key, cfg]) => {
      const card = document.createElement("div");
      card.className = "chart-card";
      card.innerHTML = `
        <div class="chart-card-header">
          <span class="chart-card-title">${cfg.label} <span style="color:var(--muted);font-weight:400">(${cfg.unit})</span></span>
          <button class="btn btn-sm btn-secondary" onclick="App.openParamModal('${brId}','${key}')">Expand</button>
        </div>
        <div class="chart-wrap"><canvas id="chart-${brId}-${key}"></canvas></div>`;
      grid.appendChild(card);
      loadAndRenderChart(brId, key, `chart-${brId}-${key}`, 200);
    });
  }

  async function loadAndRenderChart(brId, param, canvasId, height) {
    const cacheKey = `${brId}/${param}/${timeRangeHours}`;
    if (!historyCache[cacheKey]) {
      const resp = await fetch(`/api/history/${brId}/${param}?hours=${timeRangeHours}`);
      const json = await resp.json();
      historyCache[cacheKey] = json.data;
    }
    const data = historyCache[cacheKey];
    const cfg = CONFIG.parameters[param];
    const canvas = document.getElementById(canvasId);
    if (!canvas) return;

    const points = data.map(r => ({ x: r.timestamp, y: r.value }));
    const chart = new Chart(canvas, {
      type: "line",
      data: {
        datasets: [{
          data: points,
          borderColor: cfg.color,
          backgroundColor: cfg.color + "22",
          borderWidth: 1.5,
          pointRadius: 0,
          pointHoverRadius: 4,
          fill: true,
          tension: 0.3,
        }]
      },
      options: {
        responsive: true,
        maintainAspectRatio: false,
        animation: false,
        interaction: { mode: "index", intersect: false },
        plugins: {
          legend: { display: false },
          tooltip: {
            callbacks: {
              label: ctx => ` ${fmtVal(ctx.parsed.y)} ${cfg.unit}`
            }
          }
        },
        scales: {
          x: {
            type: "time",
            time: { tooltipFormat: "HH:mm:ss", displayFormats: { minute: "HH:mm", hour: "HH:mm", day: "MMM d" } },
            ticks: { color: "#6b7280", maxTicksLimit: 8 },
            grid: { color: "#2a2d3e" }
          },
          y: {
            ticks: { color: "#6b7280" },
            grid: { color: "#2a2d3e" },
            suggestedMin: cfg.min,
            suggestedMax: cfg.max,
          }
        }
      }
    });
    charts[canvasId] = chart;
  }

  function updateCharts() {
    Object.entries(charts).forEach(([canvasId, chart]) => {
      // parse id: chart-{brId}-{param}
      const parts = canvasId.replace("chart-", "").split("-");
      const brId = parts[0];
      const param = parts.slice(1).join("-");
      const cacheKey = `${brId}/${param}/${timeRangeHours}`;
      const data = historyCache[cacheKey];
      if (!data) return;
      chart.data.datasets[0].data = data.map(r => ({ x: r.timestamp, y: r.value }));
      chart.update("none");
    });
  }

  function destroyCharts() {
    Object.values(charts).forEach(c => c.destroy());
    charts = {};
  }

  // ── Modal: expanded chart ──────────────────────────────────
  async function openParamModal(brId, param) {
    const cfg = CONFIG.parameters[param];
    const br = CONFIG.bioreactors.find(b => b.id === brId);
    document.getElementById("modal-title").textContent = `${br.name} — ${cfg.label}`;
    document.getElementById("modal-body").innerHTML =
      `<div class="chart-full"><canvas id="modal-chart"></canvas></div>`;
    document.getElementById("modal-overlay").classList.remove("hidden");
    await loadAndRenderChart(brId, param, "modal-chart", 400);
  }

  function closeModal() {
    document.getElementById("modal-overlay").classList.add("hidden");
    if (charts["modal-chart"]) { charts["modal-chart"].destroy(); delete charts["modal-chart"]; }
    document.getElementById("modal-body").innerHTML = "";
  }

  // ── OPC UA Tag Browser ─────────────────────────────────────
  async function showTagBrowser() {
    document.getElementById("modal-title").textContent = "OPC UA Tag Browser";
    document.getElementById("modal-body").innerHTML = `
      <p style="color:var(--muted);margin-bottom:10px;font-size:13px">
        Connect directly to a DASware 6 OPC UA server to discover the exact node IDs.<br>
        Scivario (192.168.137.8): <code style="color:var(--accent)">opc.tcp://CTPCOG910098:51530/UA/connectServer</code><br>
        DASbox (192.168.137.7): <code style="color:var(--accent)">opc.tcp://CTPCMO508723:51530/UA/connectServer</code>
      </p>
      <div style="display:grid;grid-template-columns:1fr auto;gap:8px;margin-bottom:8px">
        <input id="tb-url"  class="select" value="opc.tcp://CTPCOG910098:51530/UA/connectServer" style="padding:7px">
        <input id="tb-root" class="select" placeholder="Root NodeId (optional, e.g. ns=2;s=Plant1/Unit1)" style="padding:7px;grid-column:1/3">
        <input id="tb-user" class="select" placeholder="Username (if required)" style="padding:7px">
        <input id="tb-pass" class="select" type="password" placeholder="Password (if required)" style="padding:7px">
      </div>
      <div style="display:flex;gap:8px;margin-bottom:10px">
        <button class="btn btn-sm" onclick="App._tbBrowse()">Browse Nodes</button>
        <button class="btn btn-sm btn-secondary" onclick="App._tbRead()">Read Single Node</button>
      </div>
      <div id="tb-result" style="font-family:monospace;font-size:12px;color:var(--text);white-space:pre-wrap;max-height:380px;overflow-y:auto;background:var(--bg);padding:10px;border-radius:6px;border:1px solid var(--border)">Results will appear here…

Quick guide:
  1. Click "Browse Nodes" to explore the full address space (starts from ObjectsFolder).
  2. Copy a NodeId from the results, paste into "Root NodeId" and browse again to drill down.
  3. Copy variable NodeIds into config.json under the matching bioreactor's "tags" section.
  4. Use "Read Single Node" to test a specific node ID before committing it.</div>`;
    document.getElementById("modal-overlay").classList.remove("hidden");
  }

  async function _tbBrowse() {
    const url  = document.getElementById("tb-url").value.trim();
    const root = document.getElementById("tb-root").value.trim();
    const user = document.getElementById("tb-user").value.trim();
    const pass = document.getElementById("tb-pass").value;
    if (!url) { document.getElementById("tb-result").textContent = "Enter the OPC UA endpoint URL."; return; }
    document.getElementById("tb-result").textContent = "Connecting to OPC UA server… (may take 5-10 seconds)";
    try {
      let apiUrl = `/api/opc/browse-ua?url=${encodeURIComponent(url)}&root_node=${encodeURIComponent(root)}`;
      if (user) apiUrl += `&username=${encodeURIComponent(user)}&password=${encodeURIComponent(pass)}`;
      const resp = await fetch(apiUrl);
      const data = await resp.json();
      if (!resp.ok) { document.getElementById("tb-result").textContent = "Error: " + (data.detail || resp.statusText); return; }
      if (!data.nodes.length) {
        document.getElementById("tb-result").textContent = "No variable nodes found under " + (root || "ObjectsFolder") + ".\nTry a different root NodeId.";
        return;
      }
      let text = `Found ${data.count} nodes under "${data.root_node}":\n\n`;
      data.nodes.forEach(n => { text += `  ${n.node_id.padEnd(60)}  ${n.name}\n`; });
      if (data.count >= 300) text += "\n(truncated at 300 — use a root NodeId to narrow down)";
      text += "\n\nCopy node_id values into config.json tags section.";
      document.getElementById("tb-result").textContent = text;
    } catch (e) {
      document.getElementById("tb-result").textContent = "Request failed: " + e.message;
    }
  }

  async function _tbRead() {
    const url    = document.getElementById("tb-url").value.trim();
    const nodeId = document.getElementById("tb-root").value.trim();
    const user   = document.getElementById("tb-user").value.trim();
    const pass   = document.getElementById("tb-pass").value;
    if (!url || !nodeId) {
      document.getElementById("tb-result").textContent = "Enter the OPC UA URL and a NodeId to read.";
      return;
    }
    document.getElementById("tb-result").textContent = "Reading node…";
    try {
      let readUrl = `/api/opc/read-ua?url=${encodeURIComponent(url)}&node_id=${encodeURIComponent(nodeId)}`;
      if (user) readUrl += `&username=${encodeURIComponent(user)}&password=${encodeURIComponent(pass)}`;
      const resp = await fetch(readUrl);
      const data = await resp.json();
      if (!resp.ok) { document.getElementById("tb-result").textContent = "Error: " + (data.detail || resp.statusText); return; }
      document.getElementById("tb-result").textContent =
        `Node: ${data.node_id}\nName: ${data.display_name}\nValue: ${data.value}`;
    } catch (e) {
      document.getElementById("tb-result").textContent = "Request failed: " + e.message;
    }
  }

  // ── Connection log ─────────────────────────────────────────
  async function showConnectionLog() {
    const rows = await fetch("/api/connection-log").then(r => r.json());
    document.getElementById("modal-title").textContent = "OPC Connection Log";
    let html = `<table class="log-table"><thead><tr><th>Time</th><th>Server</th><th>Status</th><th>Message</th></tr></thead><tbody>`;
    if (rows.length === 0) html += `<tr><td colspan="4" style="color:var(--muted)">No log entries</td></tr>`;
    rows.forEach(r => {
      const color = r.status === "Connected" ? "var(--good)" : r.status === "Error" ? "var(--bad)" : "var(--muted)";
      html += `<tr><td>${fmtTime(r.timestamp)}</td><td>${r.server}</td><td style="color:${color}">${r.status}</td><td>${r.message || ""}</td></tr>`;
    });
    html += `</tbody></table>`;
    document.getElementById("modal-body").innerHTML = html;
    document.getElementById("modal-overlay").classList.remove("hidden");
  }

  // ── Helpers ────────────────────────────────────────────────
  function fmtVal(v) {
    if (v === null || v === undefined) return "–";
    return Number(v).toLocaleString(undefined, { maximumFractionDigits: 2, minimumFractionDigits: 0 });
  }

  function fmtTime(ts) {
    if (!ts) return "–";
    const hasZone = ts.endsWith("Z") || /[+-]\d{2}:\d{2}$/.test(ts);
    const d = new Date(hasZone ? ts : ts + "Z");
    return isNaN(d.getTime()) ? ts : d.toLocaleString();
  }

  function qualityColor(q) {
    if (!q) return "var(--muted)";
    if (q === "Good") return "var(--good)";
    if (q === "Simulated") return "var(--accent)";
    return "var(--bad)";
  }

  // ── Analytical data view ──────────────────────────────────

  async function showAnalytical() {
    activeView = "analytical";
    activeBR = null;
    buildNav();
    renderView();
  }

  async function renderAnalytical() {
    content.innerHTML = `<div id="anal-content"></div>`;
    await _renderBiohtContent();
  }

  async function showNova() {
    activeView = "nova";
    activeBR = null;
    buildNav();
    renderView();
  }

  async function renderNova() {
    content.innerHTML = `<div id="anal-content"></div>`;
    await _renderNovaContent();
  }

  // ── BioHT tab ─────────────────────────────────────────────

  function _biohtRefreshDebounced() {
    clearTimeout(_biohtDebounceTimer);
    _biohtDebounceTimer = setTimeout(_biohtRefresh, 600);
  }

  function _novaRefreshDebounced() {
    clearTimeout(_novaDebounceTimer);
    _novaDebounceTimer = setTimeout(_novaRefresh, 600);
  }

  async function _renderBiohtContent() {
    const analContent = document.getElementById("anal-content");
    if (!analContent) return;

    const today      = new Date();
    const ninetyAgo  = new Date(today - 90 * 24 * 3600 * 1000);
    const todayStr   = today.toISOString().slice(0, 10);
    const ninetyStr  = ninetyAgo.toISOString().slice(0, 10);

    analContent.innerHTML = `
      <div class="analytical-controls" style="flex-wrap:wrap;gap:8px">
        <span style="color:var(--muted);font-size:12px;align-self:center">From:</span>
        <input type="date" id="bioht-from" class="select" value="${ninetyStr}" style="width:140px" onchange="App._biohtRefreshDebounced()">
        <span style="color:var(--muted);font-size:12px;align-self:center">To:</span>
        <input type="date" id="bioht-to"   class="select" value="${todayStr}"  style="width:140px" onchange="App._biohtRefreshDebounced()">
        <select id="bioht-analyte" class="select" style="width:180px" onchange="App._biohtDrawChart()">
          <option value="">Select analyte to chart</option>
        </select>
        <label style="display:flex;align-items:center;gap:5px;color:var(--muted);font-size:12px;cursor:pointer;white-space:nowrap" title="Group analytes that share the same base name — e.g. LDH2B and LDH2D both appear as LDH2">
          <input type="checkbox" id="bioht-consolidate" onchange="App._biohtConsolidateToggle()" style="cursor:pointer">
          Consolidate variants
        </label>
        <input type="text" id="bioht-sample-filter" class="select" placeholder="Filter by Sample ID…" style="width:180px" oninput="App._biohtFilter()">
        <button class="btn btn-sm" onclick="App._biohtRefresh()">Refresh</button>
        <button class="btn btn-sm btn-secondary" onclick="App._biohtImportTxt()" title="Import CEDEX BIO HT archive .txt files">Import TXT</button>
        <button class="btn btn-sm btn-secondary" onclick="App._biohtExportXlsx()" title="Download an xlsx file matching the Cedex_Data template">Export xlsx</button>
        <input type="file" id="bioht-file-input" accept=".txt" multiple style="display:none" onchange="App._biohtHandleFileImport(event)">
      </div>
      <div style="display:flex;gap:16px;align-items:center;margin:4px 0 2px">
        <div id="bioht-range-info" style="color:var(--muted);font-size:12px"></div>
        <div id="bioht-mast-status" style="font-size:11px;margin-left:auto"></div>
      </div>
      <div id="bioht-load-bar"></div>
      <div style="display:flex;align-items:center;gap:10px;margin-bottom:14px">
        <span style="color:var(--muted);font-size:12px;white-space:nowrap">View sample:</span>
        <select id="bioht-sample-sel" class="select" style="flex:1;max-width:480px" onchange="App._biohtSelectSample()">
          <option value="">Latest sample</option>
        </select>
      </div>
      <div id="bioht-latest-card" style="margin-bottom:16px"></div>
      <div id="bioht-chart-card" class="chart-card" style="display:none;margin-bottom:16px">
        <div class="chart-card-header">
          <span class="chart-card-title" id="bioht-chart-title">BioHT History</span>
        </div>
        <div style="position:relative;height:260px"><canvas id="bioht-chart"></canvas></div>
      </div>
      <div id="bioht-table-wrap"></div>`;
    await _biohtRefresh();
  }

  async function _biohtRefresh() {
    const fromEl = document.getElementById("bioht-from");
    const toEl   = document.getElementById("bioht-to");
    const since  = fromEl?.value ? fromEl.value + "T00:00:00" : null;
    const until  = toEl?.value   ? toEl.value   + "T23:59:59" : null;

    const params = new URLSearchParams();
    if (since) params.set("since", since);
    if (until) params.set("until", until);

    const rangeInfo = document.getElementById("bioht-range-info");
    const loadBar   = document.getElementById("bioht-load-bar");

    const loadStart   = Date.now();
    const lastLoadMs  = +(localStorage.getItem("bioht_last_load_ms") || 0);
    let   countResult = null;

    const updateLoadingUI = () => {
      const elapsed = (Date.now() - loadStart) / 1000;
      const elapsedStr = elapsed.toFixed(1) + "s elapsed";
      let msg = `<span class="spinner-inline"></span> `;
      let barHtml;

      if (countResult) {
        const total = countResult.total || 0;
        msg += `Loading ${total.toLocaleString()} entries`;
        if (countResult.mast > 0)
          msg += ` <span style="color:var(--muted)">(${countResult.local.toLocaleString()} local + ${countResult.mast.toLocaleString()} MAST)</span>`;
        msg += ` — ${elapsedStr}`;
        if (lastLoadMs > 0) {
          const remSec = Math.max(0, Math.round((lastLoadMs - (Date.now() - loadStart)) / 1000));
          if (remSec > 1) msg += ` <span style="color:var(--accent)">~${remSec}s remaining</span>`;
        }
        // Time-based fill: use last known duration as 100%; cap at 95% so bar doesn't finish early
        const pct = lastLoadMs > 0
          ? Math.min(95, (elapsed / (lastLoadMs / 1000)) * 100)
          : null;
        barHtml = pct !== null
          ? `<div class="progress-track"><div class="progress-fill" style="width:${pct}%"></div></div>`
          : `<div class="progress-track"><div class="progress-fill indeterminate"></div></div>`;
      } else {
        msg += `Querying MAST SQL Server — ${elapsedStr}`;
        if (lastLoadMs > 0) {
          const remSec = Math.max(0, Math.round((lastLoadMs - (Date.now() - loadStart)) / 1000));
          if (remSec > 1) msg += ` <span style="color:var(--accent)">~${remSec}s remaining</span>`;
        }
        const pct = lastLoadMs > 0
          ? Math.min(95, (elapsed / (lastLoadMs / 1000)) * 100)
          : null;
        barHtml = pct !== null
          ? `<div class="progress-track"><div class="progress-fill" style="width:${pct}%"></div></div>`
          : `<div class="progress-track"><div class="progress-fill indeterminate"></div></div>`;
      }

      if (rangeInfo) { rangeInfo.innerHTML = msg; rangeInfo.style.color = "var(--muted)"; }
      if (loadBar)   loadBar.innerHTML = barHtml;
    };

    updateLoadingUI();
    const ticker = setInterval(updateLoadingUI, 500);

    // Fetch count (fast) and full data (slow) in parallel
    fetch(`/api/bioht/count?${params}`)
      .then(r => r.ok ? r.json() : null)
      .then(c => { if (c) countResult = c; })
      .catch(() => {});

    let latestRows = [], histData = [], samples = [], mastOnline = false;
    try {
      const [histRes, sampRes] = await Promise.all([
        fetch(`/api/bioht/all?${params}`),
        fetch("/api/bioht/all-samples"),
      ]);
      if (histRes.ok) {
        const d = await histRes.json();
        histData   = d.data || [];
        mastOnline = (d.mast_count || 0) > 0;
      }
      if (sampRes.ok) samples = (await sampRes.json()).data || [];
    } catch (_) {}

    clearInterval(ticker);
    localStorage.setItem("bioht_last_load_ms", Date.now() - loadStart);
    if (loadBar) loadBar.innerHTML = "";

    if (histData.length > 0) {
      const newestTime = histData.reduce((a, b) => (a.sample_time > b.sample_time ? a : b)).sample_time;
      const newestId   = histData.find(r => r.sample_time === newestTime)?.sample_id || "";
      latestRows = histData.filter(r => r.sample_id === newestId);
    }

    _biohtHistFull = histData;
    _biohtSampFull = samples;

    const statusEl = document.getElementById("bioht-mast-status");
    if (statusEl) {
      statusEl.textContent = mastOnline ? "MAST: online" : "MAST: offline (showing local TXT data only)";
      statusEl.style.color = mastOnline ? "var(--good)" : "var(--warn)";
    }

    _biohtApplyFilters(latestRows);
  }

  function _biohtFilter() { _biohtApplyFilters(null); }

  function _biohtApplyFilters(latestRows) {
    const raw = (document.getElementById("bioht-sample-filter")?.value || "").trim().toLowerCase();

    const histData = raw
      ? _biohtHistFull.filter(r => r.sample_id && r.sample_id.toLowerCase().includes(raw))
      : _biohtHistFull;

    const samples = raw
      ? _biohtSampFull.filter(s => s.sample_id && s.sample_id.toLowerCase().includes(raw))
      : _biohtSampFull;

    _biohtRowsCache = histData;

    const rangeInfo = document.getElementById("bioht-range-info");
    if (rangeInfo) {
      const n = new Set(histData.map(r => r.sample_id)).size;
      if (n === 0) {
        rangeInfo.textContent = raw ? `No samples match "${raw}" in the selected range.` : "No samples in selected date range.";
        rangeInfo.style.color = "var(--warn)";
      } else {
        rangeInfo.textContent = `${n} sample${n !== 1 ? "s" : ""} — ${histData.length} measurements` +
          (n < 10 ? " (widen range for more trend data)" : "");
        rangeInfo.style.color = n < 10 ? "var(--warn)" : "var(--muted)";
      }
    }

    _biohtPopulateSampleSelector(samples);

    const selSample = document.getElementById("bioht-sample-sel")?.value;
    if (selSample) {
      const cached = histData.filter(r => r.sample_id === selSample);
      if (cached.length > 0) {
        _renderBiohtSampleCard(cached);
      } else {
        fetch(`/api/bioht/sample?sample_id=${encodeURIComponent(selSample)}`)
          .then(r => r.ok ? r.json() : { data: [] })
          .then(d => _renderBiohtSampleCard(d.data || []))
          .catch(() => _renderBiohtSampleCard([]));
      }
    } else if (latestRows !== null) {
      if (raw && histData.length > 0) {
        const latestId = histData.reduce((a, b) => a.sample_time > b.sample_time ? a : b).sample_id;
        _renderBiohtSampleCard(histData.filter(r => r.sample_id === latestId));
      } else {
        _renderBiohtSampleCard(latestRows);
      }
    }

    _biohtPopulateAnalyteSelector(histData);
    _renderBiohtTable(histData);
    _biohtDrawChart();
  }

  function _biohtPopulateSampleSelector(samples) {
    const sel = document.getElementById("bioht-sample-sel");
    if (!sel) return;
    const cur = sel.value;
    sel.innerHTML = '<option value="">Latest sample</option>';
    samples.forEach(s => {
      const o = document.createElement("option");
      o.value = s.sample_id;
      const srcTag = s.source === "mast" ? " [MAST]" : s.source === "both" ? " [MAST+TXT]" : " [TXT]";
      o.textContent = `${s.sample_id}  —  ${fmtTime(s.latest_time)}${srcTag}`;
      if (s.sample_id === cur) o.selected = true;
      sel.appendChild(o);
    });
  }

  async function _biohtSelectSample() {
    const sel = document.getElementById("bioht-sample-sel");
    const sampleId = sel?.value;
    if (!sampleId) {
      const r = await fetch("/api/bioht/latest").catch(() => null);
      _renderBiohtSampleCard(r && r.ok ? (await r.json()).data || [] : []);
      return;
    }
    const cached = _biohtRowsCache.filter(r => r.sample_id === sampleId);
    if (cached.length > 0) { _renderBiohtSampleCard(cached); return; }
    const r = await fetch(`/api/bioht/sample?sample_id=${encodeURIComponent(sampleId)}`).catch(() => null);
    _renderBiohtSampleCard(r && r.ok ? (await r.json()).data || [] : []);
  }

  function _biohtBaseOf(abbrev) {
    // Strip trailing letter (A-Z) — the dilution-variant suffix.
    // Only strip if it leaves a non-empty string and the preceding char is a digit,
    // so we don't mangle names like "LDH" that have no suffix.
    return /\d[A-Z]$/i.test(abbrev) ? abbrev.slice(0, -1) : abbrev;
  }

  function _biohtPopulateAnalyteSelector(rows) {
    const sel         = document.getElementById("bioht-analyte");
    const consolidate = document.getElementById("bioht-consolidate")?.checked;
    if (!sel) return;

    const curDisplay = sel.value; // previously selected display label
    const abbrevs    = [...new Set(rows.map(r => r.test_abbrev))].sort();

    // Build analyte map: display label -> [original abbrevs]
    const map = {};
    abbrevs.forEach(a => {
      const label = consolidate ? _biohtBaseOf(a) : a;
      if (!map[label]) map[label] = [];
      map[label].push(a);
    });
    _biohtAnalyteMap = map;

    sel.innerHTML = '<option value="">Select analyte to chart</option>';
    Object.keys(map).sort().forEach(label => {
      const o = document.createElement("option");
      o.value = label;
      const variants = map[label];
      o.textContent = variants.length > 1
        ? `${label}  (${variants.join(", ")})`
        : label;
      // Preserve selection: match current display label or any underlying variant
      if (label === curDisplay || variants.includes(curDisplay)) o.selected = true;
      sel.appendChild(o);
    });
  }

  function _biohtConsolidateToggle() {
    _biohtPopulateAnalyteSelector(_biohtRowsCache);
    _biohtDrawChart();
  }

  function _renderBiohtSampleCard(rows) {
    const wrap = document.getElementById("bioht-latest-card");
    if (!wrap) return;
    if (!rows.length) {
      wrap.innerHTML = `<div class="chart-card" style="color:var(--muted);padding:20px;text-align:center">
        No BioHT samples stored yet. Import a CEDEX BIO HT archive .txt file to get started.</div>`;
      return;
    }
    const sampleId   = rows[0].sample_id || "–";
    const sampleTime = fmtTime(rows[0].sample_time);
    const cardSource = rows[0]?.source;
    const srcBadge   = cardSource === "mast"  ? `<span style="background:#4f8ef722;color:#4f8ef7;border-radius:4px;padding:1px 6px;font-size:10px;font-weight:600">MAST</span>` :
                       cardSource === "local" ? `<span style="background:#22c55e22;color:#22c55e;border-radius:4px;padding:1px 6px;font-size:10px;font-weight:600">TXT</span>` : "";
    let html = `<div class="chart-card">
      <div class="chart-card-header">
        <span class="chart-card-title">Sample: ${sampleId} ${srcBadge}</span>
        <span style="color:var(--muted);font-size:12px">${sampleTime}</span>
      </div>
      <div class="kpi-row" style="margin:0">`;
    rows.forEach(r => {
      const val  = r.result_value !== null ? Number(r.result_value).toPrecision(4) : r.result_text || "–";
      const unit = r.unit || "";
      const ok   = !r.status || r.status.trim() === "";
      html += `<div class="kpi-card" style="cursor:default">
        <div class="kpi-label">${r.test_abbrev}</div>
        <div class="kpi-value" style="color:var(--accent);font-size:18px">${val}${unit ? `<span class="kpi-unit" style="font-size:11px"> ${unit}</span>` : ""}</div>
        <div class="kpi-quality" style="color:${ok ? "var(--good)" : "var(--warn)"}">${ok ? "OK" : r.status}</div>
      </div>`;
    });
    html += `</div></div>`;
    wrap.innerHTML = html;
  }

  // Normalize timestamp to ISO 8601 with T separator — required by Luxon/Chart.js time axis.
  // BioHT TXT timestamps are stored as "YYYY-MM-DD HH:MM:SS" (space separator).
  function _toISO(ts) { return ts ? ts.replace(" ", "T") : ts; }

  function _biohtDrawChart() {
    const sel     = document.getElementById("bioht-analyte");
    const display = sel?.value;
    const card    = document.getElementById("bioht-chart-card");
    if (!card) return;
    if (!display) { card.style.display = "none"; return; }

    // Resolve which underlying test_abbrev values to include
    const variants = _biohtAnalyteMap[display] || [display];
    const filtered = _biohtRowsCache.filter(r => variants.includes(r.test_abbrev) && r.result_value !== null);
    if (!filtered.length) { card.style.display = "none"; return; }

    const unit = filtered[0].unit || "";
    document.getElementById("bioht-chart-title").textContent =
      `${display}${unit ? " (" + unit + ")" : ""} — History`;
    card.style.display = "";

    if (charts["bioht-chart"]) { charts["bioht-chart"].destroy(); delete charts["bioht-chart"]; }
    const canvas = document.getElementById("bioht-chart");
    if (!canvas) return;

    const palette = ["#22c55e", "#4f8ef7", "#f59e0b", "#ef4444", "#a855f7"];
    const consolidating = variants.length > 1;

    // When consolidating, one dataset per variant so they're distinguishable
    const datasets = consolidating
      ? variants.map((v, i) => {
          const pts = filtered.filter(r => r.test_abbrev === v)
            .sort((a, b) => a.sample_time.localeCompare(b.sample_time))
            .map(r => ({ x: _toISO(r.sample_time), y: r.result_value, sample_id: r.sample_id, abbrev: r.test_abbrev }));
          return { label: v, data: pts,
            borderColor: palette[i % palette.length],
            backgroundColor: palette[i % palette.length] + "22",
            borderWidth: 2, pointRadius: 6, pointHoverRadius: 8, fill: false, tension: 0 };
        })
      : [{
          label: display,
          data: [...filtered]
            .sort((a, b) => a.sample_time.localeCompare(b.sample_time))
            .map(r => ({ x: _toISO(r.sample_time), y: r.result_value, sample_id: r.sample_id, abbrev: r.test_abbrev })),
          borderColor: palette[0], backgroundColor: palette[0] + "22",
          borderWidth: 2, pointRadius: 6, pointHoverRadius: 8, fill: false, tension: 0,
        }];

    charts["bioht-chart"] = new Chart(canvas, {
      type: "line",
      data: { datasets },
      options: {
        responsive: true, maintainAspectRatio: false, animation: false,
        interaction: { mode: "index", intersect: false },
        plugins: {
          legend: { display: consolidating, labels: { color: "#e2e8f0", font: { size: 12 } } },
          tooltip: { callbacks: {
            label: ctx => ` ${ctx.dataset.label}: ${Number(ctx.parsed.y).toPrecision(4)} ${unit}`,
            afterLabel: ctx => ctx.raw.sample_id ? ` Sample: ${ctx.raw.sample_id}` : "",
          }}
        },
        scales: {
          x: { type: "time", time: { tooltipFormat: "MMM d, HH:mm", displayFormats: { hour: "MMM d HH:mm", day: "MMM d" } }, ticks: { color: "#6b7280", maxTicksLimit: 10 }, grid: { color: "#2a2d3e" } },
          y: { ticks: { color: "#6b7280" }, grid: { color: "#2a2d3e" }, title: { display: !!unit, text: unit, color: "#6b7280" } }
        }
      }
    });
  }

  function _renderBiohtTable(rows) {
    const wrap = document.getElementById("bioht-table-wrap");
    if (!wrap) return;
    if (!rows.length) {
      wrap.innerHTML = `<div style="color:var(--muted);padding:20px;text-align:center;background:var(--surface);border-radius:var(--radius)">
        No BioHT results in the selected range.</div>`;
      return;
    }
    const bySample = {};
    rows.forEach(r => {
      if (!bySample[r.sample_id]) bySample[r.sample_id] = { latest_time: r.sample_time, rows: [] };
      if (r.sample_time > bySample[r.sample_id].latest_time) bySample[r.sample_id].latest_time = r.sample_time;
      bySample[r.sample_id].rows.push(r);
    });
    const sampleKeys = Object.keys(bySample).sort((a, b) => bySample[b].latest_time.localeCompare(bySample[a].latest_time));

    let html = `<div class="chart-card">
      <div class="chart-card-header"><span class="chart-card-title">${sampleKeys.length} Sample${sampleKeys.length !== 1 ? "s" : ""}</span></div>
      <table class="log-table"><thead><tr>
        <th>Sample ID</th><th>Source</th><th>Time</th><th>Analyte</th><th>Result</th><th>Unit</th><th>Status</th>
      </tr></thead><tbody>`;

    sampleKeys.forEach(sid => {
      const m = bySample[sid];
      const sorted = [...m.rows].sort((a, b) => a.sample_time.localeCompare(b.sample_time));
      const src = m.rows[0]?.source;
      const srcCell = src === "mast"  ? `<span style="color:#4f8ef7;font-size:11px;font-weight:600">MAST</span>` :
                      src === "local" ? `<span style="color:#22c55e;font-size:11px;font-weight:600">TXT</span>`  : "–";
      sorted.forEach((r, i) => {
        const val = r.result_value !== null ? Number(r.result_value).toPrecision(4) : r.result_text || "–";
        const ok  = !r.status || r.status.trim() === "";
        html += `<tr>
          ${i === 0 ? `<td rowspan="${sorted.length}"><b>${sid}</b></td><td rowspan="${sorted.length}">${srcCell}</td>` : ""}
          <td style="color:var(--muted)">${fmtTime(r.sample_time)}</td>
          <td>${r.test_abbrev}</td>
          <td style="font-variant-numeric:tabular-nums;font-weight:700">${val}</td>
          <td>${r.unit || "–"}</td>
          <td><span style="color:${ok ? "var(--good)" : "var(--warn)"}">${ok ? "✓" : r.status}</span></td>
        </tr>`;
      });
    });
    html += `</tbody></table></div>`;
    wrap.innerHTML = html;
  }

  function _biohtImportTxt() {
    const input = document.getElementById("bioht-file-input");
    if (input) input.click();
  }

  async function _biohtHandleFileImport(event) {
    const files = Array.from(event.target.files || []);
    event.target.value = "";
    if (!files.length) return;
    const info = document.getElementById("bioht-range-info");
    if (info) { info.textContent = `Importing ${files.length} file${files.length !== 1 ? "s" : ""}…`; info.style.color = "var(--accent)"; }
    let totalRows = 0, totalInserted = 0, totalSkipped = 0, errors = 0;
    for (const file of files) {
      const fd = new FormData(); fd.append("file", file);
      try {
        const res = await fetch("/api/bioht/import-txt", { method: "POST", body: fd });
        if (res.ok) {
          const d = await res.json();
          totalRows     += d.rows_parsed || 0;
          totalInserted += d.inserted    || 0;
          totalSkipped  += d.skipped     || 0;
        } else { errors++; }
      } catch (_) { errors++; }
    }
    if (info) {
      info.textContent = `Import done: ${totalRows} rows, ${totalInserted} new, ${totalSkipped} already existed` +
        (errors ? ` (${errors} error${errors !== 1 ? "s" : ""})` : "");
      info.style.color = errors ? "var(--warn)" : "var(--good)";
    }
    await _biohtRefresh();
  }

  // ── Nova Flex2 tab ────────────────────────────────────────

  async function _renderNovaContent() {
    const analContent = document.getElementById("anal-content");
    if (!analContent) return;

    const today = new Date();
    const thirtyAgo = new Date(today - 30 * 24 * 3600 * 1000);
    const todayStr    = today.toISOString().slice(0, 10);
    const thirtyStr   = thirtyAgo.toISOString().slice(0, 10);

    analContent.innerHTML = `
      <div class="analytical-controls" style="flex-wrap:wrap;gap:8px">
        <span style="color:var(--muted);font-size:12px;align-self:center">From:</span>
        <input type="date" id="nova-from" class="select" value="${thirtyStr}" style="width:140px" onchange="App._novaRefreshDebounced()">
        <span style="color:var(--muted);font-size:12px;align-self:center">To:</span>
        <input type="date" id="nova-to"   class="select" value="${todayStr}"  style="width:140px" onchange="App._novaRefreshDebounced()">
        <select id="nova-analyte" class="select" style="width:220px" onchange="App._novaDrawChart()">
          <option value="">Select analyte to chart</option>
        </select>
        <input type="text" id="nova-sample-filter" class="select" placeholder="Filter by Sample ID…" style="width:180px" oninput="App._novaFilter()">
        <button class="btn btn-sm" onclick="App._novaRefresh()">Refresh</button>
        <button class="btn btn-sm btn-secondary" onclick="App._novaImportCSV()" title="Import one or more Nova BioProfile CSV exports">Import CSV</button>
        <button class="btn btn-sm btn-secondary" onclick="App._novaExportXlsx()" title="Download an xlsx file matching the Flex_2_Data template">Export xlsx</button>
        <input type="file" id="nova-file-input" accept=".csv" multiple style="display:none" onchange="App._novaHandleFileImport(event)">
      </div>
      <div id="nova-range-info" style="color:var(--muted);font-size:12px;margin:6px 0 4px"></div>
      <div id="nova-load-bar"></div>
      <div style="display:flex;align-items:center;gap:10px;margin-bottom:14px">
        <span style="color:var(--muted);font-size:12px;white-space:nowrap">View sample:</span>
        <select id="nova-sample-sel" class="select" style="flex:1;max-width:480px" onchange="App._novaSelectSample()">
          <option value="">Latest measurement</option>
        </select>
      </div>
      <div id="nova-latest-card" style="margin-bottom:16px"></div>
      <div id="nova-chart-card" class="chart-card" style="display:none;margin-bottom:16px">
        <div class="chart-card-header">
          <span class="chart-card-title" id="nova-chart-title">Nova History</span>
        </div>
        <div style="position:relative;height:260px"><canvas id="nova-chart"></canvas></div>
      </div>
      <div id="nova-table-wrap"></div>`;
    await _novaRefresh();
  }

  // Full fetch (called on date change or Refresh button).
  // Stores unfiltered data, then calls _novaApplyFilters to do the rest.
  async function _novaRefresh() {
    const fromEl = document.getElementById("nova-from");
    const toEl   = document.getElementById("nova-to");
    const since  = fromEl?.value ? fromEl.value + "T00:00:00" : null;
    const until  = toEl?.value   ? toEl.value   + "T23:59:59" : null;

    const params = new URLSearchParams();
    if (since) params.set("since", since);
    if (until) params.set("until", until);

    const rangeInfo = document.getElementById("nova-range-info");
    const loadBar   = document.getElementById("nova-load-bar");

    const loadStart  = Date.now();
    const lastLoadMs = +(localStorage.getItem("nova_last_load_ms") || 0);
    let   totalCount = null;

    const updateLoadingUI = () => {
      const elapsed = (Date.now() - loadStart) / 1000;
      const elapsedStr = elapsed.toFixed(1) + "s elapsed";
      let msg = `<span class="spinner-inline"></span> `;
      const pct = lastLoadMs > 0 ? Math.min(95, (elapsed / (lastLoadMs / 1000)) * 100) : null;
      const barHtml = pct !== null
        ? `<div class="progress-track"><div class="progress-fill" style="width:${pct}%"></div></div>`
        : `<div class="progress-track"><div class="progress-fill indeterminate"></div></div>`;

      if (totalCount !== null) {
        msg += `Loading ${totalCount.toLocaleString()} entries — ${elapsedStr}`;
      } else {
        msg += `Loading Nova results — ${elapsedStr}`;
      }
      if (lastLoadMs > 0) {
        const remSec = Math.max(0, Math.round((lastLoadMs - (Date.now() - loadStart)) / 1000));
        if (remSec > 1) msg += ` <span style="color:var(--accent)">~${remSec}s remaining</span>`;
      }

      if (rangeInfo) { rangeInfo.innerHTML = msg; rangeInfo.style.color = "var(--muted)"; }
      if (loadBar)   loadBar.innerHTML = barHtml;
    };

    updateLoadingUI();
    const ticker = setInterval(updateLoadingUI, 500);

    fetch(`/api/nova/count?${params}`)
      .then(r => r.ok ? r.json() : null)
      .then(c => { if (c) totalCount = c.count || 0; })
      .catch(() => {});

    let latestRows = [], histData = [], samples = [];
    try {
      const [latRes, histRes, sampRes] = await Promise.all([
        fetch("/api/nova/latest"),
        fetch(`/api/nova/results?${params}`),
        fetch("/api/nova/samples"),
      ]);
      if (latRes.ok)  latestRows = (await latRes.json()).data || [];
      if (histRes.ok) histData   = (await histRes.json()).data || [];
      if (sampRes.ok) samples    = (await sampRes.json()).data || [];
    } catch (_) {}

    clearInterval(ticker);
    localStorage.setItem("nova_last_load_ms", Date.now() - loadStart);
    if (loadBar) loadBar.innerHTML = "";

    _novaHistFull = histData;
    _novaSampFull = samples;
    _novaApplyFilters(latestRows);
  }

  // Text-filter only — no network call, just re-filters cached data.
  function _novaFilter() {
    _novaApplyFilters(null);
  }

  // Apply text filter to cached data and re-render everything.
  // latestRows: pass the /api/nova/latest result on a full refresh; null on text-only change.
  function _novaApplyFilters(latestRows) {
    const raw = (document.getElementById("nova-sample-filter")?.value || "").trim().toLowerCase();

    const histData = raw
      ? _novaHistFull.filter(r => r.sample_id && r.sample_id.toLowerCase().includes(raw))
      : _novaHistFull;

    const samples = raw
      ? _novaSampFull.filter(s => s.sample_id && s.sample_id.toLowerCase().includes(raw))
      : _novaSampFull;

    _novaRowsCache = histData;

    // Range / count info
    const rangeInfo = document.getElementById("nova-range-info");
    if (rangeInfo) {
      const n = new Set(histData.map(r => r.sample_time)).size;
      if (n === 0) {
        rangeInfo.textContent = raw
          ? `No measurements match "${raw}" in the selected date range.`
          : "No measurements in selected date range.";
        rangeInfo.style.color = "var(--warn)";
      } else {
        rangeInfo.textContent =
          `${n} measurement${n !== 1 ? "s" : ""} — ${histData.length} data points` +
          (n < 10 ? " (widen range for ≥10 measurements)" : "");
        rangeInfo.style.color = n < 10 ? "var(--warn)" : "var(--muted)";
      }
    }

    // Sample picker (filtered)
    _novaPopulateSampleSelector(samples);

    // Latest / selected-sample card
    const selSample = document.getElementById("nova-sample-sel")?.value;
    if (selSample) {
      const cached = histData.filter(r => r.sample_time === selSample);
      if (cached.length > 0) {
        _renderNovaLatestCard(cached);
      } else {
        // Selected sample is outside current filter — try API
        fetch(`/api/nova/sample?sample_time=${encodeURIComponent(selSample)}`)
          .then(r => r.ok ? r.json() : { data: [] })
          .then(d => _renderNovaLatestCard(d.data || []))
          .catch(() => _renderNovaLatestCard([]));
      }
    } else if (latestRows !== null) {
      // Full refresh: show the overall latest, or latest in filtered set if filter active
      if (raw && histData.length > 0) {
        const latestTs = histData.reduce((a, b) => a.sample_time > b.sample_time ? a : b).sample_time;
        _renderNovaLatestCard(histData.filter(r => r.sample_time === latestTs));
      } else {
        _renderNovaLatestCard(latestRows);
      }
    }
    // (if latestRows===null and no selection, leave the card as-is)

    _novaPopulateAnalyteSelector(histData);
    _renderNovaTable(histData);
    _novaDrawChart();
  }

  function _novaPopulateSampleSelector(samples) {
    const sel = document.getElementById("nova-sample-sel");
    if (!sel) return;
    const cur = sel.value;
    sel.innerHTML = '<option value="">Latest measurement</option>';
    samples.forEach(s => {
      const o = document.createElement("option");
      o.value = s.sample_time;
      o.textContent = fmtTime(s.sample_time) + (s.sample_id ? "  —  " + s.sample_id : "");
      if (s.sample_time === cur) o.selected = true;
      sel.appendChild(o);
    });
  }

  function _novaImportCSV() {
    const input = document.getElementById("nova-file-input");
    if (input) input.click();
  }

  async function _novaHandleFileImport(event) {
    const files = Array.from(event.target.files || []);
    event.target.value = "";   // reset so same file can be re-selected
    if (!files.length) return;

    const info = document.getElementById("nova-range-info");
    if (info) {
      info.textContent = `Importing ${files.length} file${files.length !== 1 ? "s" : ""}…`;
      info.style.color = "var(--accent)";
    }

    let totalSamples = 0, totalInserted = 0, totalSkipped = 0, errors = 0;

    for (const file of files) {
      const fd = new FormData();
      fd.append("file", file);
      try {
        const res = await fetch("/api/nova/import-csv", { method: "POST", body: fd });
        if (res.ok) {
          const d = await res.json();
          totalSamples  += d.samples  || 0;
          totalInserted += d.inserted || 0;
          totalSkipped  += d.skipped  || 0;
        } else {
          errors++;
        }
      } catch (_) { errors++; }
    }

    if (info) {
      const msg = `Import done: ${totalSamples} samples, ${totalInserted} new data points, ${totalSkipped} already existed` +
                  (errors ? ` (${errors} file error${errors !== 1 ? "s" : ""})` : "");
      info.textContent = msg;
      info.style.color = errors ? "var(--warn)" : "var(--good)";
    }

    await _novaRefresh();
  }

  async function _novaSelectSample() {
    const sel = document.getElementById("nova-sample-sel");
    const sampleTime = sel?.value;
    if (!sampleTime) {
      // Show latest
      try {
        const r = await fetch("/api/nova/latest");
        _renderNovaLatestCard(r.ok ? (await r.json()).data || [] : []);
      } catch (_) {}
      return;
    }
    // Try cache first
    const cached = _novaRowsCache.filter(r => r.sample_time === sampleTime);
    if (cached.length > 0) { _renderNovaLatestCard(cached); return; }
    // Fetch from server
    try {
      const r = await fetch(`/api/nova/sample?sample_time=${encodeURIComponent(sampleTime)}`);
      _renderNovaLatestCard(r.ok ? (await r.json()).data || [] : []);
    } catch (_) { _renderNovaLatestCard([]); }
  }

  function _renderNovaLatestCard(analytes) {
    const wrap = document.getElementById("nova-latest-card");
    if (!wrap) return;
    if (analytes.length === 0) {
      wrap.innerHTML = `<div class="chart-card" style="color:var(--muted);padding:20px;text-align:center">
        No Nova measurements stored yet — the poller runs every 2 minutes and stores new results automatically.</div>`;
      return;
    }

    const groupOrder = ["CellDensity","CalculatedResults","Chem","Gas","Osmo"];
    const groupLabels = { CellDensity: "Cell Density", CalculatedResults: "Calculated Results", Chem: "Chemistry", Gas: "Gas", Osmo: "Osmolality" };
    const groups = {};
    analytes.forEach(a => { (groups[a.group_name] = groups[a.group_name] || []).push(a); });
    const orderedGroups = [...groupOrder.filter(g => groups[g]), ...Object.keys(groups).filter(g => !groupOrder.includes(g))];

    const sampleTime = fmtTime(analytes[0].sample_time);
    const sampleId   = analytes[0].sample_id || "–";

    let html = `<div class="chart-card">
      <div class="chart-card-header">
        <span class="chart-card-title">Latest Measurement — ${sampleTime}</span>
        <span style="color:var(--muted);font-size:12px">Sample: ${sampleId}</span>
      </div>`;

    orderedGroups.forEach(grp => {
      html += `<div style="margin-bottom:14px">
        <p style="font-size:10px;font-weight:700;letter-spacing:.1em;color:var(--muted);text-transform:uppercase;margin-bottom:8px">${groupLabels[grp] || grp}</p>
        <div class="kpi-row" style="margin:0">`;
      groups[grp].forEach(a => {
        const val  = a.result_value !== null ? Number(a.result_value).toPrecision(4) : "–";
        const unit = a.unit ? a.unit.replace(/'/g, "").trim() : "";
        const ok   = !a.error_status || a.error_status === "None";
        html += `<div class="kpi-card" style="cursor:default">
          <div class="kpi-label">${a.display_name}</div>
          <div class="kpi-value" style="color:var(--accent);font-size:18px">${val}${unit ? `<span class="kpi-unit" style="font-size:11px"> ${unit}</span>` : ""}</div>
          <div class="kpi-quality" style="color:${ok ? "var(--good)" : "var(--warn)"}">${ok ? "OK" : a.error_status}</div>
        </div>`;
      });
      html += `</div></div>`;
    });

    html += `</div>`;
    wrap.innerHTML = html;
  }

  function _novaPopulateAnalyteSelector(rows) {
    const sel = document.getElementById("nova-analyte");
    if (!sel) return;
    const cur = sel.value;
    const seen = new Set();
    const analytes = [];
    rows.forEach(r => {
      if (!seen.has(r.analyte)) {
        seen.add(r.analyte);
        analytes.push({ analyte: r.analyte, display_name: r.display_name, group_name: r.group_name });
      }
    });
    sel.innerHTML = '<option value="">Select analyte to chart</option>';
    analytes.sort((a, b) => a.group_name.localeCompare(b.group_name) || a.display_name.localeCompare(b.display_name));
    analytes.forEach(a => {
      const o = document.createElement("option");
      o.value = a.analyte;
      o.textContent = `${a.display_name} (${a.group_name})`;
      if (a.analyte === cur) o.selected = true;
      sel.appendChild(o);
    });
  }

  function _novaDrawChart() {
    const sel     = document.getElementById("nova-analyte");
    const analyte = sel?.value;
    const chartCard = document.getElementById("nova-chart-card");
    if (!chartCard) return;

    if (!analyte) { chartCard.style.display = "none"; return; }

    const filtered = _novaRowsCache.filter(r => r.analyte === analyte && r.result_value !== null);
    if (filtered.length === 0) { chartCard.style.display = "none"; return; }

    const displayName = filtered[0].display_name;
    const unit = filtered[0].unit ? filtered[0].unit.replace(/'/g, "").trim() : "";
    document.getElementById("nova-chart-title").textContent = `${displayName}${unit ? " (" + unit + ")" : ""} — History`;
    chartCard.style.display = "";

    if (charts["nova-chart"]) { charts["nova-chart"].destroy(); delete charts["nova-chart"]; }
    const canvas = document.getElementById("nova-chart");
    if (!canvas) return;

    const points = [...filtered]
      .sort((a, b) => a.sample_time.localeCompare(b.sample_time))
      .map(r => ({ x: r.sample_time, y: r.result_value, sample_id: r.sample_id }));

    charts["nova-chart"] = new Chart(canvas, {
      type: "line",
      data: {
        datasets: [{
          label: displayName,
          data: points,
          borderColor: "#4f8ef7",
          backgroundColor: "#4f8ef722",
          borderWidth: 2, pointRadius: 6, pointHoverRadius: 8, fill: false, tension: 0,
        }]
      },
      options: {
        responsive: true, maintainAspectRatio: false, animation: false,
        interaction: { mode: "index", intersect: false },
        plugins: {
          legend: { display: false },
          tooltip: { callbacks: {
            label: ctx => ` ${displayName}: ${Number(ctx.parsed.y).toPrecision(4)} ${unit}`,
            afterLabel: ctx => ctx.raw.sample_id ? ` Sample: ${ctx.raw.sample_id}` : "",
          } }
        },
        scales: {
          x: { type: "time", time: { tooltipFormat: "MMM d, HH:mm", displayFormats: { hour: "MMM d HH:mm", day: "MMM d" } }, ticks: { color: "#6b7280", maxTicksLimit: 10 }, grid: { color: "#2a2d3e" } },
          y: { ticks: { color: "#6b7280" }, grid: { color: "#2a2d3e" }, title: { display: !!unit, text: unit, color: "#6b7280" } }
        }
      }
    });
  }

  function _renderNovaTable(rows) {
    const wrap = document.getElementById("nova-table-wrap");
    if (!wrap) return;
    if (rows.length === 0) {
      wrap.innerHTML = `<div style="color:var(--muted);padding:20px;text-align:center;background:var(--surface);border-radius:var(--radius)">
        No Nova results in the selected time window.</div>`;
      return;
    }

    // Group by sample_time
    const byTime = {};
    rows.forEach(r => {
      if (!byTime[r.sample_time]) byTime[r.sample_time] = { sample_id: r.sample_id, analytes: [] };
      byTime[r.sample_time].analytes.push(r);
    });
    const timeKeys = Object.keys(byTime).sort().reverse();

    let html = `<div class="chart-card">
      <div class="chart-card-header"><span class="chart-card-title">${timeKeys.length} Measurement${timeKeys.length !== 1 ? "s" : ""}</span></div>
      <table class="log-table"><thead><tr>
        <th>Measurement Time</th><th>Sample ID</th><th>Analyte</th><th>Result</th><th>Unit</th><th>Status</th>
      </tr></thead><tbody>`;

    timeKeys.forEach(ts => {
      const m = byTime[ts];
      const tStr = fmtTime(ts);
      const sorted = [...m.analytes].sort((a, b) => a.group_name.localeCompare(b.group_name) || a.display_name.localeCompare(b.display_name));
      sorted.forEach((a, i) => {
        const val  = a.result_value !== null ? Number(a.result_value).toPrecision(4) : "–";
        const unit = a.unit ? a.unit.replace(/'/g, "").trim() : "–";
        const ok   = !a.error_status || a.error_status === "None";
        html += `<tr>
          ${i === 0 ? `<td rowspan="${sorted.length}"><b>${tStr}</b></td><td rowspan="${sorted.length}" style="color:var(--muted)">${m.sample_id || "–"}</td>` : ""}
          <td>${a.display_name}</td>
          <td style="font-variant-numeric:tabular-nums;font-weight:700">${val}</td>
          <td>${unit}</td>
          <td><span style="color:${ok ? "var(--good)" : "var(--warn)"}">${ok ? "✓" : a.error_status}</span></td>
        </tr>`;
      });
    });

    html += `</tbody></table></div>`;
    wrap.innerHTML = html;
  }

  // ── MAST Sampling Status ───────────────────────────────────

  async function showMastStatus() {
    activeView = "mast-status";
    activeBR = null;
    buildNav();
    renderView();
  }

  async function renderMastStatus() {
    content.innerHTML = `
      <div class="detail-header">
        <button class="back-btn" onclick="App._backToOverview()">← Back</button>
        <h2 class="section-title" style="margin:0">MAST Sampling Status</h2>
        <button class="btn btn-sm" onclick="App._mastStatusRefresh()">Refresh</button>
      </div>
      <div id="mast-status-body"><div style="color:var(--muted);padding:40px;text-align:center">Loading…</div></div>`;
    await _mastStatusRefresh();
  }

  async function _mastStatusRefresh() {
    const body = document.getElementById("mast-status-body");
    if (!body) return;

    // ── Progress indicator — live elapsed timers per step ─────
    const pageStart = Date.now();
    const steps = {
      mast:    { label: "MAST SQL Server",    state: "loading", start: Date.now(), elapsed: 0 },
      alarms:  { label: "Alarm History",      state: "loading", start: Date.now(), elapsed: 0 },
      pilots:  { label: "Sample Pilots",      state: "loading", start: Date.now(), elapsed: 0 },
      gilson:  { label: "Gilson SQL",         state: "loading", start: Date.now(), elapsed: 0 },
      run:     { label: "Gilson Run Status",  state: "loading", start: Date.now(), elapsed: 0 },
    };

    const renderProgress = () => {
      const b = document.getElementById("mast-status-body");
      if (!b) return;
      const allSteps = Object.values(steps);
      const total    = allSteps.length;
      const done     = allSteps.filter(s => s.state !== "loading").length;
      const pct      = Math.round((done / total) * 100);
      const now      = Date.now();
      const rows = allSteps.map(s => {
        const icon = s.state === "loading" ? "⟳" : s.state === "ok" ? "✓" : "✗";
        const cls  = s.state === "loading" ? "badge-pending" : s.state === "ok" ? "badge-good" : "badge-bad";
        let note;
        if (s.state === "loading") {
          const sec = ((now - s.start) / 1000).toFixed(1);
          note = `<span style="color:var(--muted);font-size:12px">waiting… ${sec}s</span>`;
        } else if (s.state === "ok") {
          note = `<span style="color:var(--good);font-size:12px">connected in ${(s.elapsed / 1000).toFixed(1)}s</span>`;
        } else {
          note = `<span style="color:var(--bad);font-size:12px">${s.state} (after ${(s.elapsed / 1000).toFixed(1)}s)</span>`;
        }
        return `<div style="display:flex;align-items:center;gap:10px;padding:9px 0;border-bottom:1px solid var(--border)">
          <span class="badge ${cls}" style="width:22px;text-align:center;flex-shrink:0">${icon}</span>
          <span style="flex:1;color:var(--text);font-size:13px">${s.label}</span>
          ${note}
        </div>`;
      }).join("");
      const overallSec = ((now - pageStart) / 1000).toFixed(1);
      b.innerHTML = `
        <div class="chart-card" style="max-width:440px">
          <div class="chart-card-header">
            <span class="chart-card-title">Connecting to databases</span>
            <span style="color:var(--muted);font-size:12px">${done} / ${total} &nbsp;·&nbsp; ${overallSec}s</span>
          </div>
          <div style="padding:0 16px 10px">
            <div style="background:var(--border);border-radius:99px;height:6px;overflow:hidden">
              <div style="height:100%;width:${pct}%;background:var(--accent);border-radius:99px;transition:width 0.4s ease"></div>
            </div>
          </div>
          <div style="padding:0 16px 8px">${rows}</div>
          <div style="padding:0 16px 12px;color:var(--muted);font-size:11px">
            First connection to each SQL Server takes 3–15 s; subsequent refreshes are faster.
          </div>
        </div>`;
    };

    renderProgress();
    // Live tick so elapsed times update every 500 ms while any step is still loading
    const mastTicker = setInterval(() => {
      if (Object.values(steps).every(s => s.state !== "loading")) { clearInterval(mastTicker); return; }
      renderProgress();
    }, 500);

    // ── Fire all five in parallel; each records elapsed time on completion ──
    let status = null, pilots = [], gilson = null, gilsonRun = null, alarmHistory = null;
    let mastOnline = false, gilsonOnline = false;

    const mastFetch = fetch("/api/mast/status")
      .then(async r => {
        steps.mast.elapsed = Date.now() - steps.mast.start;
        if (r.ok) { status = await r.json(); mastOnline = true; steps.mast.state = "ok"; }
        else { steps.mast.state = `HTTP ${r.status}`; }
      })
      .catch(() => { steps.mast.elapsed = Date.now() - steps.mast.start; steps.mast.state = "unreachable"; })
      .finally(renderProgress);

    const alarmsFetch = fetch("/api/mast/alarms?days=7&limit=200")
      .then(async r => {
        steps.alarms.elapsed = Date.now() - steps.alarms.start;
        if (r.ok) { alarmHistory = await r.json(); steps.alarms.state = "ok"; }
        else { steps.alarms.state = `HTTP ${r.status}`; }
      })
      .catch(() => { steps.alarms.elapsed = Date.now() - steps.alarms.start; steps.alarms.state = "unreachable"; })
      .finally(renderProgress);

    const pilotsFetch = fetch("/api/mast/sample-pilots")
      .then(async r => {
        steps.pilots.elapsed = Date.now() - steps.pilots.start;
        if (r.ok) { pilots = (await r.json()).data || []; steps.pilots.state = "ok"; }
        else { steps.pilots.state = `HTTP ${r.status}`; }
      })
      .catch(() => { steps.pilots.elapsed = Date.now() - steps.pilots.start; steps.pilots.state = "unreachable"; })
      .finally(renderProgress);

    const gilsonFetch = fetch("/api/gilson/rs232")
      .then(async r => {
        steps.gilson.elapsed = Date.now() - steps.gilson.start;
        if (r.ok) { gilson = await r.json(); gilsonOnline = gilson.db_reachable !== false; steps.gilson.state = "ok"; }
        else { steps.gilson.state = "offline"; }
      })
      .catch(() => { steps.gilson.elapsed = Date.now() - steps.gilson.start; steps.gilson.state = "offline"; })
      .finally(renderProgress);

    const runFetch = fetch("/api/gilson/run")
      .then(async r => {
        steps.run.elapsed = Date.now() - steps.run.start;
        if (r.ok) { gilsonRun = await r.json(); steps.run.state = "ok"; }
        else { steps.run.state = "offline"; }
      })
      .catch(() => { steps.run.elapsed = Date.now() - steps.run.start; steps.run.state = "offline"; })
      .finally(renderProgress);

    await Promise.all([mastFetch, alarmsFetch, pilotsFetch, gilsonFetch, runFetch]);
    clearInterval(mastTicker);
    if (!document.getElementById("mast-status-body")) return;

    if (!status) {
      body.innerHTML = `<div style="color:var(--bad);padding:40px;text-align:center">MAST SQL offline or unreachable.</div>`;
      return;
    }

    // ── Gilson status badges ──────────────────────────────────
    const rs232 = gilson;
    const rs232BadgeClass = !gilsonOnline ? "badge-bad" : "badge-good";
    const rs232BadgeText  = !gilsonOnline ? "Gilson SQL: Offline" : "Gilson SQL: Connected";

    const runBusy = gilsonRun?.busy;
    const runBadgeClass = !gilsonOnline  ? "badge-pending"
                        : runBusy === true  ? "badge-sim"
                        : runBusy === false ? "badge-good"
                        :                    "badge-pending";
    const runBadgeText = !gilsonOnline  ? "Gilson: –"
                       : runBusy === true  ? "Gilson: Running"
                       : runBusy === false ? "Gilson: Idle"
                       :                    "Gilson: –";

    let html = `<div style="display:flex;align-items:center;gap:10px;margin-bottom:16px;flex-wrap:wrap">
      <span class="badge ${mastOnline ? "badge-good" : "badge-bad"}">${mastOnline ? "MAST: Online" : "MAST: Offline"}</span>
      <span class="badge ${rs232BadgeClass}">${rs232BadgeText}</span>
      <span class="badge ${runBadgeClass}">${runBadgeText}</span>
    </div>`;

    // ── Gilson run status card ────────────────────────────────
    html += `<div class="chart-card" style="margin-bottom:16px">
      <div class="chart-card-header">
        <span class="chart-card-title">Gilson 215 — Current Run</span>
        ${gilsonRun?.run_table ? `<span style="color:var(--muted);font-size:11px">Source: ${gilsonRun.run_table}</span>` : ""}
      </div>`;

    if (!gilsonOnline) {
      html += `<div style="color:var(--muted);padding:12px;font-size:13px">Gilson SQL offline.</div>`;
    } else if (!gilsonRun?.run_table) {
      html += `<div style="color:var(--muted);padding:12px;font-size:13px">${gilsonRun?.note || "No run/queue table found."}</div>`;
    } else {
      // Helper: format duration seconds as "Xh Ym Zs" or "Ym Zs"
      const fmtDur = sec => {
        if (sec == null || isNaN(sec)) return "–";
        const abs = Math.abs(sec);
        const h = Math.floor(abs / 3600);
        const m = Math.floor((abs % 3600) / 60);
        const s = Math.floor(abs % 60);
        const sign = sec < 0 ? "-" : "";
        return h > 0 ? `${sign}${h}h ${m}m` : `${sign}${m}m ${s}s`;
      };

      const methodName    = gilsonRun.method_name || null;
      const startedAt     = gilsonRun.started_at  || null;
      const eta           = gilsonRun.eta          || null;
      const elapsedSec    = gilsonRun.elapsed_sec;
      const remainingSec  = gilsonRun.remaining_sec;
      const queueDepth    = gilsonRun.queue_depth;

      const stateColor = runBusy === true  ? "var(--accent)"
                       : runBusy === false ? "var(--good)"
                       :                    "var(--muted)";
      const stateText  = runBusy === true  ? "Running"
                       : runBusy === false ? "Idle"
                       :                    "Unknown";

      html += `<div class="kpi-row" style="margin:0 0 10px">
        <div class="kpi-card" style="cursor:default">
          <div class="kpi-label">Status</div>
          <div class="kpi-value" style="color:${stateColor};font-size:20px">${stateText}</div>
          <div class="kpi-quality" style="color:var(--muted)">${gilsonRun.note || ""}</div>
        </div>`;

      if (methodName) {
        html += `<div class="kpi-card" style="cursor:default;min-width:160px">
          <div class="kpi-label">Method / Program</div>
          <div class="kpi-value" style="font-size:14px;color:var(--text);word-break:break-word">${methodName}</div>
          <div class="kpi-quality" style="color:var(--muted)">active method</div>
        </div>`;
      }

      if (startedAt) {
        html += `<div class="kpi-card" style="cursor:default">
          <div class="kpi-label">Started</div>
          <div class="kpi-value" style="font-size:14px;color:var(--text)">${fmtTime(startedAt)}</div>
          <div class="kpi-quality" style="color:var(--muted)">Elapsed: ${fmtDur(elapsedSec)}</div>
        </div>`;
      }

      if (remainingSec != null) {
        const overdue = remainingSec < 0;
        html += `<div class="kpi-card" style="cursor:default">
          <div class="kpi-label">${overdue ? "Overdue by" : "Time Remaining"}</div>
          <div class="kpi-value" style="font-size:18px;color:${overdue ? "var(--warn)" : "var(--accent)"}">
            ${fmtDur(remainingSec)}
          </div>
          <div class="kpi-quality" style="color:var(--muted)">${eta ? "ETA: " + fmtTime(eta) : ""}</div>
        </div>`;
      } else if (eta) {
        html += `<div class="kpi-card" style="cursor:default">
          <div class="kpi-label">ETA</div>
          <div class="kpi-value" style="font-size:14px;color:var(--accent)">${fmtTime(eta)}</div>
          <div class="kpi-quality" style="color:var(--muted)">estimated finish</div>
        </div>`;
      }

      if (queueDepth != null && queueDepth > 0) {
        html += `<div class="kpi-card" style="cursor:default">
          <div class="kpi-label">Queue</div>
          <div class="kpi-value" style="font-size:20px;color:var(--muted)">${queueDepth}</div>
          <div class="kpi-quality" style="color:var(--muted)">samples waiting</div>
        </div>`;
      }

      html += `</div>`;

      // Raw active row (collapsed) for debugging until schema is confirmed
      if (gilsonRun.run_row) {
        const rawCols = Object.keys(gilsonRun.run_row);
        html += `<details style="margin-top:4px"><summary style="cursor:pointer;color:var(--muted);font-size:11px;user-select:none">Raw row from ${gilsonRun.run_table}</summary>
          <table class="log-table" style="margin-top:6px"><thead><tr>${rawCols.map(c => `<th>${c}</th>`).join("")}</tr></thead><tbody><tr>
          ${rawCols.map(c => {
            const v = gilsonRun.run_row[c] ?? "–";
            const isTs = /time|date|stamp/i.test(c) && String(v).length > 10;
            return `<td style="font-size:11px">${isTs ? fmtTime(String(v)) : v}</td>`;
          }).join("")}
          </tr></tbody></table></details>`;
      }
    }
    html += `</div>`;

    // ── Sample Pilots card ────────────────────────────────────
    html += `<div class="chart-card" style="margin-bottom:16px">
      <div class="chart-card-header">
        <span class="chart-card-title">Sample Pilots</span>
        <span style="color:var(--muted);font-size:11px">${pilots.filter(p => p.isOnline).length} online · ${pilots.filter(p => p.ExperimentRunning).length} running</span>
      </div>`;

    if (!mastOnline || !pilots.length) {
      html += `<div style="color:var(--muted);padding:16px;font-size:13px">
        ${mastOnline ? "No sample pilot data returned." : "MAST SQL offline."}</div>`;
    } else {
      const onlinePilots  = pilots.filter(p => p.isOnline);
      const offlinePilots = pilots.filter(p => !p.isOnline);

      const renderPilotCard = (p) => {
        const running  = p.ExperimentRunning;
        const lastSamp = p.last_sample_time ? fmtTime(p.last_sample_time) : "never";
        const seq      = p.sequence_name || "–";
        const interval = p.sampling_interval_min;

        let nextLabel = "none scheduled";
        let nextColor = "var(--muted)";
        if (p.next_sample_time) {
          const msUntil = new Date(p.next_sample_time) - Date.now();
          if (msUntil < 0) {
            nextLabel = "overdue";
            nextColor = "var(--warn)";
          } else {
            const minUntil = Math.round(msUntil / 60000);
            nextLabel = minUntil < 60
              ? `in ${minUntil} min`
              : `in ${Math.round(minUntil / 60)}h ${minUntil % 60}m`;
            nextColor = minUntil <= 5 ? "var(--accent)" : "var(--good)";
          }
        }

        const stateColor = running   ? "var(--accent)"
                         : p.isOnline ? "var(--good)"
                         : "var(--muted)";
        const stateText  = running   ? "Running"
                         : p.isOnline ? "Online"
                         : "Offline";

        return `<div style="background:var(--bg);border:1px solid var(--border);border-radius:var(--radius);padding:12px 14px;display:flex;flex-direction:column;gap:4px;min-width:150px">
          <div style="display:flex;align-items:center;justify-content:space-between;gap:8px;margin-bottom:2px">
            <span style="font-weight:700;font-size:13px;color:var(--text)">${p.name}</span>
            <span style="font-size:11px;font-weight:600;color:${stateColor}">${stateText}</span>
          </div>
          <div style="font-size:11px;color:var(--muted)">Seq: ${seq}</div>
          <div style="font-size:11px;color:var(--muted)">Last: ${lastSamp}</div>
          <div style="font-size:11px;color:${nextColor};font-weight:${p.next_sample_time ? "600" : "400"}">
            Next: ${p.next_sample_time
              ? `${nextLabel} <span style="color:var(--muted);font-weight:400">(${fmtTime(p.next_sample_time)})</span>`
              : nextLabel}
          </div>
          ${interval ? `<div style="font-size:10px;color:var(--muted)">Every ${interval} min</div>` : ""}
        </div>`;
      };

      if (onlinePilots.length) {
        html += `<div style="padding:10px 16px 4px">
          <p style="font-size:10px;font-weight:700;letter-spacing:.1em;color:var(--muted);text-transform:uppercase;margin-bottom:8px">Online (${onlinePilots.length})</p>
          <div style="display:flex;flex-wrap:wrap;gap:8px">${onlinePilots.map(renderPilotCard).join("")}</div>
        </div>`;
      }

      if (offlinePilots.length) {
        html += `<details style="padding:4px 16px 8px">
          <summary style="cursor:pointer;color:var(--muted);font-size:12px;user-select:none;padding:6px 0">Offline pilots (${offlinePilots.length})</summary>
          <div style="display:flex;flex-wrap:wrap;gap:8px;margin-top:8px">${offlinePilots.map(renderPilotCard).join("")}</div>
        </details>`;
      }
    }
    html += `</div>`;

    // Gilson SQL connection card
    html += `<div class="chart-card" style="margin-bottom:16px">
      <div class="chart-card-header">
        <span class="chart-card-title">Gilson 215 — SQL Connection</span>
      </div>
      <div style="padding:12px 16px;display:flex;align-items:center;gap:12px">
        <span class="badge ${gilsonOnline ? "badge-good" : "badge-bad"}" style="font-size:12px;padding:4px 10px">
          ${gilsonOnline ? "Connected" : "Offline"}
        </span>
        <span style="color:var(--muted);font-size:13px">${rs232?.note || (gilsonOnline ? "Gilson SQL reachable." : "Gilson SQL unreachable — check GILSON_SQL_* in .env.")}</span>
      </div>
    </div>`;

    // ── Alarm History card ────────────────────────────────────
    {
      const alarmData  = alarmHistory?.data  || [];
      const alarmTable = alarmHistory?.table || null;
      const alarmCols  = alarmHistory?.columns || (alarmData.length ? Object.keys(alarmData[0]) : []);

      // Detect column names by heuristic (Ignition journal or similar)
      const col = (patterns) => alarmCols.find(c => patterns.some(p => p.test(c))) || null;
      const timeCol     = col([/^eventtime$/i, /^event_time$/i, /^alarmtime$/i, /^timestamp$/i, /^time$/i, /date/i]);
      const pathCol     = col([/displaypath/i, /alarmpath/i, /display_path/i, /^path$/i, /^source$/i, /^name$/i, /description/i]);
      const stateCol    = col([/eventstate/i, /event_state/i, /eventtype/i, /event_type/i, /^state$/i, /^type$/i]);
      const ackByCol    = col([/ackby/i, /ack_by/i, /acknowledgedby/i, /acknowledged_by/i, /^user$/i, /operator/i]);
      const curStateCol = col([/currentstate/i, /current_state/i]);
      const priorityCol = col([/^priority$/i]);
      const labelCol    = col([/^label$/i]);

      // State → badge color
      const stateClass = (val) => {
        const s = String(val || "").toLowerCase();
        if (s.includes("active") && !s.includes("ack") && !s.includes("clear")) return "badge-bad";
        if (s.includes("ack"))    return "badge-pending";
        if (s.includes("clear"))  return "badge-good";
        if (s === "0" || s === "clear") return "badge-good";
        if (s === "1" || s === "active") return "badge-bad";
        if (s === "2" || s === "ack")   return "badge-pending";
        return "badge-pending";
      };
      const stateLabel = (val) => {
        const s = String(val || "").toLowerCase();
        if (/^0$/.test(s)) return "Clear";
        if (/^1$/.test(s)) return "Active";
        if (/^2$/.test(s)) return "Ack";
        return val ?? "–";
      };

      // Summary counts
      let nActive = 0, nUnack = 0;
      alarmData.forEach(row => {
        const sv = String(row[stateCol] || row[curStateCol] || "").toLowerCase();
        if (sv.includes("active") && !sv.includes("ack") && !sv.includes("clear")) nActive++;
        if (sv.includes("unack")) nUnack++;
      });

      html += `<div class="chart-card" style="margin-bottom:16px">
        <div class="chart-card-header">
          <span class="chart-card-title">Alarm History</span>
          <span style="display:flex;gap:6px;align-items:center">
            ${nActive > 0 ? `<span class="badge badge-bad" style="font-size:10px">${nActive} Active</span>` : ""}
            ${nUnack  > 0 ? `<span class="badge badge-pending" style="font-size:10px">${nUnack} Unacknowledged</span>` : ""}
            ${alarmTable ? `<span style="color:var(--muted);font-size:11px">Table: ${alarmTable}</span>` : ""}
          </span>
        </div>`;

      if (!alarmHistory?.found) {
        html += `<div style="color:var(--muted);padding:16px;font-size:13px">
          No alarm journal table found in MAST_SP.<br>
          <span style="font-size:11px">Checked: ALARM_EVENTS, AlarmEvents, Alarms, AlarmHistory, SystemAlarms, AlarmLog, EventLog + GUID-column schema scan.</span>
        </div>`;
      } else if (!alarmData.length) {
        html += `<div style="color:var(--muted);padding:16px;font-size:13px">No alarms in the last 7 days (table: ${alarmTable}).</div>`;
      } else {
        html += `<div style="overflow-x:auto">
          <table class="log-table">
            <thead><tr>
              ${timeCol     ? "<th>Event Time</th>" : ""}
              ${pathCol     ? "<th>Display Path</th>" : ""}
              ${stateCol    ? "<th>Event State</th>" : ""}
              ${curStateCol ? "<th>Current State</th>" : ""}
              ${priorityCol ? "<th>Priority</th>" : ""}
              ${ackByCol    ? "<th>Ack'd By</th>" : ""}
              ${labelCol    ? "<th>Label</th>" : ""}
              ${!pathCol && !stateCol ? alarmCols.map(c => `<th>${c}</th>`).join("") : ""}
            </tr></thead>
            <tbody>`;
        alarmData.slice(0, 100).forEach(row => {
          const sv = row[stateCol] ?? null;
          const cls = stateClass(sv);
          const rowStyle = cls === "badge-bad" ? "background:rgba(239,68,68,0.07)" :
                           cls === "badge-pending" ? "background:rgba(251,191,36,0.07)" : "";
          html += `<tr style="${rowStyle}">
            ${timeCol     ? `<td style="font-size:11px;color:var(--muted);white-space:nowrap">${row[timeCol] ? fmtTime(String(row[timeCol])) : "–"}</td>` : ""}
            ${pathCol     ? `<td style="font-size:12px;max-width:320px;word-break:break-word">${row[pathCol] ?? "–"}</td>` : ""}
            ${stateCol    ? `<td><span class="badge ${cls}" style="font-size:10px">${stateLabel(sv)}</span></td>` : ""}
            ${curStateCol ? `<td style="font-size:11px;color:var(--muted)">${row[curStateCol] ?? "–"}</td>` : ""}
            ${priorityCol ? `<td style="font-size:11px;color:var(--muted)">${row[priorityCol] ?? "–"}</td>` : ""}
            ${ackByCol    ? `<td style="font-size:11px;color:var(--muted)">${row[ackByCol] ?? "–"}</td>` : ""}
            ${labelCol    ? `<td style="font-size:11px;color:var(--muted)">${row[labelCol] ?? "–"}</td>` : ""}
            ${!pathCol && !stateCol ? alarmCols.map(c => {
              const v = row[c] ?? "–";
              const isTs = /time|date|stamp/i.test(c) && String(v).length > 10;
              return `<td style="font-size:11px">${isTs ? fmtTime(String(v)) : v}</td>`;
            }).join("") : ""}
          </tr>`;
        });
        html += `</tbody></table></div>`;
        if (alarmData.length > 100) {
          html += `<div style="padding:8px 16px;color:var(--muted);font-size:11px">Showing 100 of ${alarmData.length} events. Use /api/mast/alarms to query more.</div>`;
        }
      }
      html += `</div>`;
    }

    // ── Instruments card ──────────────────────────────────────
    html += `<div class="chart-card" style="margin-bottom:16px">
      <div class="chart-card-header"><span class="chart-card-title">Installed Instruments</span></div>`;

    const instr = status.instruments || {};
    if (!instr.found || !instr.data?.length) {
      html += `<div style="color:var(--muted);padding:16px;font-size:13px">No instrument records found in MAST_SP.</div>`;
    } else {
      const cols = Object.keys(instr.data[0]);
      const nameCol  = cols.find(c => /name/i.test(c))       || cols[0];
      const typeCol  = cols.find(c => /type/i.test(c));
      const activeCol= cols.find(c => /active|enabled|status/i.test(c));

      html += `<table class="log-table"><thead><tr>
        <th>Name</th>${typeCol ? "<th>Type</th>" : ""}
        <th>Status</th>
        ${cols.filter(c => c !== nameCol && c !== typeCol && c !== activeCol && !/^id$/i.test(c)).map(c => `<th>${c}</th>`).join("")}
      </tr></thead><tbody>`;

      instr.data.forEach(row => {
        const name   = row[nameCol] ?? "–";
        const type   = typeCol   ? (row[typeCol]   ?? "–") : null;
        const active = activeCol ? row[activeCol]           : null;
        const isGilson = /gilson/i.test(String(name));
        const isActive = active === true || active === 1 || String(active).toLowerCase() === "true";
        const badge = active === null || active === undefined
          ? ""
          : `<span class="badge ${isActive ? "badge-good" : "badge-bad"}" style="font-size:10px">${isActive ? "Active" : "Inactive"}</span>`;
        const extra = cols.filter(c => c !== nameCol && c !== typeCol && c !== activeCol && !/^id$/i.test(c))
          .map(c => `<td style="color:var(--muted);font-size:12px">${row[c] ?? "–"}</td>`).join("");
        html += `<tr${isGilson ? ' style="background:rgba(79,142,247,0.08)"' : ""}>
          <td><b>${name}</b>${isGilson ? ' <span style="color:#4f8ef7;font-size:10px;font-weight:600">GILSON</span>' : ""}</td>
          ${typeCol ? `<td style="color:var(--muted)">${type}</td>` : ""}
          <td>${badge || "–"}</td>
          ${extra}
        </tr>`;
      });
      html += `</tbody></table>`;
    }
    html += `</div>`;

    // ── Active Samples card ───────────────────────────────────
    html += `<div class="chart-card" style="margin-bottom:16px">
      <div class="chart-card-header">
        <span class="chart-card-title">Active / Recent Samples</span>
        ${status.active_samples?.end_col === null ? '<span style="color:var(--muted);font-size:11px">End column unknown — showing most recent</span>' : ""}
      </div>`;

    const as = status.active_samples || {};
    if (!as.found || !as.data?.length) {
      html += `<div style="color:var(--muted);padding:16px;font-size:13px">${as.found ? "No active samples running." : "Could not query SampleData."}</div>`;
    } else {
      const scols = Object.keys(as.data[0]);
      html += `<table class="log-table"><thead><tr>${scols.map(c => `<th>${c}</th>`).join("")}</tr></thead><tbody>`;
      as.data.forEach(row => {
        html += `<tr>${scols.map(c => {
          const v = row[c] ?? "–";
          const isTs = /time|start|stop|date/i.test(c) && String(v).length > 10;
          return `<td style="font-size:12px">${isTs ? fmtTime(String(v)) : v}</td>`;
        }).join("")}</tr>`;
      });
      html += `</tbody></table>`;
    }
    html += `</div>`;

    // ── Schedule card ─────────────────────────────────────────
    html += `<div class="chart-card" style="margin-bottom:16px">
      <div class="chart-card-header">
        <span class="chart-card-title">Upcoming Schedule</span>
        ${status.schedule?.table ? `<span style="color:var(--muted);font-size:11px">Table: ${status.schedule.table}</span>` : ""}
      </div>`;

    const sched = status.schedule || {};
    if (!sched.found) {
      html += `<div style="color:var(--muted);padding:16px;font-size:13px">No scheduling table found in MAST_SP
        <br><span style="font-size:11px">Checked: SamplingSchedule, SampleQueue, ScheduledTasks, SamplingOrder, SampleRequests, TaskQueue</span></div>`;
    } else if (!sched.data?.length) {
      html += `<div style="color:var(--muted);padding:16px;font-size:13px">Schedule table found (${sched.table}) but no upcoming entries.</div>`;
    } else {
      const cols2 = Object.keys(sched.data[0]);
      html += `<table class="log-table"><thead><tr>${cols2.map(c => `<th>${c}</th>`).join("")}</tr></thead><tbody>`;
      sched.data.forEach(row => {
        html += `<tr>${cols2.map(c => {
          const v = row[c] ?? "–";
          const isTs = /time|date|start|scheduled/i.test(c) && String(v).length > 10;
          return `<td style="font-size:12px">${isTs ? fmtTime(String(v)) : v}</td>`;
        }).join("")}</tr>`;
      });
      html += `</tbody></table>`;
    }
    html += `</div>`;

    body.innerHTML = html;
  }

  // ── Sample History ─────────────────────────────────────────

  async function showSampleHistory() {
    activeView = "sample-history";
    activeBR = null;
    buildNav();
    renderView();
  }

  async function renderSampleHistory() {
    const today    = new Date();
    const thirtyAgo= new Date(today - 30 * 24 * 3600 * 1000);
    const todayStr = today.toISOString().slice(0, 10);
    const thirtyStr= thirtyAgo.toISOString().slice(0, 10);

    content.innerHTML = `
      <div class="detail-header">
        <button class="back-btn" onclick="App._backToOverview()">← Back</button>
        <h2 class="section-title" style="margin:0">Sample History</h2>
      </div>
      <div class="analytical-controls" style="flex-wrap:wrap;gap:8px;margin-bottom:12px">
        <span style="color:var(--muted);font-size:12px;align-self:center">Days back:</span>
        <select id="sh-days" class="select" style="width:140px" onchange="App._shRefresh()">
          <option value="7">Last 7 days</option>
          <option value="30" selected>Last 30 days</option>
          <option value="90">Last 90 days</option>
          <option value="365">Last 365 days</option>
        </select>
        <input type="text" id="sh-filter" class="select" placeholder="Filter by Vessel / Sample ID…" style="width:220px" oninput="App._shFilter()">
        <button class="btn btn-sm" onclick="App._shRefresh()">Refresh</button>
      </div>
      <div id="sh-info" style="color:var(--muted);font-size:12px;margin-bottom:12px"></div>
      <div id="sh-body"></div>`;
    await _shRefresh();
  }

  let _shRowsCache = [];

  async function _shRefresh() {
    const days = +( document.getElementById("sh-days")?.value || 30 );
    const info = document.getElementById("sh-info");
    if (info) { info.textContent = "Loading…"; info.style.color = "var(--muted)"; }

    let rows = [];
    let mastOnline = false;
    try {
      const r = await fetch(`/api/mast/sample-history?days=${days}`);
      if (r.ok) { const d = await r.json(); rows = d.data || []; mastOnline = true; }
    } catch (_) {}

    _shRowsCache = rows;
    const statusEl = document.getElementById("sh-info");
    if (statusEl) {
      statusEl.textContent = mastOnline
        ? `${rows.length} samples in the last ${days} days`
        : "MAST SQL offline — no data";
      statusEl.style.color = mastOnline ? "var(--muted)" : "var(--warn)";
    }
    _shRender(_shRowsCache);
  }

  function _shFilter() {
    const raw = (document.getElementById("sh-filter")?.value || "").trim().toLowerCase();
    const filtered = raw
      ? _shRowsCache.filter(r => Object.values(r).some(v => String(v ?? "").toLowerCase().includes(raw)))
      : _shRowsCache;
    _shRender(filtered);
  }

  function _shRender(rows) {
    const wrap = document.getElementById("sh-body");
    if (!wrap) return;

    if (!rows.length) {
      wrap.innerHTML = `<div style="color:var(--muted);padding:40px;text-align:center;background:var(--surface);border-radius:var(--radius)">No samples in this range.</div>`;
      return;
    }

    const allCols = Object.keys(rows[0]);
    const sampleIdCol = allCols.find(c => /sampleid|sample_id|samplename/i.test(c)) || allCols[0];
    const vesselCol   = allCols.find(c => /vessel/i.test(c));
    const expCol      = allCols.find(c => /experiment/i.test(c));
    const startCol    = allCols.find(c => /^start$/i.test(c)) || allCols.find(c => /start|begin/i.test(c));
    const stopCol     = allCols.find(c => /^stop$|^end$/i.test(c)) || allCols.find(c => /stop|end|finish/i.test(c));
    const statusCol   = allCols.find(c => /status|state/i.test(c));

    // Priority columns shown prominently, rest as extra
    const primaryCols = [sampleIdCol, vesselCol, expCol, startCol, stopCol, statusCol].filter(Boolean);
    const extraCols   = allCols.filter(c => !primaryCols.includes(c) && !/^id$/i.test(c));

    let html = `<div class="chart-card">
      <div class="chart-card-header"><span class="chart-card-title">${rows.length} Sample${rows.length !== 1 ? "s" : ""}</span>
        <span style="color:var(--muted);font-size:11px">from MAST SampleData</span>
      </div>
      <table class="log-table"><thead><tr>
        ${primaryCols.map(c => `<th>${c}</th>`).join("")}
        ${extraCols.map(c => `<th style="color:var(--muted)">${c}</th>`).join("")}
      </tr></thead><tbody>`;

    rows.forEach(row => {
      const sid    = row[sampleIdCol] ?? "–";
      const vessel = vesselCol  ? (row[vesselCol]  ?? "–") : null;
      const exp    = expCol     ? (row[expCol]     ?? "–") : null;
      const start  = startCol   ? fmtTime(String(row[startCol]  ?? "")) : null;
      const stop   = stopCol    ? (row[stopCol]  ? fmtTime(String(row[stopCol])) : '<span style="color:var(--accent)">Running</span>') : null;
      const stat   = statusCol  ? (row[statusCol] ?? "–") : null;

      const isRunning = stopCol && !row[stopCol];

      html += `<tr${isRunning ? ' style="background:rgba(79,142,247,0.06)"' : ""}>`;
      html += `<td><b>${sid}</b></td>`;
      if (vessel !== null) html += `<td>${vessel}</td>`;
      if (exp    !== null) html += `<td style="color:var(--muted)">${exp}</td>`;
      if (start  !== null) html += `<td style="font-size:12px">${start}</td>`;
      if (stop   !== null) html += `<td style="font-size:12px">${stop}</td>`;
      if (stat   !== null) {
        const okColor = /complet|done|ok|finish/i.test(String(stat)) ? "var(--good)" : /run|activ/i.test(String(stat)) ? "var(--accent)" : "var(--muted)";
        html += `<td><span style="color:${okColor}">${stat}</span></td>`;
      }
      extraCols.forEach(c => {
        const v = row[c] ?? "–";
        const isTs = /time|date/i.test(c) && String(v).length > 10;
        html += `<td style="color:var(--muted);font-size:12px">${isTs ? fmtTime(String(v)) : v}</td>`;
      });
      html += `</tr>`;
    });

    html += `</tbody></table></div>`;
    wrap.innerHTML = html;
  }

  function _backToOverview() { activeView = "bioreactor"; activeBR = null; buildNav(); renderView(); }

  // ── Vi-CELL Cell Counter ────────────────────────────────────

  let _vicellHistFull  = [];  // all rows from current date range (unfiltered)
  let _vicellRowsCache = [];  // after text filter applied
  let _vicellSampFull  = [];  // all (sample_id, sample_date) pairs in range
  let _vicellLatest    = null;
  let _vicellDebounce  = null;

  const _VICELL_METRICS = [
    { key: "viability_pct",       label: "Viability",           unit: "%"         },
    { key: "viable_cells_per_ml", label: "VCD",                 unit: "×10⁶/ml"  },
    { key: "total_cells_per_ml",  label: "TCD",                 unit: "×10⁶/ml"  },
    { key: "avg_diameter_um",     label: "Avg Diameter",        unit: "µm"        },
    { key: "avg_circularity",     label: "Avg Circularity",     unit: ""          },
  ];

  async function showVicell() {
    activeView = "vicell";
    activeBR = null;
    buildNav();
    renderView();
  }

  async function renderVicell() {
    const today    = new Date();
    const yearAgo  = new Date(today - 365 * 24 * 3600 * 1000);
    const todayStr = today.toISOString().slice(0, 10);
    const yearStr  = yearAgo.toISOString().slice(0, 10);

    content.innerHTML = `
      <div class="analytical-controls" style="flex-wrap:wrap;gap:8px">
        <span style="color:var(--muted);font-size:12px;align-self:center">From:</span>
        <input type="date" id="vicell-from" class="select" value="${yearStr}" style="width:140px"
          onchange="App._vicellRefreshDebounced()">
        <span style="color:var(--muted);font-size:12px;align-self:center">To:</span>
        <input type="date" id="vicell-to" class="select" value="${todayStr}" style="width:140px"
          onchange="App._vicellRefreshDebounced()">
        <select id="vicell-metric" class="select" style="width:200px" onchange="App._vicellDrawChart()">
          <option value="">Select metric to chart</option>
          ${_VICELL_METRICS.map(m => `<option value="${m.key}">${m.label}${m.unit ? " (" + m.unit + ")" : ""}</option>`).join("")}
        </select>
        <input type="text" id="vicell-filter" class="select" placeholder="Filter by Sample ID…"
          style="width:180px" oninput="App._vicellFilter()">
        <button class="btn btn-sm" onclick="App._vicellRefresh()">Refresh</button>
        <button class="btn btn-sm btn-secondary" onclick="App._vicellImportClick()"
          title="Import one or more Vi-CELL XR xlsx exports">Import xlsx</button>
        <button class="btn btn-sm btn-secondary" onclick="App._vicellExportXlsx()"
          title="Download an xlsx file matching the ViCell_data template">Export xlsx</button>
        <input type="file" id="vicell-file-input" accept=".xlsx" multiple style="display:none"
          onchange="App._vicellHandleFileImport(event)">
      </div>
      <div id="vicell-range-info" style="color:var(--muted);font-size:12px;margin:6px 0 14px"></div>
      <div style="display:flex;align-items:center;gap:10px;margin-bottom:14px">
        <span style="color:var(--muted);font-size:12px;white-space:nowrap">View sample:</span>
        <select id="vicell-sample-sel" class="select" style="flex:1;max-width:480px"
          onchange="App._vicellSelectSample()">
          <option value="">Latest measurement</option>
        </select>
      </div>
      <div id="vicell-latest-card" style="margin-bottom:16px"></div>
      <div id="vicell-chart-card" class="chart-card" style="display:none;margin-bottom:16px">
        <div class="chart-card-header">
          <span class="chart-card-title" id="vicell-chart-title">Vi-CELL History</span>
        </div>
        <div style="position:relative;height:260px"><canvas id="vicell-chart"></canvas></div>
      </div>
      <div id="vicell-table-wrap"></div>`;

    await _vicellRefresh();
  }

  function _vicellImportClick() {
    const inp = document.getElementById("vicell-file-input");
    if (inp) inp.click();
  }

  function _vicellRefreshDebounced() {
    clearTimeout(_vicellDebounce);
    _vicellDebounce = setTimeout(_vicellRefresh, 600);
  }

  async function _vicellRefresh() {
    const fromEl = document.getElementById("vicell-from");
    const toEl   = document.getElementById("vicell-to");
    const since  = fromEl?.value ? fromEl.value + "T00:00:00" : null;
    const until  = toEl?.value   ? toEl.value   + "T23:59:59" : null;

    let results = [], samples = [], latest = null;
    try {
      const params = new URLSearchParams();
      if (since) params.set("since", since);
      if (until) params.set("until", until);
      const [resResp, sampResp, latResp] = await Promise.all([
        fetch(`/api/vicell/results?${params}`),
        fetch(`/api/vicell/samples?${params}`),
        fetch("/api/vicell/latest"),
      ]);
      if (resResp.ok)  results = (await resResp.json()).results  || [];
      if (sampResp.ok) samples = (await sampResp.json()).samples || [];
      if (latResp.ok)  latest  = (await latResp.json()).result   || null;
    } catch (_) {}

    _vicellHistFull = results;
    _vicellSampFull = samples;
    _vicellLatest   = latest;
    _vicellApplyFilter(latest);
  }

  function _vicellFilter() {
    _vicellApplyFilter(null);
  }

  function _vicellApplyFilter(latestRow) {
    const raw = (document.getElementById("vicell-filter")?.value || "").trim().toLowerCase();

    const rows = raw
      ? _vicellHistFull.filter(r => (r.sample_id || "").toLowerCase().includes(raw))
      : _vicellHistFull;

    const samples = raw
      ? _vicellSampFull.filter(s => (s.sample_id || "").toLowerCase().includes(raw))
      : _vicellSampFull;

    _vicellRowsCache = rows;

    const rangeInfo = document.getElementById("vicell-range-info");
    if (rangeInfo) {
      if (!rows.length) {
        rangeInfo.textContent = raw
          ? `No measurements match "${raw}" in the selected date range.`
          : "No measurements in selected date range.";
        rangeInfo.style.color = "var(--warn)";
      } else {
        rangeInfo.textContent = `${rows.length} measurement${rows.length !== 1 ? "s" : ""}`;
        rangeInfo.style.color = "var(--muted)";
      }
    }

    _vicellPopulateSampleSelector(samples);

    const selEl = document.getElementById("vicell-sample-sel");
    const selKey = selEl?.value;
    if (selKey) {
      const [sid, sdate] = selKey.split("|");
      const hit = rows.find(r => r.sample_id === sid && r.sample_date === sdate);
      _renderVicellCard(hit || null);
    } else if (latestRow !== null) {
      if (raw && rows.length > 0) {
        _renderVicellCard(rows[0]);
      } else {
        _renderVicellCard(_vicellLatest);
      }
    }

    _renderVicellTable(rows);
    _vicellDrawChart();
  }

  function _vicellPopulateSampleSelector(samples) {
    const sel = document.getElementById("vicell-sample-sel");
    if (!sel) return;
    const cur = sel.value;
    sel.innerHTML = '<option value="">Latest measurement</option>';
    samples.forEach(s => {
      const o = document.createElement("option");
      o.value = `${s.sample_id}|${s.sample_date}`;
      const label = s.sample_id || "Unknown";
      const dt = s.sample_date ? fmtTime(s.sample_date) : "no date";
      o.textContent = `${label}  —  ${dt}`;
      if (o.value === cur) o.selected = true;
      sel.appendChild(o);
    });
  }

  function _vicellSelectSample() {
    const sel = document.getElementById("vicell-sample-sel");
    const key = sel?.value;
    if (!key) {
      _renderVicellCard(_vicellLatest);
      return;
    }
    const [sid, sdate] = key.split("|");
    const hit = _vicellRowsCache.find(r => r.sample_id === sid && r.sample_date === sdate);
    _renderVicellCard(hit || null);
  }

  function _renderVicellCard(row) {
    const wrap = document.getElementById("vicell-latest-card");
    if (!wrap) return;
    if (!row) {
      wrap.innerHTML = `<div class="chart-card" style="color:var(--muted);padding:20px;text-align:center">
        No Vi-CELL measurements stored yet — use Import xlsx to load data.</div>`;
      return;
    }

    const fmtV  = v => v == null ? "—" : Number(v).toFixed(2);
    const fmtV1 = v => v == null ? "—" : Number(v).toFixed(1);
    const viab  = row.viability_pct;
    const viabColor = viab == null ? "var(--accent)"
      : viab >= 90 ? "var(--good)"
      : viab >= 75 ? "#f5a623"
      : "var(--bad)";

    const kpis = [
      { label: "Viability",       val: viab == null ? "—" : viab.toFixed(1) + "%",   color: viabColor },
      { label: "VCD (×10⁶/ml)",  val: fmtV(row.viable_cells_per_ml),                color: "var(--accent)" },
      { label: "TCD (×10⁶/ml)",  val: fmtV(row.total_cells_per_ml),                 color: "var(--accent)" },
      { label: "Avg Diam (µm)",   val: fmtV1(row.avg_diameter_um),                   color: "var(--accent)" },
      { label: "Circularity",     val: fmtV(row.avg_circularity),                    color: "var(--accent)" },
      { label: "Dilution",        val: row.dilution_factor != null ? row.dilution_factor : "—", color: "var(--text)" },
    ];

    const ts = row.sample_date ? fmtTime(row.sample_date) : "unknown date";
    const sid = row.sample_id || "–";
    const ct  = row.cell_type ? `  •  ${row.cell_type}` : "";

    let html = `<div class="chart-card">
      <div class="chart-card-header">
        <span class="chart-card-title">Measurement — ${ts}</span>
        <span style="color:var(--muted);font-size:12px">Sample: ${sid}${ct}</span>
      </div>
      <div class="kpi-row" style="margin:0">`;

    kpis.forEach(k => {
      html += `<div class="kpi-card" style="cursor:default">
        <div class="kpi-label">${k.label}</div>
        <div class="kpi-value" style="color:${k.color};font-size:20px">${k.val}</div>
      </div>`;
    });

    html += `</div></div>`;
    wrap.innerHTML = html;
  }

  function _vicellDrawChart() {
    const metricSel = document.getElementById("vicell-metric");
    const metricKey = metricSel?.value;
    const chartCard = document.getElementById("vicell-chart-card");
    if (!chartCard) return;

    if (!metricKey) { chartCard.style.display = "none"; return; }

    const meta = _VICELL_METRICS.find(m => m.key === metricKey);
    const points = _vicellRowsCache
      .filter(r => r[metricKey] != null && r.sample_date)
      .map(r => ({ x: r.sample_date, y: r[metricKey], sample_id: r.sample_id }))
      .sort((a, b) => a.x.localeCompare(b.x));

    if (!points.length) { chartCard.style.display = "none"; return; }

    const unit = meta?.unit || "";
    const label = meta?.label || metricKey;
    document.getElementById("vicell-chart-title").textContent =
      `${label}${unit ? " (" + unit + ")" : ""} — History`;
    chartCard.style.display = "";

    if (charts["vicell-chart"]) { charts["vicell-chart"].destroy(); delete charts["vicell-chart"]; }
    const canvas = document.getElementById("vicell-chart");
    if (!canvas) return;

    charts["vicell-chart"] = new Chart(canvas, {
      type: "line",
      data: {
        datasets: [{
          label,
          data: points,
          borderColor: "#4f8ef7",
          backgroundColor: "#4f8ef722",
          borderWidth: 2, pointRadius: 6, pointHoverRadius: 8, fill: false, tension: 0,
        }]
      },
      options: {
        responsive: true, maintainAspectRatio: false, animation: false,
        interaction: { mode: "index", intersect: false },
        plugins: {
          legend: { display: false },
          tooltip: { callbacks: {
            label: ctx => ` ${label}: ${Number(ctx.parsed.y).toPrecision(4)}${unit ? " " + unit : ""}`,
            afterLabel: ctx => ctx.raw.sample_id ? ` Sample: ${ctx.raw.sample_id}` : "",
          }}
        },
        scales: {
          x: { type: "time", time: { tooltipFormat: "MMM d, HH:mm", displayFormats: { hour: "MMM d HH:mm", day: "MMM d" } },
               ticks: { color: "#6b7280", maxTicksLimit: 10 }, grid: { color: "#2a2d3e" } },
          y: { ticks: { color: "#6b7280" }, grid: { color: "#2a2d3e" },
               title: { display: !!unit, text: unit, color: "#6b7280" } }
        }
      }
    });
  }

  function _renderVicellTable(rows) {
    const wrap = document.getElementById("vicell-table-wrap");
    if (!wrap) return;
    if (!rows.length) {
      wrap.innerHTML = `<div style="color:var(--muted);padding:20px;text-align:center;background:var(--surface);border-radius:var(--radius)">
        No Vi-CELL results in the selected time window.</div>`;
      return;
    }

    const fmtV  = v => v == null ? "—" : Number(v).toFixed(2);
    const fmtV1 = v => v == null ? "—" : Number(v).toFixed(1);
    const viabFmt = v => v == null ? "—" : Number(v).toFixed(1) + "%";
    const viabColor = v => v == null ? "var(--text)" : v >= 90 ? "var(--good)" : v >= 75 ? "#f5a623" : "var(--bad)";

    let html = `<div class="chart-card">
      <div class="chart-card-header"><span class="chart-card-title">${rows.length} Measurement${rows.length !== 1 ? "s" : ""}</span></div>
      <table class="log-table"><thead><tr>
        <th>Date</th><th>Sample ID</th><th>Viability</th>
        <th>VCD (×10⁶/ml)</th><th>TCD (×10⁶/ml)</th>
        <th>Avg Diam (µm)</th><th>Circ.</th><th>Cell Type</th><th>Dilution</th>
      </tr></thead><tbody>`;

    rows.forEach(r => {
      const dt = r.sample_date ? fmtTime(r.sample_date) : "—";
      html += `<tr>
        <td><b>${dt}</b></td>
        <td style="color:var(--muted)">${r.sample_id || "—"}</td>
        <td style="font-weight:700;color:${viabColor(r.viability_pct)}">${viabFmt(r.viability_pct)}</td>
        <td style="font-variant-numeric:tabular-nums">${fmtV(r.viable_cells_per_ml)}</td>
        <td style="font-variant-numeric:tabular-nums">${fmtV(r.total_cells_per_ml)}</td>
        <td style="font-variant-numeric:tabular-nums">${fmtV1(r.avg_diameter_um)}</td>
        <td style="font-variant-numeric:tabular-nums">${fmtV(r.avg_circularity)}</td>
        <td style="color:var(--muted)">${r.cell_type || "—"}</td>
        <td style="color:var(--muted)">${r.dilution_factor != null ? r.dilution_factor : "—"}</td>
      </tr>`;
    });

    html += `</tbody></table></div>`;
    wrap.innerHTML = html;
  }

  async function _vicellHandleFileImport(event) {
    const files = Array.from(event.target.files || []);
    event.target.value = "";
    if (!files.length) return;

    const info = document.getElementById("vicell-range-info");
    if (info) {
      info.textContent = `Importing ${files.length} file${files.length !== 1 ? "s" : ""}…`;
      info.style.color = "var(--accent)";
    }

    let totalInserted = 0, totalSkipped = 0, errors = 0;
    for (const file of files) {
      const fd = new FormData();
      fd.append("file", file);
      try {
        const res = await fetch("/api/vicell/import-xlsx", { method: "POST", body: fd });
        if (res.ok) {
          const d = await res.json();
          totalInserted += d.inserted || 0;
          totalSkipped  += d.skipped  || 0;
        } else { errors++; }
      } catch (_) { errors++; }
    }

    if (info) {
      const msg = `Import done: ${totalInserted} new result${totalInserted !== 1 ? "s" : ""}` +
        (totalSkipped ? `, ${totalSkipped} duplicate${totalSkipped !== 1 ? "s" : ""} skipped` : "") +
        (errors ? ` (${errors} file error${errors !== 1 ? "s" : ""})` : "");
      info.textContent = msg;
      info.style.color = errors ? "var(--warn)" : "var(--good)";
    }

    await _vicellRefresh();
  }

  // ── xlsx exports (analytical templates) ───────────────────
  // Triggers a browser download from a backend endpoint. Uses a hidden anchor
  // so the current view isn't navigated away from.
  function _downloadUrl(url, filename) {
    const a = document.createElement("a");
    a.href = url;
    if (filename) a.download = filename;
    a.style.display = "none";
    document.body.appendChild(a);
    a.click();
    document.body.removeChild(a);
  }

  function _rangeParams(fromId, toId) {
    const params = new URLSearchParams();
    const f = document.getElementById(fromId)?.value;
    const t = document.getElementById(toId)?.value;
    if (f) params.set("since", f + "T00:00:00");
    if (t) params.set("until", t + "T23:59:59");
    return params;
  }

  function _vicellExportXlsx() {
    const params = _rangeParams("vicell-from", "vicell-to");
    const filt = (document.getElementById("vicell-filter")?.value || "").trim();
    if (filt) params.set("sample_id", filt);
    const qs = params.toString();
    _downloadUrl("/api/vicell/export-xlsx" + (qs ? "?" + qs : ""));
  }

  function _novaExportXlsx() {
    const params = _rangeParams("nova-from", "nova-to");
    const qs = params.toString();
    _downloadUrl("/api/nova/export-xlsx" + (qs ? "?" + qs : ""));
  }

  function _biohtExportXlsx() {
    const params = _rangeParams("bioht-from", "bioht-to");
    const qs = params.toString();
    _downloadUrl("/api/bioht/export-xlsx" + (qs ? "?" + qs : ""));
  }

  return { init, openParamModal, closeModal, showConnectionLog, showTagBrowser,
           _tbBrowse, _tbRead, _backToOverview, showAnalytical, showNova,
           _novaRefresh, _novaRefreshDebounced, _novaFilter, _novaDrawChart,
           _novaSelectSample, _novaImportCSV, _novaHandleFileImport,
           _novaExportXlsx,
           _biohtRefresh, _biohtRefreshDebounced, _biohtFilter, _biohtDrawChart, _biohtConsolidateToggle,
           _biohtSelectSample, _biohtImportTxt, _biohtHandleFileImport,
           _biohtExportXlsx,
           showMastStatus, _mastStatusRefresh,
           showSampleHistory, _shRefresh, _shFilter,
           showVicell, _vicellRefresh, _vicellRefreshDebounced,
           _vicellFilter, _vicellSelectSample, _vicellDrawChart,
           _vicellImportClick, _vicellHandleFileImport, _vicellExportXlsx };
})();

document.addEventListener("DOMContentLoaded", App.init);
