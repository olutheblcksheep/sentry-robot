"""
pathfinder.py — Occupancy grid + A* planner + map save/load + path recording.
==============================================================================

Key improvements over previous version:
  • CELL_MM reduced from 50 → 25mm for 2× finer map detail
  • GRID_SIZE increased to 400 (400×400 cells = 10m×10m at 25mm/cell)
  • MIN_SCAN_DIST filters out LiDAR returns closer than 150mm (robot body noise)
  • CONFIDENCE-BASED GRID — cells must be hit multiple times before being
    marked solid, and decay back to unknown if not re-confirmed. This
    filters single-scan noise without needing any external SLAM library.
  • Home position can be set and saved
  • Full map can be saved to JSON and reloaded
  • Waypoint paths can be recorded, saved, and replayed
  • All files saved to ~/sentry/maps/
"""

from __future__ import annotations

import math, heapq, threading, time, json, os
from typing import List, Tuple, Optional

import esp32
import state
from state import socketio

# ── Grid config ───────────────────────────────────────────────────────
GRID_SIZE     = 400          # 400×400 cells
CELL_MM       = 25           # 25mm per cell → finer detail
ORIGIN        = GRID_SIZE // 2
INFLATE_RADIUS = 1           # inflate obstacles by 1 cell for safety
MIN_SCAN_DIST  = 150         # ignore LiDAR returns closer than 150mm
STEP_DELAY     = 0.4         # seconds per cell (tune to robot speed)

# ── Confidence grid tuning ───────────────────────────────────────────
HIT_THRESHOLD   = 3      # cell must be hit this many times to be "solid"
HIT_INCREMENT   = 2      # confidence added per hit (obstacle return)
MISS_DECREMENT  = 1      # confidence removed per miss (ray passed through free)
CONFIDENCE_MAX  = 10     # cap so old obstacles can be cleared in reasonable time
CONFIDENCE_MIN  = 0

# ── Maps directory ────────────────────────────────────────────────────
MAPS_DIR = os.path.expanduser("~/sentry/maps")
os.makedirs(MAPS_DIR, exist_ok=True)

# ── Shared state ──────────────────────────────────────────────────────
# grid: final binary view derived from confidence (0=free, 1=obstacle)
# confidence: raw evidence counter per cell, drives the binary grid
grid: List[List[int]] = [[0] * GRID_SIZE for _ in range(GRID_SIZE)]
confidence: List[List[int]] = [[0] * GRID_SIZE for _ in range(GRID_SIZE)]
grid_lock = threading.Lock()

current_path: List[Tuple[int, int]] = []
path_lock    = threading.Lock()

robot_pos: List[int]  = [ORIGIN, ORIGIN]
robot_heading: float  = 0.0
home_pos: List[int]   = [ORIGIN, ORIGIN]   # set by user

_executor_running = False
_executor_lock    = threading.Lock()

# Recorded waypoints for path saving
_recorded_waypoints: List[dict] = []


# ═══════════════════════════════════════════════════════════════
#  OCCUPANCY GRID (confidence-based, noise-filtered)
# ═══════════════════════════════════════════════════════════════

def _world_to_cell(angle_deg: float, distance_mm: float) -> Tuple[int, int]:
    rad = math.radians(angle_deg)
    dx  =  distance_mm * math.sin(rad)
    dy  =  distance_mm * math.cos(rad)
    col = ORIGIN + int(dx / CELL_MM)
    row = ORIGIN - int(dy / CELL_MM)
    return row, col


def _register_hit(r: int, c: int) -> None:
    """Increase confidence a cell is an obstacle. Promote to solid past threshold."""
    if not (0 <= r < GRID_SIZE and 0 <= c < GRID_SIZE):
        return
    confidence[r][c] = min(CONFIDENCE_MAX, confidence[r][c] + HIT_INCREMENT)
    if confidence[r][c] >= HIT_THRESHOLD:
        grid[r][c] = 1


def _register_miss(r: int, c: int) -> None:
    """Decrease confidence for a cell a ray passed through (likely free)."""
    if not (0 <= r < GRID_SIZE and 0 <= c < GRID_SIZE):
        return
    confidence[r][c] = max(CONFIDENCE_MIN, confidence[r][c] - MISS_DECREMENT)
    if confidence[r][c] < HIT_THRESHOLD:
        grid[r][c] = 0


