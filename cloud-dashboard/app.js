/* ═══════════════════════════════════════════════════════════════════
   app.js — SENTRY Cloud Portal
   Full controls: drive, detection, LiDAR map, A* navigate,
   map save/load, home position, waypoint recording, path replay
   ═══════════════════════════════════════════════════════════════════ */
(() => {
  "use strict";

  // ── DOM ───────────────────────────────────────────────────────────
  const gatewayInput   = document.getElementById("gateway-url");
  const connectBtn     = document.getElementById("btn-connect");
  const pingDisplay    = document.getElementById("ping-display");
  const robotBadge     = document.getElementById("robot-status-badge");
  const clockVal       = document.getElementById("clock");

  const canvas         = document.getElementById("radar-canvas");
  const container      = document.getElementById("radar-container");
  const ctx            = canvas.getContext("2d");

  const zoomInBtn      = document.getElementById("zoom-in");
  const zoomOutBtn     = document.getElementById("zoom-out");
  const zoomLabel      = document.getElementById("zoom-label");

  const batFill        = document.getElementById("bat-fill");
  const batPct         = document.getElementById("bat-pct");

  const targetDisplay  = document.getElementById("target-display");
  const inputX         = document.getElementById("input-x");
  const inputY         = document.getElementById("input-y");
  const navigateBtn    = document.getElementById("btn-navigate");
  const clearBtn       = document.getElementById("btn-clear");

  const speedSlider    = document.getElementById("speed-slider");
  const speedDisplay   = document.getElementById("speed-display");

  const logList        = document.getElementById("log-list");
  const threatFlash    = document.getElementById("threat-flash");

  const remoteVideo    = document.getElementById("remote-video");
  const fallbackImg    = document.getElementById("fallback-img");
  const feedPlaceholder= document.getElementById("feed-placeholder");
  const streamTypeTag  = document.getElementById("stream-type-tag");

  const btnDetection   = document.getElementById("btn-detection");
  const btnAuto        = document.getElementById("btn-auto");
  const mapStatusTag   = document.getElementById("map-status-tag");
  const pathStatusDisplay = document.getElementById("path-status-display");
  const pathSteps      = document.getElementById("path-steps");
  const waypointCount  = document.getElementById("waypoint-count");
  const waypointCountTag = document.getElementById("waypoint-count-tag");
  const detectionList  = document.getElementById("detection-list");

  const mapNameInput   = document.getElementById("map-name");
  const pathNameInput  = document.getElementById("path-name");

  // ── State ─────────────────────────────────────────────────────────
  let socket = null, peerConnection = null, fallbackImgBlobUrl = null;
  let rangeMm = 4000;
  const RANGES = [500, 1000, 2000, 3000, 4000, 6000, 8000, 12000];
  let target = null, lidarPoints = [], speedVal = 80;
  let pingInterval = null, pingStart = 0, teleopTimer = null;
  let detectionEnabled = false, autoEnabled = false;
  let recordedWaypoints = [], activePath = [], homeCell = null;
  let gridData = null, gridOrigin = 200, cellMm = 25, downsample = 2;
  let cursorMm = { x_mm: 0, y_mm: 0 };

  const webrtcConfig = { iceServers: [{ urls: "stun:stun.l.google.com:19302" }] };

  // ── Canvas ────────────────────────────────────────────────────────
  function resizeCanvas() {
    const rect = container.getBoundingClientRect();
    const dpr  = window.devicePixelRatio || 1;
    canvas.width  = Math.round(rect.width  * dpr);
    canvas.height = Math.round(rect.height * dpr);
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    renderRadar();
  }
  window.addEventListener("resize", resizeCanvas);

  function getGeom() {
    const rect = container.getBoundingClientRect();
    const cx = rect.width / 2, cy = rect.height / 2;
    const maxR = Math.min(cx, cy) - 10;
    return { cx, cy, maxR, scale: maxR / rangeMm };
  }

  function cellToCanvas(r, c, cx, cy, scale) {
    return {
      px: cx + (c - gridOrigin) * cellMm * scale,
      py: cy - (r - gridOrigin) * cellMm * scale,
    };
  }

  function pixelToMm(clientX, clientY) {
    const rect = container.getBoundingClientRect();
    const { cx, cy, scale } = getGeom();
    return {
      x_mm: Math.round((clientX - rect.left - cx) / scale / 10) * 10,
      y_mm: Math.round(-(clientY - rect.top  - cy) / scale / 10) * 10,
    };
  }

  // ── Render ────────────────────────────────────────────────────────
  function renderRadar() {
    const { cx, cy, maxR, scale } = getGeom();
    ctx.clearRect(0, 0, canvas.width / (window.devicePixelRatio||1), canvas.height / (window.devicePixelRatio||1));

    // Range rings
    for (let i = 1; i <= 4; i++) {
      const rr = (maxR * i) / 4;
      ctx.strokeStyle = "rgba(0,255,136,0.12)"; ctx.lineWidth = 1;
      ctx.beginPath(); ctx.arc(cx, cy, rr, 0, Math.PI * 2); ctx.stroke();
      ctx.fillStyle = "rgba(0,170,85,0.7)"; ctx.font = "10px 'JetBrains Mono',monospace";
      ctx.fillText(((rangeMm * i / 4) / 1000).toFixed(1) + "m", cx + 4, cy - rr + 12);
    }

    // Spokes
    ctx.strokeStyle = "rgba(0,255,136,0.05)";
    for (let a = 0; a < 360; a += 30) {
      const r = a * Math.PI / 180;
      ctx.beginPath(); ctx.moveTo(cx, cy);
      ctx.lineTo(cx + Math.cos(r) * maxR, cy - Math.sin(r) * maxR); ctx.stroke();
    }

    // Occupancy grid
    if (gridData) {
      const cellPx = cellMm * downsample * scale;
      for (let r = 0; r < gridData.length; r++) {
        for (let c = 0; c < (gridData[0]?.length || 0); c++) {
          if (!gridData[r][c]) continue;
          const { px, py } = cellToCanvas(r * downsample, c * downsample, cx, cy, scale);
          ctx.fillStyle = "rgba(255,80,80,0.4)";
          ctx.fillRect(px - cellPx/2, py - cellPx/2, cellPx, cellPx);
        }
      }
    }

    // Active path
    if (activePath.length > 1) {
      ctx.strokeStyle = "rgba(0,200,255,0.7)"; ctx.lineWidth = 2;
      ctx.setLineDash([4, 3]);
      ctx.beginPath();
      activePath.forEach(([pr, pc], i) => {
        const { px, py } = cellToCanvas(pr, pc, cx, cy, scale);
        i === 0 ? ctx.moveTo(px, py) : ctx.lineTo(px, py);
      });
      ctx.stroke(); ctx.setLineDash([]);
    }

    // Recorded waypoints
    if (recordedWaypoints.length) {
      ctx.strokeStyle = "rgba(255,200,0,0.6)"; ctx.lineWidth = 1.5;
      ctx.setLineDash([3, 4]);
      ctx.beginPath();
      recordedWaypoints.forEach(({ x_mm, y_mm }, i) => {
        const px = cx + x_mm * scale, py = cy - y_mm * scale;
        i === 0 ? ctx.moveTo(px, py) : ctx.lineTo(px, py);
      });
      ctx.stroke(); ctx.setLineDash([]);
      recordedWaypoints.forEach(({ x_mm, y_mm }, i) => {
        const px = cx + x_mm * scale, py = cy - y_mm * scale;
        ctx.fillStyle = "#ffcc00"; ctx.strokeStyle = "#000"; ctx.lineWidth = 1;
        ctx.beginPath(); ctx.arc(px, py, 5, 0, Math.PI * 2); ctx.fill(); ctx.stroke();
        ctx.fillStyle = "#000"; ctx.font = "bold 8px monospace"; ctx.textAlign = "center";
        ctx.fillText(i + 1, px, py + 3); ctx.textAlign = "left";
      });
    }

    // Home marker
    if (homeCell) {
      const { px, py } = cellToCanvas(homeCell[0], homeCell[1], cx, cy, scale);
      ctx.fillStyle = "#ffcc00"; ctx.shadowColor = "#ffcc00"; ctx.shadowBlur = 10;
      ctx.font = "18px sans-serif"; ctx.textAlign = "center";
      ctx.fillText("⌂", px, py + 6); ctx.textAlign = "left"; ctx.shadowBlur = 0;
    }

    // LiDAR scan points
    lidarPoints.forEach(p => {
      if (!p.distance || p.distance < 150) return;
      const rad = p.angle * Math.PI / 180;
      const x = cx + Math.cos(rad) * p.distance * scale;
      const y = cy - Math.sin(rad) * p.distance * scale;
      const close = p.distance < 500;
      ctx.fillStyle   = close ? "#ff3366" : "#00ffaa";
      ctx.shadowColor = close ? "#ff3366" : "#00ffaa";
      ctx.shadowBlur  = close ? 6 : 2;
      ctx.beginPath(); ctx.arc(x, y, close ? 2.5 : 1.8, 0, Math.PI * 2); ctx.fill();
    });
    ctx.shadowBlur = 0;

    // Target
    if (target) {
      const tx = cx + target.x_mm * scale, ty = cy - target.y_mm * scale;
      ctx.strokeStyle = "#ff3366"; ctx.lineWidth = 1.5;
      ctx.beginPath(); ctx.arc(tx, ty, 9, 0, Math.PI * 2); ctx.stroke();
      ctx.beginPath();
      ctx.moveTo(tx - 14, ty); ctx.lineTo(tx + 14, ty);
      ctx.moveTo(tx, ty - 14); ctx.lineTo(tx, ty + 14); ctx.stroke();
      ctx.strokeStyle = "rgba(255,51,102,0.25)"; ctx.setLineDash([4,4]);
      ctx.beginPath(); ctx.moveTo(cx,cy); ctx.lineTo(tx,ty); ctx.stroke();
      ctx.setLineDash([]);
    }

    // Robot dot
    ctx.fillStyle = "#ff3c00"; ctx.shadowColor = "#ff3c00"; ctx.shadowBlur = 8;
    ctx.beginPath(); ctx.arc(cx, cy, 6, 0, Math.PI * 2); ctx.fill();
    ctx.shadowBlur = 0;
  }

  // ── Target ────────────────────────────────────────────────────────
  function setTarget(x_mm, y_mm) {
    target = { x_mm, y_mm };
    inputX.value = x_mm; inputY.value = y_mm;
    navigateBtn.disabled = false;
    const dist    = Math.round(Math.hypot(x_mm, y_mm));
    const bearing = ((Math.atan2(y_mm, x_mm) * 180 / Math.PI) + 360) % 360;
    targetDisplay.innerHTML = `<div style="display:grid;grid-template-columns:1fr 1fr;gap:4px;width:100%">
      <span style="color:#00aa55;font-size:9px">X</span><span>${x_mm} mm</span>
      <span style="color:#00aa55;font-size:9px">Y</span><span>${y_mm} mm</span>
      <span style="color:#00aa55;font-size:9px">DIST</span><span>${dist} mm</span>
      <span style="color:#00aa55;font-size:9px">BRG</span><span>${bearing.toFixed(1)}°</span>
    </div>`;
    renderRadar();
  }

  function clearTarget() {
    target = null; inputX.value = ""; inputY.value = "";
    navigateBtn.disabled = true;
    targetDisplay.innerHTML = `<span class="target-placeholder">Click radar to set target</span>`;
    renderRadar();
  }

  // ── Canvas events ─────────────────────────────────────────────────
  canvas.addEventListener("click", e => {
    const mm = pixelToMm(e.clientX, e.clientY);
    if (e.shiftKey) {
      recordedWaypoints.push(mm);
      waypointCount.textContent = recordedWaypoints.length;
      waypointCountTag.textContent = `${recordedWaypoints.length} waypoints`;
      emit("path_waypoint", { x_mm: mm.x_mm, y_mm: mm.y_mm });
      renderRadar();
    } else {
      setTarget(mm.x_mm, mm.y_mm);
    }
  });

  canvas.addEventListener("mousemove", e => { cursorMm = pixelToMm(e.clientX, e.clientY); });

  document.addEventListener("keydown", e => {
    if (e.key === "h" || e.key === "H") {
      emit("home_set", { x_mm: cursorMm.x_mm, y_mm: cursorMm.y_mm });
      showMapStatus(`Home set at (${cursorMm.x_mm}, ${cursorMm.y_mm}) mm`);
    }
  });

  // ── Zoom ──────────────────────────────────────────────────────────
  function updateRange(idx) {
    idx = Math.max(0, Math.min(RANGES.length - 1, idx));
    rangeMm = RANGES[idx];
    zoomLabel.textContent = "Range: " + (rangeMm / 1000).toFixed(1) + " m";
    renderRadar();
  }
  zoomInBtn.addEventListener("click",  () => updateRange(RANGES.indexOf(rangeMm) - 1));
  zoomOutBtn.addEventListener("click", () => updateRange(RANGES.indexOf(rangeMm) + 1));

  // ── Speed ─────────────────────────────────────────────────────────
  speedSlider.addEventListener("input", () => {
    speedVal = parseInt(speedSlider.value);
    speedDisplay.textContent = speedVal + "%";
  });

  // ── Detection / Auto toggle ───────────────────────────────────────
  btnDetection.addEventListener("click", () => {
    detectionEnabled = !detectionEnabled;
    btnDetection.textContent = `🔍 DETECTION: ${detectionEnabled ? "ON" : "OFF"}`;
    btnDetection.style.borderColor = detectionEnabled ? "var(--green)" : "";
    btnDetection.style.color = detectionEnabled ? "var(--green)" : "";
    emit("toggle_detection", { enabled: detectionEnabled });
  });

  btnAuto.addEventListener("click", () => {
    autoEnabled = !autoEnabled;
    btnAuto.textContent = `🤖 AUTO: ${autoEnabled ? "ON" : "OFF"}`;
    btnAuto.style.borderColor = autoEnabled ? "var(--green)" : "";
    btnAuto.style.color = autoEnabled ? "var(--green)" : "";
    emit("toggle_auto", { enabled: autoEnabled });
  });

  // ── Map buttons ───────────────────────────────────────────────────
  document.getElementById("btn-save-map").addEventListener("click", () => {
    const name = mapNameInput.value || "map_001";
    emit("map_save", { name });
    showMapStatus(`Saving: ${name}...`);
  });

  document.getElementById("btn-load-map").addEventListener("click", () => {
    const name = mapNameInput.value || "map_001";
    emit("map_load", { name });
    showMapStatus(`Loading: ${name}...`);
  });

  document.getElementById("btn-clear-map").addEventListener("click", () => {
    emit("map_clear", {});
    gridData = null;
    showMapStatus("Map cleared");
    renderRadar();
  });

  document.getElementById("btn-set-home").addEventListener("click", () => {
    const x = parseInt(inputX.value) || 0;
    const y = parseInt(inputY.value) || 0;
    emit("home_set", { x_mm: x, y_mm: y });
    showMapStatus(`Home set at (${x}, ${y}) mm`);
  });

  document.getElementById("btn-go-home").addEventListener("click", () => {
    emit("home_go", {});
    appendLog("system", "returning to home position");
  });

  // ── Path buttons ──────────────────────────────────────────────────
  document.getElementById("btn-save-path").addEventListener("click", () => {
    const name = pathNameInput.value || "path_001";
    emit("path_save", { name });
    showMapStatus(`Path saved: ${name}`);
  });

  document.getElementById("btn-run-path").addEventListener("click", () => {
    const name = pathNameInput.value || "path_001";
    emit("path_run", { name });
    appendLog("system", `running path: ${name}`);
  });

  document.getElementById("btn-clear-path").addEventListener("click", () => {
    recordedWaypoints = [];
    waypointCount.textContent = 0;
    waypointCountTag.textContent = "0 waypoints";
    emit("path_clear", {});
    renderRadar();
  });

  document.getElementById("btn-nav-stop").addEventListener("click", () => {
    emit("nav_stop", {});
    appendLog("warning", "navigation stopped");
  });

  // ── Navigate ──────────────────────────────────────────────────────
  navigateBtn.addEventListener("click", () => {
    if (!target) return;
    emit("navigate", { x_mm: target.x_mm, y_mm: target.y_mm, speed_pct: speedVal });
    appendLog("system", `nav target → x:${target.x_mm} y:${target.y_mm}`);
  });

  clearBtn.addEventListener("click", clearTarget);

  [inputX, inputY].forEach(inp => inp.addEventListener("input", () => {
    const x = parseInt(inputX.value), y = parseInt(inputY.value);
    if (Number.isFinite(x) && Number.isFinite(y)) setTarget(x, y);
  }));

  // ── Helper ────────────────────────────────────────────────────────
  function emit(event, data) {
    if (socket && socket.connected) socket.emit(event, data);
  }

  function showMapStatus(msg) {
    mapStatusTag.textContent = msg;
    setTimeout(() => { mapStatusTag.textContent = ""; }, 4000);
  }

  function appendLog(type, message) {
    const now = new Date().toISOString().split("T")[1].slice(0, 8);
    const item = document.createElement("div");
    item.className = "log-item";
    item.innerHTML = `<span class="log-time">[${now}]</span> <span class="log-message ${type}">${message.toUpperCase()}</span>`;
    logList.prepend(item);
    while (logList.children.length > 40) logList.removeChild(logList.lastChild);
  }

  function flashAlarm() {
    threatFlash.classList.add("active");
    setTimeout(() => threatFlash.classList.remove("active"), 700);
  }

  // ── WebRTC ────────────────────────────────────────────────────────
  async function startWebRTC(offer) {
    peerConnection = new RTCPeerConnection(webrtcConfig);
    peerConnection.ontrack = e => {
      remoteVideo.srcObject = e.streams[0];
      remoteVideo.style.display = "block";
      fallbackImg.style.display = "none";
      feedPlaceholder.style.display = "none";
      streamTypeTag.textContent = "STREAM: WEBRTC";
    };
    peerConnection.onicecandidate = e => {
      if (e.candidate) emit("webrtc_signal", { payload: { type: "candidate", candidate: e.candidate } });
    };
    await peerConnection.setRemoteDescription(new RTCSessionDescription(offer));
    const answer = await peerConnection.createAnswer();
    await peerConnection.setLocalDescription(answer);
    emit("webrtc_signal", { payload: { type: "answer", sdp: peerConnection.localDescription.sdp } });
  }

  function closeWebRTC() {
    if (peerConnection) { peerConnection.close(); peerConnection = null; }
    remoteVideo.srcObject = null; remoteVideo.style.display = "none";
    streamTypeTag.textContent = "STREAM: NONE";
  }

  // ── Drive ─────────────────────────────────────────────────────────
  function sendCommand(cmd) { emit("control", { cmd, speed: speedVal }); }

  document.querySelectorAll(".btn").forEach(btn => {
    const cmd = btn.dataset.cmd;
    if (!cmd) return;
    const press = e => {
      e.preventDefault(); btn.classList.add("pressed");
      if (cmd === "stop") { sendCommand("stop"); return; }
      sendCommand(cmd);
      clearInterval(teleopTimer);
      teleopTimer = setInterval(() => sendCommand(cmd), 120);
    };
    const release = e => {
      e.preventDefault(); btn.classList.remove("pressed");
      clearInterval(teleopTimer);
      if (cmd !== "stop") sendCommand("stop");
    };
    btn.addEventListener("mousedown", press);
    btn.addEventListener("touchstart", press, { passive: false });
    btn.addEventListener("mouseup", release);
    btn.addEventListener("mouseleave", release);
    btn.addEventListener("touchend", release);
  });

  const keyMap = {
    ArrowUp:"forward", w:"forward", W:"forward",
    ArrowDown:"backward", s:"backward", S:"backward",
    ArrowLeft:"left", a:"left", A:"left",
    ArrowRight:"right", d:"right", D:"right", " ":"stop"
  };
  const activeKeys = new Set();

  document.addEventListener("keydown", e => {
    const cmd = keyMap[e.key];
    if (!cmd || activeKeys.has(e.key)) return;
    if (document.activeElement.tagName === "INPUT") return;
    e.preventDefault(); activeKeys.add(e.key);
    document.querySelector(`.btn[data-cmd="${cmd}"]`)?.classList.add("pressed");
    sendCommand(cmd);
    clearInterval(teleopTimer);
    if (cmd !== "stop") teleopTimer = setInterval(() => sendCommand(cmd), 120);
  });

  document.addEventListener("keyup", e => {
    const cmd = keyMap[e.key];
    if (!cmd) return;
    e.preventDefault(); activeKeys.delete(e.key);
    document.querySelector(`.btn[data-cmd="${cmd}"]`)?.classList.remove("pressed");
    clearInterval(teleopTimer);
    if (cmd !== "stop") sendCommand("stop");
  });

  // ── Socket.IO connect ─────────────────────────────────────────────
  connectBtn.addEventListener("click", () => {
    if (socket && socket.connected) { socket.disconnect(); return; }
    const url = gatewayInput.value.trim();
    if (!url) { appendLog("warning", "invalid gateway URL"); return; }
    connectBtn.disabled = true; connectBtn.textContent = "UPLINKING...";
    appendLog("system", `connecting to ${url}...`);

    socket = io(url, { transports: ["websocket", "polling"], reconnectionAttempts: 5, timeout: 8000 });

    socket.on("connect", () => {
      appendLog("success", "uplink established with gateway");
      connectBtn.disabled = false; connectBtn.textContent = "DISCONNECT";
      connectBtn.classList.add("connected");
      socket.emit("register", "operator");
      pingStart = Date.now(); socket.emit("ping_req");
      pingInterval = setInterval(() => { pingStart = Date.now(); socket.emit("ping_req"); }, 3000);
    });

    socket.on("disconnect", () => {
      appendLog("warning", "uplink terminated");
      connectBtn.disabled = false; connectBtn.textContent = "CONNECT";
      connectBtn.classList.remove("connected");
      robotBadge.textContent = "DISCONNECTED"; robotBadge.className = "meta-value";
      pingDisplay.textContent = "-- ms";
      clearInterval(pingInterval); closeWebRTC();
      fallbackImg.style.display = "none"; feedPlaceholder.style.display = "flex";
      lidarPoints = []; renderRadar();
    });

    socket.on("connect_error", err => {
      appendLog("warning", `uplink failure: ${err.message}`);
      connectBtn.disabled = false; connectBtn.textContent = "CONNECT";
    });

    socket.on("pong_res", () => {
      pingDisplay.textContent = `${Date.now() - pingStart} ms`;
    });

    socket.on("robot_status", data => {
      if (data.online) {
        robotBadge.textContent = "ONLINE"; robotBadge.className = "meta-value online";
        appendLog("success", "SENTRY robot detected online");
      } else {
        robotBadge.textContent = "OFFLINE"; robotBadge.className = "meta-value";
        appendLog("warning", "SENTRY robot went offline");
        closeWebRTC(); fallbackImg.style.display = "none"; feedPlaceholder.style.display = "flex";
      }
    });

    socket.on("battery_update", data => {
      const pct = Math.round(data.level);
      batFill.style.width = `${pct}%`;
      batPct.textContent  = `${pct}%`;
      batFill.style.backgroundColor = pct > 40 ? "var(--green)" : pct > 20 ? "var(--amber)" : "var(--red)";
    });

    socket.on("status", data => {
      if (data.detection_enabled !== undefined) {
        detectionEnabled = data.detection_enabled;
        btnDetection.textContent = `🔍 DETECTION: ${detectionEnabled ? "ON" : "OFF"}`;
      }
    });

    socket.on("lidar_scan", data => {
      if (data.points) { lidarPoints = data.points; renderRadar(); }
    });

    socket.on("grid_snapshot", data => {
      gridData   = data.grid     || null;
      homeCell   = data.home     || null;
      activePath = data.path     || [];
      gridOrigin = data.origin   || 200;
      cellMm     = data.cell_mm  || 25;
      downsample = data.downsample || 2;
      pathSteps.textContent = activePath.length;
      renderRadar();
    });

    socket.on("path_status", data => {
      const colors = { started:"#00ffaa", arrived:"#00ffaa", replanning:"#ffaa00",
                       blocked:"#ff3366", stopped:"#ff3366", error:"#ff3366" };
      const color = colors[data.status] || "#005522";
      pathStatusDisplay.innerHTML = `<span style="color:${color}">● ${data.message}</span>`;
      appendLog(data.status === "arrived" ? "success" : "system", data.message);
    });

    socket.on("path_update", data => {
      activePath = data.path || [];
      pathSteps.textContent = activePath.length;
      renderRadar();
    });

    socket.on("waypoint_added", data => {
      waypointCount.textContent = data.index;
      waypointCountTag.textContent = `${data.index} waypoints`;
    });

    socket.on("map_status",      data => showMapStatus(data.message));
    socket.on("map_loaded",      data => { showMapStatus(`Map loaded: ${data.name}`); appendLog("success", `map loaded: ${data.name}`); });
    socket.on("home_set_confirm",data => { homeCell = data.home; showMapStatus("Home set"); renderRadar(); });

    socket.on("detection", data => {
      const entry = document.createElement("div");
      entry.style.cssText = "font-size:10px;padding:2px 0;border-bottom:1px solid #0d3318;";
      const threat = ["Knife","Axe","Chainsaw"].includes(data.label);
      entry.innerHTML = `<span style="color:${threat ? "#ff3333" : "#00ffaa"}">${threat ? "⚠ " : ""}${data.label}</span>
        <span style="color:#005522"> ${Math.round((data.confidence||0)*100)}%</span>`;
      detectionList.prepend(entry);
      while (detectionList.children.length > 8) detectionList.removeChild(detectionList.lastChild);
      if (threat) { flashAlarm(); appendLog("threat", `THREAT: ${data.label}`); }
      else appendLog("system", `detected: ${data.label}`);
    });

    socket.on("alert",    data => { appendLog("threat", `THREAT: ${data.message}`); flashAlarm(); });
    socket.on("obstacle", data => appendLog("warning", `obstacle ${data.distance}mm at ${data.angle}°`));

    socket.on("webrtc_signal", async data => {
      const p = data.payload;
      if (p.type === "offer") await startWebRTC(p);
      else if (p.type === "candidate" && peerConnection) {
        try { await peerConnection.addIceCandidate(new RTCIceCandidate(p.candidate)); } catch {}
      }
    });

    socket.on("video_frame", data => {
      if (peerConnection && peerConnection.connectionState === "connected") return;
      let url = "";
      if (data.jpeg) {
        url = `data:image/jpeg;base64,${data.jpeg}`;
      } else {
        const blob = new Blob([new Uint8Array(data)], { type: "image/jpeg" });
        if (fallbackImgBlobUrl) URL.revokeObjectURL(fallbackImgBlobUrl);
        fallbackImgBlobUrl = URL.createObjectURL(blob);
        url = fallbackImgBlobUrl;
      }
      streamTypeTag.textContent = "STREAM: WEBSOCKET (FALLBACK)";
      fallbackImg.src = url;
      fallbackImg.style.display = "block";
      remoteVideo.style.display = "none";
      feedPlaceholder.style.display = "none";
    });
  });

  // ── Clock ─────────────────────────────────────────────────────────
  setInterval(() => {
    clockVal.textContent = new Date().toISOString().split("T")[1].slice(0, 8);
  }, 1000);

  // ── Boot ──────────────────────────────────────────────────────────
  resizeCanvas();
  clearTarget();
  updateRange(RANGES.indexOf(rangeMm));
})();
