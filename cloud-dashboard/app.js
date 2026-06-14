/* ═══════════════════════════════════════════════════════════════════
   app.js — Sentry Cloud Dashboard front-end Controller
   ═══════════════════════════════════════════════════════════════════ */

(() => {
    "use strict";

    // ── DOM Handles ──────────────────────────────────────────────────
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

    // ── Application State ────────────────────────────────────────────
    let socket = null;
    let peerConnection = null;
    let localStream = null;
    let rangeMm = 4000;
    const RANGES = [1000, 2000, 4000, 6000, 8000, 12000];
    
    let target = null; // {x_mm, y_mm}
    let lidarPoints = [];
    let speedVal = 80;
    let pingInterval = null;
    let pingStart = 0;
    let fallbackImgBlobUrl = null;

    // WebRTC Peer Connection Configuration (STUN only)
    const webrtcConfig = {
        iceServers: [{ urls: "stun:stun.l.google.com:19302" }]
    };

    // ── Canvas Sizing & Geom ─────────────────────────────────────────
    function resizeCanvas() {
        const rect = container.getBoundingClientRect();
        const dpr = window.devicePixelRatio || 1;
        canvas.width = Math.round(rect.width * dpr);
        canvas.height = Math.round(rect.height * dpr);
        ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
        renderRadar();
    }
    
    window.addEventListener("resize", resizeCanvas);
    
    function getGeom() {
        const rect = container.getBoundingClientRect();
        const w = rect.width;
        const h = rect.height;
        const cx = w / 2;
        const cy = h / 2;
        const maxR = Math.min(cx, cy) - 10;
        return { w, h, cx, cy, maxR, scale: maxR / rangeMm };
    }

    // ── Render Radar ─────────────────────────────────────────────────
    function renderRadar() {
        const { w, h, cx, cy, maxR, scale } = getGeom();
        ctx.clearRect(0, 0, w, h);

        // Grid Concentric Rings
        ctx.lineWidth = 1;
        const rings = 4;
        for (let i = 1; i <= rings; i++) {
            const rr = (maxR * i) / rings;
            ctx.strokeStyle = "rgba(0, 255, 136, 0.12)";
            ctx.beginPath();
            ctx.arc(cx, cy, rr, 0, Math.PI * 2);
            ctx.stroke();

            // Ring Label
            const labelMm = (rangeMm * i) / rings;
            ctx.fillStyle = "rgba(0, 200, 100, 0.7)";
            ctx.font = "10px 'Share Tech Mono', monospace";
            ctx.fillText((labelMm / 1000).toFixed(1) + "m", cx + 4, cy - rr + 12);
        }

        // Spokes every 30 degrees
        ctx.strokeStyle = "rgba(0, 255, 136, 0.05)";
        for (let angle = 0; angle < 360; angle += 30) {
            const rad = (angle * Math.PI) / 180;
            ctx.beginPath();
            ctx.moveTo(cx, cy);
            ctx.lineTo(cx + Math.cos(rad) * maxR, cy - Math.sin(rad) * maxR);
            ctx.stroke();
        }

        // Render LiDAR Points
        lidarPoints.forEach((pt) => {
            if (!pt.distance) return;
            
            // Convert polar to cartesian
            // Note: Angle convention: 0 is Right (+X), 90 is Up (+Y)
            const rad = (pt.angle * Math.PI) / 180;
            const x = cx + Math.cos(rad) * pt.distance * scale;
            const y = cy - Math.sin(rad) * pt.distance * scale;
            
            const close = pt.distance < 500;
            ctx.fillStyle = close ? "#ff3333" : "#00ff88";
            ctx.shadowColor = close ? "#ff3333" : "#00ff88";
            ctx.shadowBlur = close ? 6 : 2;
            
            ctx.beginPath();
            ctx.arc(x, y, close ? 2.5 : 1.5, 0, Math.PI * 2);
            ctx.fill();
        });
        ctx.shadowBlur = 0; // reset

        // Target Marker
        if (target) {
            const tx = cx + target.x_mm * scale;
            const ty = cy - target.y_mm * scale;
            
            ctx.strokeStyle = "#ff3366";
            ctx.lineWidth = 1.5;
            
            ctx.beginPath();
            ctx.arc(tx, ty, 8, 0, Math.PI * 2);
            ctx.stroke();
            
            ctx.beginPath();
            ctx.moveTo(tx - 12, ty); ctx.lineTo(tx + 12, ty);
            ctx.moveTo(tx, ty - 12); ctx.lineTo(tx, ty + 12);
            ctx.stroke();
            
            // Dash path from robot center to target
            ctx.strokeStyle = "rgba(255, 51, 102, 0.4)";
            ctx.setLineDash([4, 4]);
            ctx.beginPath();
            ctx.moveTo(cx, cy);
            ctx.lineTo(tx, ty);
            ctx.stroke();
            ctx.setLineDash([]);
        }
    }

    // ── Pixel ↔ mm helpers ──────────────────────────────────────────
    function pixelToMm(clientX, clientY) {
        const rect = canvas.getBoundingClientRect();
        const { cx, cy, scale } = getGeom();
        const px = clientX - rect.left;
        const py = clientY - rect.top;
        
        return {
            x_mm: Math.round((px - cx) / scale / 10) * 10, // snap to 10mm
            y_mm: Math.round(-(py - cy) / scale / 10) * 10
        };
    }

    // ── Target management ────────────────────────────────────────────
    function setTarget(x_mm, y_mm) {
        target = { x_mm, y_mm };
        inputX.value = x_mm;
        inputY.value = y_mm;
        navigateBtn.disabled = false;

        const dist = Math.round(Math.hypot(x_mm, y_mm));
        const bearing = ((Math.atan2(y_mm, x_mm) * 180) / Math.PI + 360) % 360;
        
        targetDisplay.innerHTML = `
            <div class="target-info">
                <div><span class="coord-lbl">X</span> <span class="coord-val">${x_mm} mm</span></div>
                <div><span class="coord-lbl">Y</span> <span class="coord-val">${y_mm} mm</span></div>
                <div><span class="coord-lbl">DIST</span> <span class="coord-val">${dist} mm</span></div>
                <div><span class="coord-lbl">BRG</span> <span class="coord-val">${bearing.toFixed(1)}°</span></div>
            </div>`;
        
        renderRadar();
    }

    function clearTarget() {
        target = null;
        inputX.value = "";
        inputY.value = "";
        navigateBtn.disabled = true;
        targetDisplay.innerHTML = `<span class="target-placeholder">Select coordinate on radar</span>`;
        renderRadar();
    }

    // Canvas click listener to place coordinate target
    canvas.addEventListener("click", (e) => {
        const { x_mm, y_mm } = pixelToMm(e.clientX, e.clientY);
        setTarget(x_mm, y_mm);
    });

    [inputX, inputY].forEach(inp => {
        inp.addEventListener("input", () => {
            const x = parseInt(inputX.value, 10);
            const y = parseInt(inputY.value, 10);
            if (Number.isFinite(x) && Number.isFinite(y)) {
                setTarget(x, y);
            }
        });
    });

    clearBtn.addEventListener("click", clearTarget);

    // Zoom Controls
    function updateRange(index) {
        index = Math.max(0, Math.min(RANGES.length - 1, index));
        rangeMm = RANGES[index];
        zoomLabel.textContent = `Range: ${(rangeMm / 1000).toFixed(1)} m`;
        renderRadar();
    }
    
    zoomInBtn.addEventListener("click", () => updateRange(RANGES.indexOf(rangeMm) - 1));
    zoomOutBtn.addEventListener("click", () => updateRange(RANGES.indexOf(rangeMm) + 1));

    // Speed Slider
    speedSlider.addEventListener("input", () => {
        speedVal = parseInt(speedSlider.value, 10);
        speedDisplay.textContent = `${speedVal}%`;
    });

    // ── Tactical Event Log ──────────────────────────────────────────
    function appendLog(type, msg) {
        const item = document.createElement("div");
        item.className = "log-item";
        
        const timestamp = new Date().toISOString().split("T")[1].slice(0, 8);
        const typeClass = ["system", "success", "warning", "threat"].includes(type) ? type : "system";
        
        item.innerHTML = `<span class="log-time">[${timestamp}]</span> <span class="log-message ${typeClass}">${msg.toUpperCase()}</span>`;
        logList.prepend(item);
        
        // Cap lines at 50
        while (logList.children.length > 50) {
            logList.removeChild(logList.lastChild);
        }
    }

    function flashAlarm() {
        threatFlash.classList.remove("active");
        void threatFlash.offsetWidth; // trigger reflow
        threatFlash.classList.add("active");
    }

    // ── WebRTC Signaling & Operations ────────────────────────────────
    async function startWebRTC(signalingPayload) {
        try {
            if (peerConnection) {
                peerConnection.close();
            }
            
            peerConnection = new RTCPeerConnection(webrtcConfig);
            appendLog("system", "initializing WebRTC connection...");

            // Handle incoming ICE candidates
            peerConnection.onicecandidate = (event) => {
                if (event.candidate && socket && socket.connected) {
                    socket.emit("webrtc_signal", {
                        recipient: "robot",
                        payload: {
                            type: "candidate",
                            candidate: event.candidate
                        }
                    });
                }
            };

            // Bind tracks to video element
            peerConnection.ontrack = (event) => {
                appendLog("success", "WebRTC video stream active");
                streamTypeTag.textContent = "STREAM: WEBRTC (LOW LATENCY)";
                remoteVideo.srcObject = event.streams[0];
                remoteVideo.style.display = "block";
                fallbackImg.style.display = "none";
                feedPlaceholder.style.display = "none";
            };

            // Process the received offer
            await peerConnection.setRemoteDescription(new RTCSessionDescription(signalingPayload));
            const answer = await peerConnection.createAnswer();
            await peerConnection.setLocalDescription(answer);

            // Send answer back to SENTRY
            socket.emit("webrtc_signal", {
                recipient: "robot",
                payload: {
                    type: "answer",
                    sdp: answer.sdp
                }
            });

        } catch (err) {
            appendLog("warning", `WebRTC establishment failed: ${err.message}`);
        }
    }

    function closeWebRTC() {
        if (peerConnection) {
            peerConnection.close();
            peerConnection = null;
        }
        remoteVideo.srcObject = null;
        remoteVideo.style.display = "none";
        streamTypeTag.textContent = "STREAM: NONE";
    }

    // ── Socket.IO Gateway Client ─────────────────────────────────────
    connectBtn.addEventListener("click", () => {
        if (socket && socket.connected) {
            socket.disconnect();
            return;
        }

        const gatewayUrl = gatewayInput.value.trim();
        if (!gatewayUrl) {
            appendLog("warning", "invalid gateway URL");
            return;
        }

        connectBtn.disabled = true;
        connectBtn.textContent = "UPLINKING...";
        appendLog("system", `connecting to ${gatewayUrl}...`);

        socket = io(gatewayUrl, {
            transports: ["websocket"],
            reconnectionAttempts: 5,
            timeout: 5000
        });

        socket.on("connect", () => {
            appendLog("success", "uplink established with gateway");
            connectBtn.disabled = false;
            connectBtn.textContent = "DISCONNECT";
            connectBtn.classList.add("connected");
            
            // Register as operator client
            socket.emit("register", "operator");
            
            // Start Ping metrics
            pingStart = Date.now();
            socket.emit("ping_req");
            pingInterval = setInterval(() => {
                pingStart = Date.now();
                socket.emit("ping_req");
            }, 3000);
        });

        socket.on("disconnect", () => {
            appendLog("warning", "uplink terminated");
            connectBtn.disabled = false;
            connectBtn.textContent = "CONNECT";
            connectBtn.classList.remove("connected");
            robotBadge.textContent = "DISCONNECTED";
            robotBadge.className = "meta-value";
            pingDisplay.textContent = "-- ms";
            
            clearInterval(pingInterval);
            closeWebRTC();
            
            // Reset placeholders
            fallbackImg.style.display = "none";
            feedPlaceholder.style.display = "flex";
            lidarPoints = [];
            renderRadar();
        });

        socket.on("connect_error", (err) => {
            appendLog("warning", `uplink failure: ${err.message}`);
            connectBtn.disabled = false;
            connectBtn.textContent = "CONNECT";
        });

        // Pong response for latency diagnostics
        socket.on("pong_res", () => {
            const diff = Date.now() - pingStart;
            pingDisplay.textContent = `${diff} ms`;
        });

        // Robot presence tracking
        socket.on("robot_status", (data) => {
            if (data.online) {
                robotBadge.textContent = "ONLINE";
                robotBadge.className = "meta-value online";
                appendLog("success", "SENTRY robot detected online");
            } else {
                robotBadge.textContent = "OFFLINE";
                robotBadge.className = "meta-value";
                appendLog("warning", "SENTRY robot went offline");
                closeWebRTC();
                fallbackImg.style.display = "none";
                feedPlaceholder.style.display = "flex";
            }
        });

        // WebRTC Signaling Relay Handler
        socket.on("webrtc_signal", async (data) => {
            const payload = data.payload;
            if (payload.type === "offer") {
                await startWebRTC(payload);
            } else if (payload.type === "candidate") {
                if (peerConnection) {
                    try {
                        await peerConnection.addIceCandidate(new RTCIceCandidate(payload.candidate));
                    } catch (e) {
                        logger.error("Error adding ice candidate", e);
                    }
                }
            }
        });

        // --- Telemetry Handlers ---
        socket.on("battery_update", (data) => {
            const pct = Math.round(data.level);
            batFill.style.width = `${pct}%`;
            batPct.textContent = `${pct}%`;
            
            // Color updates based on levels
            if (pct > 40) {
                batFill.style.backgroundColor = "var(--green)";
            } else if (pct > 20) {
                batFill.style.backgroundColor = "var(--amber)";
            } else {
                batFill.style.backgroundColor = "var(--red)";
            }
        });

        socket.on("status", (data) => {
            // General robot diagnostics
            if (data.auto_mode) {
                appendLog("system", "autonomous navigation enabled on unit");
            }
        });

        socket.on("lidar_scan", (data) => {
            if (data.points) {
                lidarPoints = data.points;
                renderRadar();
            }
        });

        socket.on("detection", (data) => {
            appendLog("system", `detected object: ${data.label} (${Math.round(data.confidence * 100)}%)`);
        });

        socket.on("alert", (data) => {
            appendLog("threat", `THREAT RADIAL ALERT: ${data.message}`);
            flashAlarm();
        });

        socket.on("obstacle", (data) => {
            appendLog("warning", `reactive stop: obstacle ${data.distance}mm at ${data.angle}°`);
        });

        // Fallback WebSocket JPEG streaming
        socket.on("video_frame", (data) => {
	    feedPlaceholder.style.display = "none";
	    fallbackImg.style.display = "block";

            // If WebRTC is active, skip JPEG frames to conserve performance
            if (peerConnection && peerConnection.connectionState === "connected") {
                return;
            }
            
            // data contains raw JPEG bytes (emitted as binary/buffer)
            // or base64. Let's support both.
            let url = "";
            if (data.jpeg) {
                // assume base64
                url = `data:image/jpeg;base64,${data.jpeg}`;
            } else {
                // assume binary arraybuffer
                const blob = new Blob([new Uint8Array(data)], { type: "image/jpeg" }); 

                if (fallbackImgBlobUrl) {
                    URL.revokeObjectURL(fallbackImgBlobUrl);
                }
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

    // ── Command Transmissions ────────────────────────────────────────
    function sendCommand(cmd, speed = speedVal) {
        if (socket && socket.connected) {
            socket.emit("control", { cmd, speed });
        }
    }

    navigateBtn.addEventListener("click", () => {
        if (target && socket && socket.connected) {
            socket.emit("navigate", {
                x_mm: target.x_mm,
                y_mm: target.y_mm,
                speed_pct: speedVal
            });
            appendLog("system", `sent navigation target x:${target.x_mm} y:${target.y_mm}`);
        }
    });

    // ── D-pad Button Bindings (Hold-to-move) ─────────────────────────
    let teleopTimer = null;

    document.querySelectorAll(".btn").forEach(btn => {
        const cmd = btn.dataset.cmd;
        if (!cmd) return;
        
        function press(e) {
            e.preventDefault();
            btn.classList.add("pressed");
            if (cmd === "stop") {
                sendCommand("stop");
                return;
            }
            sendCommand(cmd);
            clearInterval(teleopTimer);
            teleopTimer = setInterval(() => sendCommand(cmd), 120);
        }

        function release(e) {
            e.preventDefault();
            btn.classList.remove("pressed");
            clearInterval(teleopTimer);
            if (cmd !== "stop") {
                sendCommand("stop");
            }
        }

        btn.addEventListener("mousedown", press);
        btn.addEventListener("touchstart", press, { passive: false });
        
        btn.addEventListener("mouseup", release);
        btn.addEventListener("mouseleave", release);
        btn.addEventListener("touchend", release);
    });

    // ── Keyboard Bindings ────────────────────────────────────────────
    const keyMap = {
        ArrowUp: "forward", w: "forward", W: "forward",
        ArrowDown: "backward", s: "backward", S: "backward",
        ArrowLeft: "left", a: "left", A: "left",
        ArrowRight: "right", d: "right", D: "right",
        " ": "stop"
    };
    
    const activeKeys = new Set();

    document.addEventListener("keydown", (e) => {
        const cmd = keyMap[e.key];
        if (!cmd || activeKeys.has(e.key)) return;
        
        // Don't intercept key inputs inside number textboxes
        if (document.activeElement.tagName === "INPUT" && document.activeElement.type === "number") {
            return;
        }

        e.preventDefault();
        activeKeys.add(e.key);
        
        // Trigger visual button press
        const btnElement = document.querySelector(`.btn[data-cmd="${cmd}"]`);
        if (btnElement) btnElement.classList.add("pressed");
        
        sendCommand(cmd);
        clearInterval(teleopTimer);
        if (cmd !== "stop") {
            teleopTimer = setInterval(() => sendCommand(cmd), 120);
        }
    });

    document.addEventListener("keyup", (e) => {
        const cmd = keyMap[e.key];
        if (!cmd) return;

        e.preventDefault();
        activeKeys.delete(e.key);
        
        const btnElement = document.querySelector(`.btn[data-cmd="${cmd}"]`);
        if (btnElement) btnElement.classList.remove("pressed");
        
        clearInterval(teleopTimer);
        if (cmd !== "stop") {
            sendCommand("stop");
        }
    });

    // Clock display
    setInterval(() => {
        const now = new Date();
        clockVal.textContent = now.toISOString().split("T")[1].slice(0, 8);
    }, 1000);

    // Initial canvas setup
    resizeCanvas();
    clearTarget();
    updateRange(RANGES.indexOf(rangeMm));
})();