def _inflate(obs_row: int, obs_col: int) -> None:
    """Mark neighbour cells solid immediately (safety margin) without
    requiring them to individually pass the hit threshold."""
    for dr in range(-INFLATE_RADIUS, INFLATE_RADIUS + 1):
        for dc in range(-INFLATE_RADIUS, INFLATE_RADIUS + 1):
            r, c = obs_row + dr, obs_col + dc
            if 0 <= r < GRID_SIZE and 0 <= c < GRID_SIZE:
                grid[r][c] = 1


def update_grid_from_scan(scan: list) -> None:
    """
    Update the occupancy grid from a single LiDAR scan using a confidence
    model: cells along the ray accumulate "miss" evidence (likely free),
    the endpoint accumulates "hit" evidence (likely obstacle). A cell only
    becomes a hard obstacle once it crosses HIT_THRESHOLD — this means a
    single stray reflection or sensor glitch can't corrupt the map, but a
    real wall confirmed over a few scans solidifies quickly.
    """
    with grid_lock:
        for point in scan:
            dist  = point.get("distance", 0)
            angle = point.get("angle",    0)

            # Filter robot body / mount noise and obviously bad returns
            if dist < MIN_SCAN_DIST or dist > 8000:
                continue

            # Ray-cast: cells between robot and hit are evidence of free space
            steps = max(1, int(dist / CELL_MM) - 1)
            for i in range(steps):
                r, c = _world_to_cell(angle, i * CELL_MM)
                _register_miss(r, c)

            # Endpoint is evidence of an obstacle
            obs_r, obs_c = _world_to_cell(angle, dist)
            _register_hit(obs_r, obs_c)

            # Once a cell crosses the threshold, give it a safety margin
            if 0 <= obs_r < GRID_SIZE and 0 <= obs_c < GRID_SIZE \
                    and confidence[obs_r][obs_c] >= HIT_THRESHOLD:
                _inflate(obs_r, obs_c)


def clear_grid() -> None:
    """Reset the occupancy grid and confidence evidence to all free."""
    with grid_lock:
        for r in range(GRID_SIZE):
            for c in range(GRID_SIZE):
                grid[r][c] = 0
                confidence[r][c] = 0
    socketio.emit("map_cleared", {})


def get_grid_snapshot() -> dict:
    """Return downsampled grid for the UI (every 2nd cell → 200×200)."""
    step = 2
    with grid_lock:
        mini = [
            [grid[r][c] for c in range(0, GRID_SIZE, step)]
            for r in range(0, GRID_SIZE, step)
        ]
    with path_lock:
        path_snap = [list(p) for p in current_path]

    imu_heading = None
    imu_ok = False
    try:
        import imu as _imu
        imu_ok = _imu.imu_available
        imu_heading = round(_imu.heading_deg, 1) if imu_ok else None
    except ImportError:
        pass

    return {
        "grid":       mini,
        "robot":      list(robot_pos),
        "home":       list(home_pos),
        "path":       path_snap,
        "origin":     ORIGIN,
        "cell_mm":    CELL_MM,
        "grid_size":  GRID_SIZE,
        "downsample": step,
        "heading":    imu_heading if imu_heading is not None else round(robot_heading, 1),
        "imu_active": imu_ok,
    }


# ═══════════════════════════════════════════════════════════════
#  MAP SAVE / LOAD
# ═══════════════════════════════════════════════════════════════

def save_map(name: str = "map_001") -> str:
    """Save the current occupancy grid to ~/sentry/maps/<name>.json"""
    filepath = os.path.join(MAPS_DIR, f"{name}.json")
    with grid_lock:
        # Save only obstacle cells to keep file small
        obstacles = [
            [r, c]
            for r in range(GRID_SIZE)
            for c in range(GRID_SIZE)
            if grid[r][c] == 1
        ]
    data = {
        "grid_size": GRID_SIZE,
        "cell_mm":   CELL_MM,
        "origin":    ORIGIN,
        "home":      list(home_pos),
        "obstacles": obstacles,
        "saved_at":  time.strftime("%Y-%m-%d %H:%M:%S"),
    }
    with open(filepath, "w") as f:
        json.dump(data, f)
    print(f"[Map] Saved {len(obstacles)} obstacle cells → {filepath}")
    return filepath


