/* ═══════════════════════════════════════════════════════════════════
   app.js — Sentry LiDAR Dashboard front-end
   ───────────────────────────────────────────────────────────────────
   Talks to server.py:
     GET  /api/status   → mode + esp32 link state  (badge / status dot)
     GET  /api/scan     → latest 360° scan         (radar render)
     POST /api/navigate → send target to the ESP32 (serial preview)

   Coordinate convention (matches the server):
     • Robot sits at the centre of the radar.
     • angle 0° = +X (right / east), 90° = +Y (up / north).
     • distances are millimetres; the visible "range" sets the scale.
   ═══════════════════════════════════════════════════════════════════ */

(() => {
  "use strict";

  // ── DOM handles ─────────────────────────────────────────────────────
  const canvas    = document.getElementById("radar-canvas");
  const container = document.getElementById("radar-container");
  const ctx       = canvas.getContext("2d");

  const modeBadge = document.getElementById("mode-badge");
  const statusDot = document.getElementById("status-dot");
  const statPoints= document.getElementById("stat-points");
  const statFps   = document.getElementById("stat-fps");

  const inputX    = document.getElementById("input-x");
  const inputY    = document.getElementById("input-y");
  const inputSpeed= document.getElementById("input-speed");
  const speedLabel= document.getElementById("speed-label");

  const targetDisplay = document.getElementById("target-display");
  const btnNavigate   = document.getElementById("btn-navigate");
  const btnClear      = document.getElementById("btn-clear");
  const serialPreview = document.getElementById("serial-preview");

  const cursorX   = document.getElementById("cursor-x");
  const cursorY   = document.getElementById("cursor-y");
  const cursorDist= document.getElementById("cursor-dist");
  const cursorAngle = document.getElementById("cursor-angle");

  const zoomIn    = document.getElementById("zoom-in");
  const zoomOut   = document.getElementById("zoom-out");
  const zoomLabel = document.getElementById("zoom-label");

  // ── State ───────────────────────────────────────────────────────────
  let rangeMm   = 4000;                 // visible radius in mm (zoom)
  const RANGES  = [1000, 2000, 4000, 6000, 8000, 12000];
  let target    = null;                 // {x_mm, y_mm} or null
  let latestPoints = [];
  let lastScanTs = 0;
  let fpsCounter = 0, fpsValue = 0;

  // ── Canvas sizing (keep the drawing buffer matched to the element) ──
  function resizeCanvas() {
    const rect = container.getBoundingClientRect();
    const dpr  = window.devicePixelRatio || 1;
    canvas.width  = Math.round(rect.width  * dpr);
    canvas.height = Math.round(rect.height * dpr);
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    render();
  }
  window.addEventListener("resize", resizeCanvas);

  function geom() {
    const rect = container.getBoundingClientRect();
    const w = rect.width, h = rect.height;
    const cx = w / 2, cy = h / 2;
    const maxR = Math.min(cx, cy) - 6;
    return { w, h, cx, cy, maxR, scale: maxR / rangeMm };
  }

  // ── Render the radar ────────────────────────────────────────────────
  function render() {
    const { w, h, cx, cy, maxR, scale } = geom();
    ctx.clearRect(0, 0, w, h);

    // Concentric range rings + labels
    ctx.lineWidth = 1;
    const rings = 4;
    for (let i = 1; i <= rings; i++) {
      const rr = (maxR * i) / rings;
      ctx.strokeStyle = "rgba(0,255,170,0.12)";
      ctx.beginPath();
      ctx.arc(cx, cy, rr, 0, Math.PI * 2);
      ctx.stroke();

      const labelMm = (rangeMm * i) / rings;
      ctx.fillStyle = "rgba(72,79,88,0.9)";
      ctx.font = "10px 'JetBrains Mono', monospace";
      ctx.fillText((labelMm / 1000).toFixed(1) + "m", cx + 4, cy - rr + 12);
    }

    // Radial spokes every 30°
    ctx.strokeStyle = "rgba(0,255,170,0.06)";
    for (let a = 0; a < 360; a += 30) {
      const rad = (a * Math.PI) / 180;
      ctx.beginPath();
      ctx.moveTo(cx, cy);
      ctx.lineTo(cx + Math.cos(rad) * maxR, cy - Math.sin(rad) * maxR);
      ctx.stroke();
    }

    // Scan points (red if within 500 mm, else accent green)
    latestPoints.forEach((p) => {
      if (!p.distance) return;
      const rad = (p.angle * Math.PI) / 180;
      const x = cx + Math.cos(rad) * p.distance * scale;
      const y = cy - Math.sin(rad) * p.distance * scale;
      const close = p.distance < 500;
      ctx.fillStyle   = close ? "#ff3366" : "#00ffaa";
      ctx.shadowColor = close ? "#ff3366" : "#00ffaa";
      ctx.shadowBlur  = close ? 8 : 3;
      ctx.beginPath();
      ctx.arc(x, y, close ? 2.6 : 1.6, 0, Math.PI * 2);
      ctx.fill();
    });
    ctx.shadowBlur = 0;

    // Target marker
    if (target) {
      const tx = cx + target.x_mm * scale;
      const ty = cy - target.y_mm * scale;
      ctx.strokeStyle = "#ff3366";
      ctx.lineWidth = 1.5;
      ctx.beginPath(); ctx.arc(tx, ty, 8, 0, Math.PI * 2); ctx.stroke();
      ctx.beginPath();
      ctx.moveTo(tx - 12, ty); ctx.lineTo(tx + 12, ty);
      ctx.moveTo(tx, ty - 12); ctx.lineTo(tx, ty + 12);
      ctx.stroke();
      // Line from robot to target
      ctx.strokeStyle = "rgba(255,51,102,0.35)";
      ctx.setLineDash([4, 4]);
      ctx.beginPath(); ctx.moveTo(cx, cy); ctx.lineTo(tx, ty); ctx.stroke();
      ctx.setLineDash([]);
    }
  }

  // ── Pixel ↔ mm helpers ──────────────────────────────────────────────
  function pixelToMm(clientX, clientY) {
    const rect = container.getBoundingClientRect();
    const { cx, cy, scale } = geom();
    const px = clientX - rect.left;
    const py = clientY - rect.top;
    return {
      x_mm: Math.round((px - cx) / scale / 10) * 10,   // snap to 10 mm
      y_mm: Math.round(-(py - cy) / scale / 10) * 10,
    };
  }

  // ── Target handling ─────────────────────────────────────────────────
  function setTarget(x_mm, y_mm) {
    target = { x_mm, y_mm };
    inputX.value = x_mm;
    inputY.value = y_mm;
    btnNavigate.disabled = false;

    const dist = Math.round(Math.hypot(x_mm, y_mm));
    const bearing = ((Math.atan2(y_mm, x_mm) * 180) / Math.PI + 360) % 360;
    targetDisplay.classList.add("has-target");
    targetDisplay.innerHTML = `
      <div class="target-info">
        <div class="coord"><span class="coord-label">X</span><span class="coord-value">${x_mm} mm</span></div>
        <div class="coord"><span class="coord-label">Y</span><span class="coord-value">${y_mm} mm</span></div>
        <div class="coord"><span class="coord-label">DIST</span><span class="coord-value">${dist} mm</span></div>
        <div class="coord"><span class="coord-label">BEARING</span><span class="coord-value">${bearing.toFixed(1)}°</span></div>
      </div>`;
    render();
  }

  function clearTarget() {
    target = null;
    inputX.value = "";
    inputY.value = "";
    btnNavigate.disabled = true;
    targetDisplay.classList.remove("has-target");
    targetDisplay.innerHTML = `<span class="target-placeholder">Click the map to set a target</span>`;
    serialPreview.innerHTML = `<p class="serial-hint">Set a target to see the serial commands.</p>`;
    render();
  }

  // ── Canvas interaction ──────────────────────────────────────────────
  canvas.addEventListener("click", (e) => {
    const { x_mm, y_mm } = pixelToMm(e.clientX, e.clientY);
    setTarget(x_mm, y_mm);
  });

  canvas.addEventListener("mousemove", (e) => {
    const { x_mm, y_mm } = pixelToMm(e.clientX, e.clientY);
    const dist = Math.round(Math.hypot(x_mm, y_mm));
    const angle = ((Math.atan2(y_mm, x_mm) * 180) / Math.PI + 360) % 360;
    cursorX.textContent = `${x_mm} mm`;
    cursorY.textContent = `${y_mm} mm`;
    cursorDist.textContent = `${dist} mm`;
    cursorAngle.textContent = `${angle.toFixed(1)}°`;
  });

  // Typing X/Y updates the target too
  [inputX, inputY].forEach((inp) =>
    inp.addEventListener("input", () => {
      const x = parseInt(inputX.value, 10);
      const y = parseInt(inputY.value, 10);
      if (Number.isFinite(x) && Number.isFinite(y)) setTarget(x, y);
    })
  );

  inputSpeed.addEventListener("input", () => {
    speedLabel.textContent = inputSpeed.value + "%";
  });

  btnClear.addEventListener("click", clearTarget);

  // ── Zoom ────────────────────────────────────────────────────────────
  function setRangeIndex(idx) {
    idx = Math.max(0, Math.min(RANGES.length - 1, idx));
    rangeMm = RANGES[idx];
    zoomLabel.textContent = "Range: " + (rangeMm / 1000).toFixed(1) + " m";
    render();
  }
  zoomIn.addEventListener("click", () =>
    setRangeIndex(RANGES.indexOf(rangeMm) - 1)
  );
  zoomOut.addEventListener("click", () =>
    setRangeIndex(RANGES.indexOf(rangeMm) + 1)
  );

  // ── Send command ────────────────────────────────────────────────────
  btnNavigate.addEventListener("click", async () => {
    if (!target) return;
    btnNavigate.disabled = true;
    try {
      const res = await fetch("/api/navigate", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          x_mm: target.x_mm,
          y_mm: target.y_mm,
          speed_pct: parseInt(inputSpeed.value, 10),
        }),
      });
      const data = await res.json();
      renderSerial(data);
    } catch (err) {
      serialPreview.innerHTML =
        `<p class="serial-hint" style="color:var(--target-color)">Request failed: ${err}</p>`;
    } finally {
      btnNavigate.disabled = false;
    }
  });

  function esc(s) {
    return String(s).replace(/[&<>]/g, (c) =>
      ({ "&": "&amp;", "<": "&lt;", ">": "&gt;" }[c])
    );
  }

  function renderSerial(data) {
    const sc = data.serial_commands || {};
    const esp = data.esp32 || {};
    const linkColor = esp.link === "live" ? "var(--accent)"
                    : esp.link === "error" ? "var(--target-color)"
                    : "var(--warning)";
    const linkText = esp.link === "live" ? "SENT TO ESP32"
                   : esp.link === "error" ? "ESP32 ERROR"
                   : "MOCK (no serial link)";

    let html = `
      <div class="serial-block">
        <div class="serial-block-title" style="color:${linkColor}">● ${linkText}</div>
        <div class="serial-cmd">${esc((sc.json_sent && sc.json_sent.command) || esp.transmitted || "")}</div>
      </div>
      <div class="serial-block">
        <div class="serial-block-title">Target</div>
        <div class="serial-field"><span class="serial-field-name">distance</span><span class="serial-field-value">${data.distance_mm} mm</span></div>
        <div class="serial-field"><span class="serial-field-name">bearing</span><span class="serial-field-value">${data.bearing_deg}°</span></div>
      </div>`;

    if (sc.ascii) {
      html += `
        <div class="serial-block">
          <div class="serial-block-title">ASCII (NMEA-style)</div>
          <div class="serial-cmd">${esc(sc.ascii.command)}</div>
          <div class="serial-field"><span class="serial-field-name">bytes</span><span class="serial-field-value">${sc.ascii.bytes}</span></div>
        </div>`;
    }
    if (sc.binary) {
      html += `
        <div class="serial-block">
          <div class="serial-block-title">Binary</div>
          <div class="serial-cmd">${esc(sc.binary.command_hex)}</div>
          <div class="serial-field"><span class="serial-field-name">bytes</span><span class="serial-field-value">${sc.binary.bytes}</span></div>
        </div>`;
    }
    serialPreview.innerHTML = html;
  }

  // ── Polling: status (1 Hz) and scan (~8 Hz) ─────────────────────────
  async function pollStatus() {
    try {
      const res = await fetch("/api/status");
      const d = await res.json();
      const mode = d.mode || "unknown";
      modeBadge.textContent = mode.toUpperCase();
      modeBadge.className = "badge " + (mode === "live" ? "live" : mode === "simulate" ? "simulate" : "error");
      statusDot.classList.toggle("online", mode === "live" || mode === "simulate");
    } catch {
      modeBadge.textContent = "OFFLINE";
      modeBadge.className = "badge error";
      statusDot.classList.remove("online");
    }
  }

  async function pollScan() {
    try {
      const res = await fetch("/api/scan");
      const d = await res.json();
      latestPoints = d.points || [];
      statPoints.textContent = d.count || 0;
      if (d.timestamp && d.timestamp !== lastScanTs) {
        lastScanTs = d.timestamp;
        fpsCounter++;
      }
      render();
    } catch {
      latestPoints = [];
      statPoints.textContent = "—";
      render();
    }
  }

  // FPS = scans observed per second
  setInterval(() => { fpsValue = fpsCounter; fpsCounter = 0; statFps.textContent = fpsValue; }, 1000);

  // ── Boot ────────────────────────────────────────────────────────────
  resizeCanvas();
  clearTarget();
  setRangeIndex(RANGES.indexOf(rangeMm));
  pollStatus();
  pollScan();
  setInterval(pollStatus, 1000);
  setInterval(pollScan, 125);
})();