def load_map(name: str = "map_001") -> bool:
    """Load a saved map from ~/sentry/maps/<name>.json"""
    global home_pos
    filepath = os.path.join(MAPS_DIR, f"{name}.json")
    if not os.path.exists(filepath):
        print(f"[Map] File not found: {filepath}")
        return False
    with open(filepath) as f:
        data = json.load(f)
    clear_grid()
    with grid_lock:
        for r, c in data.get("obstacles", []):
            if 0 <= r < GRID_SIZE and 0 <= c < GRID_SIZE:
                grid[r][c] = 1
                confidence[r][c] = HIT_THRESHOLD   # loaded obstacles start "confirmed"
    home_pos[:] = data.get("home", [ORIGIN, ORIGIN])
    print(f"[Map] Loaded {len(data['obstacles'])} obstacle cells from {filepath}")
    socketio.emit("map_loaded", {"name": name, "home": home_pos})
    return True


def list_maps() -> List[str]:
    """List all saved map files."""
    return [f.replace(".json", "") for f in os.listdir(MAPS_DIR)
            if f.endswith(".json") and not f.startswith("path_")]


# ═══════════════════════════════════════════════════════════════
#  HOME POSITION
# ═══════════════════════════════════════════════════════════════

def set_home(x_mm: float = 0, y_mm: float = 0) -> None:
    """
    Set home position in real-world mm from grid origin.
    Also resets the IMU heading reference to 0° here — this defines
    "north" on the map as whatever direction the robot is physically
    facing right now. Always set home with the robot pointed in a
    consistent, known direction (e.g. straight ahead from its charging
    dock) so the map stays correctly oriented across sessions.
    """
    global home_pos, robot_heading
    col = ORIGIN + int(x_mm / CELL_MM)
    row = ORIGIN - int(y_mm / CELL_MM)
    home_pos[:] = [
        max(0, min(GRID_SIZE - 1, row)),
        max(0, min(GRID_SIZE - 1, col)),
    ]
    robot_pos[:] = list(home_pos)
    robot_heading = 0.0

    try:
        import imu as _imu
        if _imu.imu_available:
            _imu.reset_heading(0.0)
    except ImportError:
        pass

    print(f"[Map] Home set to ({x_mm:.0f}, {y_mm:.0f}) mm → cell {home_pos}, heading reset to 0°")
    socketio.emit("home_set", {"home": home_pos, "x_mm": x_mm, "y_mm": y_mm})


def go_home() -> bool:
    """Navigate robot back to home position."""
    return navigate_to_cell(tuple(home_pos))


# ═══════════════════════════════════════════════════════════════
#  PATH SAVE / LOAD / RECORD
# ═══════════════════════════════════════════════════════════════

def record_waypoint(x_mm: float, y_mm: float) -> None:
    """Add a waypoint to the current recording."""
    _recorded_waypoints.append({"x_mm": x_mm, "y_mm": y_mm})
    print(f"[Path] Waypoint {len(_recorded_waypoints)} recorded: ({x_mm:.0f}, {y_mm:.0f}) mm")
    socketio.emit("waypoint_added", {
        "index": len(_recorded_waypoints),
        "x_mm": x_mm,
        "y_mm": y_mm,
    })


def clear_waypoints() -> None:
    _recorded_waypoints.clear()
    socketio.emit("waypoints_cleared", {})


def save_path(name: str = "path_001") -> str:
    """Save recorded waypoints to ~/sentry/maps/<name>.json"""
    filepath = os.path.join(MAPS_DIR, f"{name}.json")
    data = {
        "waypoints": list(_recorded_waypoints),
        "home":      list(home_pos),
        "saved_at":  time.strftime("%Y-%m-%d %H:%M:%S"),
    }
    with open(filepath, "w") as f:
        json.dump(data, f)
    print(f"[Path] Saved {len(_recorded_waypoints)} waypoints → {filepath}")
    return filepath


def load_and_run_path(name: str = "path_001") -> bool:
    """Load a saved path and execute it waypoint by waypoint."""
    filepath = os.path.join(MAPS_DIR, f"{name}.json")
    if not os.path.exists(filepath):
        print(f"[Path] File not found: {filepath}")
        return False
    with open(filepath) as f:
        data = json.load(f)
    waypoints = data.get("waypoints", [])
    if not waypoints:
        return False
    print(f"[Path] Running {len(waypoints)} waypoints from {filepath}")
    threading.Thread(
        target=_run_waypoints,
        args=(waypoints,),
        daemon=True,
    ).start()
    return True


def _run_waypoints(waypoints: List[dict]) -> None:
    """Execute a list of waypoints in order."""
    for i, wp in enumerate(waypoints):
        socketio.emit("path_status", {
            "status": "started",
            "message": f"Waypoint {i+1}/{len(waypoints)}: ({wp['x_mm']:.0f}, {wp['y_mm']:.0f}) mm",
        })
        ok = navigate_to(wp["x_mm"], wp["y_mm"])
        if not ok:
            socketio.emit("path_status", {
                "status": "blocked",
                "message": f"Waypoint {i+1} unreachable — skipping",
            })
            continue
        # Wait for executor to finish before moving to next waypoint
        while _executor_running:
            time.sleep(0.2)

    socketio.emit("path_status", {
        "status": "arrived",
        "message": "Path complete — all waypoints reached",
    })


def list_paths() -> List[str]:
    return [f.replace(".json", "") for f in os.listdir(MAPS_DIR)
            if f.startswith("path_") and f.endswith(".json")]


# ═══════════════════════════════════════════════════════════════
#  A* PATH PLANNER
# ═══════════════════════════════════════════════════════════════

def _heuristic(a: Tuple[int, int], b: Tuple[int, int]) -> float:
    dr = abs(a[0] - b[0])
    dc = abs(a[1] - b[1])
    return max(dr, dc) + (math.sqrt(2) - 1) * min(dr, dc)


def astar(start: Tuple[int, int], goal: Tuple[int, int]) -> List[Tuple[int, int]]:
    if start == goal:
        return []
    open_heap: list = []
    heapq.heappush(open_heap, (0.0, start))
    came_from: dict = {start: None}
    g: dict = {start: 0.0}
    neighbours = [
        (-1,0,1.0),(1,0,1.0),(0,-1,1.0),(0,1,1.0),
        (-1,-1,1.414),(-1,1,1.414),(1,-1,1.414),(1,1,1.414),
    ]
    with grid_lock:
        if (0 <= goal[0] < GRID_SIZE and 0 <= goal[1] < GRID_SIZE
                and grid[goal[0]][goal[1]] == 1):
            print("[Path] Goal cell is blocked")
            return []
    while open_heap:
        _, current = heapq.heappop(open_heap)
        if current == goal:
            path = []
            node = goal
            while node != start:
                path.append(node)
                node = came_from[node]
            path.reverse()
            return path
        cr, cc = current
        for dr, dc, cost in neighbours:
            nr, nc = cr+dr, cc+dc
            if not (0 <= nr < GRID_SIZE and 0 <= nc < GRID_SIZE):
                continue
            with grid_lock:
                if grid[nr][nc] == 1:
                    continue
            new_g = g[current] + cost
            nb = (nr, nc)
            if new_g < g.get(nb, float("inf")):
                came_from[nb] = current
                g[nb] = new_g
                heapq.heappush(open_heap, (new_g + _heuristic(nb, goal), nb))
    return []


# ═══════════════════════════════════════════════════════════════
#  PATH EXECUTOR  (IMU-corrected)
# ═══════════════════════════════════════════════════════════════

# Tuning for IMU-based turning
TURN_TOLERANCE_DEG  = 8     # how close to target heading before driving forward
TURN_CHECK_INTERVAL = 0.05  # seconds between heading checks while turning
MAX_TURN_TIME       = 3.0   # safety timeout per turn

try:
    import imu
    IMU_AVAILABLE = True
except ImportError:
    IMU_AVAILABLE = False
    print("[Pathfinder] imu.py not found — falling back to estimated heading")


def _bearing(from_cell, to_cell) -> float:
    """Compass bearing from one grid cell to another (0=north/forward)."""
    dr = to_cell[0] - from_cell[0]
    dc = to_cell[1] - from_cell[1]
    return math.degrees(math.atan2(dc, -dr)) % 360


def _get_current_heading() -> float:
    """Read real heading from the IMU if available, else use the
    last estimated heading (legacy behaviour)."""
    if IMU_AVAILABLE and imu.imu_available:
        return imu.heading_deg
    return robot_heading


def _heading_diff(target: float, current: float) -> float:
    """Shortest signed angle from current to target, range [-180, 180]."""
    diff = (target - current + 360) % 360
    if diff > 180:
        diff -= 360
    return diff


def _turn_to_heading(target_heading: float, speed: int = 50) -> bool:
    """
    Actively turn the robot until its real (IMU) heading matches the
    target within TURN_TOLERANCE_DEG. This replaces the old approach of
    guessing a single left/right pulse — now it continuously corrects
    using live gyro feedback, which is what actually fixes drift.

    Returns True if the turn completed, False if it timed out.
    """
    start_time = time.monotonic()

    while time.monotonic() - start_time < MAX_TURN_TIME:
        current = _get_current_heading()
        diff = _heading_diff(target_heading, current)

        if abs(diff) <= TURN_TOLERANCE_DEG:
            esp32.move("stop")
            return True

        # Turn in the direction that closes the gap fastest
        cmd = "right" if diff > 0 else "left"
        esp32.move(cmd, speed)
        time.sleep(TURN_CHECK_INTERVAL)

    esp32.move("stop")
    print(f"[Pathfinder] Turn timeout — target {target_heading:.1f}°, "
          f"reached {_get_current_heading():.1f}°")
    return False


def _drive_forward_one_cell(speed: int = 60) -> None:
    """Drive forward for STEP_DELAY seconds (one grid cell)."""
    esp32.move("forward", speed)
    time.sleep(STEP_DELAY)
    esp32.move("stop")


def execute_path(path: List[Tuple[int, int]]) -> None:
    """
    Follow a pre-planned path cell by cell using real IMU heading
    feedback for turning. Each step:
      1. Compute the bearing to the next cell.
      2. Turn in place until the IMU confirms we're facing that bearing.
      3. Drive forward one cell.
      4. Re-check for new obstacles and replan if blocked.
    """
    global current_path, robot_pos, robot_heading, _executor_running

    with path_lock:
        current_path = list(path)

    socketio.emit("path_update", {"path": [list(p) for p in path]})
    socketio.emit("path_status", {"status": "started", "message": f"{len(path)} steps"})

    i = 0
    while i < len(current_path):
        with _executor_lock:
            if not _executor_running:
                break
        if not state.auto_mode:
            break

        target_cell = current_path[i]
        with grid_lock:
            blocked = grid[target_cell[0]][target_cell[1]] == 1

        if blocked:
            goal = current_path[-1]
            socketio.emit("path_status", {"status": "replanning", "message": "Obstacle — replanning"})
            new_path = astar(tuple(robot_pos), goal)
            if not new_path:
                esp32.move("stop")
                socketio.emit("path_status", {"status": "blocked", "message": "No route found"})
                with _executor_lock:
                    _executor_running = False
                return
            with path_lock:
                current_path = new_path
            socketio.emit("path_update", {"path": [list(p) for p in current_path]})
            i = 0
            continue

        # ── IMU-corrected turn-then-drive ──────────────────────────
        target_bearing = _bearing(tuple(robot_pos), target_cell)
        turned_ok = _turn_to_heading(target_bearing, speed=50)

        if not turned_ok:
            socketio.emit("path_status", {
                "status": "replanning",
                "message": "Turn timeout — re-checking position",
            })
            # Don't abort — just proceed with best-effort heading.
            # A future scan will correct the grid if we drifted into something.

        _drive_forward_one_cell(speed=60)

        robot_pos[:] = list(target_cell)
        robot_heading = _get_current_heading()   # always sync to real IMU value
        i += 1

    esp32.move("stop")
    with _executor_lock:
        _executor_running = False

    msg = "Arrived" if i >= len(current_path) else "Stopped"
    socketio.emit("path_status", {"status": "arrived" if i >= len(current_path) else "stopped",
                                   "message": msg})
    with path_lock:
        current_path = []
    socketio.emit("path_update", {"path": []})


# ═══════════════════════════════════════════════════════════════
#  PUBLIC API
# ═══════════════════════════════════════════════════════════════

def navigate_to(x_mm: float, y_mm: float) -> bool:
    col  = ORIGIN + int(x_mm / CELL_MM)
    row  = ORIGIN - int(y_mm / CELL_MM)
    goal = (max(0, min(GRID_SIZE-1, row)), max(0, min(GRID_SIZE-1, col)))
    return navigate_to_cell(goal)


def navigate_to_cell(goal: Tuple[int, int]) -> bool:
    global _executor_running
    path = astar(tuple(robot_pos), goal)
    if not path:
        socketio.emit("path_status", {"status": "error", "message": "No path to target"})
        return False
    stop_navigation(wait=True)
    with _executor_lock:
        _executor_running = True
    threading.Thread(target=execute_path, args=(path,), daemon=True).start()
    return True


def stop_navigation(wait: bool = False) -> None:
    global _executor_running
    with _executor_lock:
        _executor_running = False
    esp32.move("stop")
    if wait:
        deadline = time.monotonic() + 1.0
        while _executor_running and time.monotonic() < deadline:
            time.sleep(0.05)
