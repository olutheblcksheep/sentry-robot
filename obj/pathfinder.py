"""
pathfinder.py — Occupancy grid + A* planner + map/path save/load.
==================================================================
  CELL_MM = 25mm per cell → fine detail
  GRID_SIZE = 400×400 cells = 10m×10m coverage
  MIN_SCAN_DIST = 150mm → filters robot body noise
"""

from __future__ import annotations

import math
import heapq
import threading
import time
import json
import os
from typing import List, Tuple

import esp32
import state
from state import socketio

# ── Grid config ───────────────────────────────────────────────────────
GRID_SIZE      = 400
CELL_MM        = 25
ORIGIN         = GRID_SIZE // 2
INFLATE_RADIUS = 1
MIN_SCAN_DIST  = 150
STEP_DELAY     = 0.4

# ── Maps directory ────────────────────────────────────────────────────
MAPS_DIR = os.path.expanduser("~/sentry/maps")
os.makedirs(MAPS_DIR, exist_ok=True)

# ── Shared state ──────────────────────────────────────────────────────
grid: List[List[int]]         = [[0] * GRID_SIZE for _ in range(GRID_SIZE)]
grid_lock                     = threading.Lock()
current_path: List[Tuple]     = []
path_lock                     = threading.Lock()
robot_pos: List[int]          = [ORIGIN, ORIGIN]
robot_heading: float          = 0.0
home_pos: List[int]           = [ORIGIN, ORIGIN]
_executor_running             = False
_executor_lock                = threading.Lock()
_recorded_waypoints: List[dict] = []


# ═══════════════════════════════════════════════════════════════
#  OCCUPANCY GRID
# ═══════════════════════════════════════════════════════════════

def _world_to_cell(angle_deg: float, distance_mm: float) -> Tuple[int, int]:
    rad = math.radians(angle_deg)
    col = ORIGIN + int(distance_mm * math.sin(rad) / CELL_MM)
    row = ORIGIN - int(distance_mm * math.cos(rad) / CELL_MM)
    return row, col


def _inflate(obs_r: int, obs_c: int) -> None:
    for dr in range(-INFLATE_RADIUS, INFLATE_RADIUS + 1):
        for dc in range(-INFLATE_RADIUS, INFLATE_RADIUS + 1):
            r, c = obs_r + dr, obs_c + dc
            if 0 <= r < GRID_SIZE and 0 <= c < GRID_SIZE:
                grid[r][c] = 1


def update_grid_from_scan(scan: list) -> None:
    """Feed a LiDAR scan into the occupancy grid."""
    with grid_lock:
        for point in scan:
            dist  = point.get("distance", 0)
            angle = point.get("angle", 0)
            if dist < MIN_SCAN_DIST or dist > 8000:
                continue
            steps = max(1, int(dist / CELL_MM) - 1)
            for i in range(steps):
                r, c = _world_to_cell(angle, i * CELL_MM)
                if 0 <= r < GRID_SIZE and 0 <= c < GRID_SIZE:
                    grid[r][c] = 0
            obs_r, obs_c = _world_to_cell(angle, dist)
            if 0 <= obs_r < GRID_SIZE and 0 <= obs_c < GRID_SIZE:
                _inflate(obs_r, obs_c)


def clear_grid() -> None:
    with grid_lock:
        for r in range(GRID_SIZE):
            for c in range(GRID_SIZE):
                grid[r][c] = 0
    socketio.emit("map_cleared", {})


def get_grid_snapshot() -> dict:
    """Return downsampled grid for the UI."""
    step = 2
    with grid_lock:
        mini = [
            [grid[r][c] for c in range(0, GRID_SIZE, step)]
            for r in range(0, GRID_SIZE, step)
        ]
    with path_lock:
        path_snap = [list(p) for p in current_path]
    return {
        "grid":       mini,
        "robot":      list(robot_pos),
        "home":       list(home_pos),
        "path":       path_snap,
        "origin":     ORIGIN,
        "cell_mm":    CELL_MM,
        "grid_size":  GRID_SIZE,
        "downsample": step,
    }


# ═══════════════════════════════════════════════════════════════
#  MAP SAVE / LOAD
# ═══════════════════════════════════════════════════════════════

def save_map(name: str = "map_001") -> str:
    fp = os.path.join(MAPS_DIR, f"{name}.json")
    with grid_lock:
        obstacles = [[r, c] for r in range(GRID_SIZE)
                     for c in range(GRID_SIZE) if grid[r][c] == 1]
    with open(fp, "w") as f:
        json.dump({
            "grid_size": GRID_SIZE, "cell_mm": CELL_MM,
            "origin": ORIGIN, "home": list(home_pos),
            "obstacles": obstacles,
            "saved_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        }, f)
    print(f"[Map] Saved {len(obstacles)} cells → {fp}")
    socketio.emit("map_status", {"message": f"Map saved: {name}"})
    return fp


def load_map(name: str = "map_001") -> bool:
    global home_pos
    fp = os.path.join(MAPS_DIR, f"{name}.json")
    if not os.path.exists(fp):
        return False
    with open(fp) as f:
        data = json.load(f)
    clear_grid()
    with grid_lock:
        for r, c in data.get("obstacles", []):
            if 0 <= r < GRID_SIZE and 0 <= c < GRID_SIZE:
                grid[r][c] = 1
    home_pos[:] = data.get("home", [ORIGIN, ORIGIN])
    socketio.emit("map_loaded", {"name": name, "home": home_pos})
    return True


def list_maps() -> List[str]:
    return [f.replace(".json", "") for f in os.listdir(MAPS_DIR)
            if f.endswith(".json") and not f.startswith("path_")]


# ═══════════════════════════════════════════════════════════════
#  HOME POSITION
# ═══════════════════════════════════════════════════════════════

def set_home(x_mm: float = 0, y_mm: float = 0) -> None:
    global home_pos
    col = ORIGIN + int(x_mm / CELL_MM)
    row = ORIGIN - int(y_mm / CELL_MM)
    home_pos[:] = [
        max(0, min(GRID_SIZE - 1, row)),
        max(0, min(GRID_SIZE - 1, col)),
    ]
    robot_pos[:] = list(home_pos)
    socketio.emit("home_set", {"home": home_pos, "x_mm": x_mm, "y_mm": y_mm})


def go_home() -> bool:
    return navigate_to_cell(tuple(home_pos))


# ═══════════════════════════════════════════════════════════════
#  PATH RECORDING / SAVE / LOAD
# ═══════════════════════════════════════════════════════════════

def record_waypoint(x_mm: float, y_mm: float) -> None:
    _recorded_waypoints.append({"x_mm": x_mm, "y_mm": y_mm})
    socketio.emit("waypoint_added", {
        "index": len(_recorded_waypoints),
        "x_mm": x_mm, "y_mm": y_mm,
    })


def clear_waypoints() -> None:
    _recorded_waypoints.clear()
    socketio.emit("waypoints_cleared", {})


def save_path(name: str = "path_001") -> str:
    fp = os.path.join(MAPS_DIR, f"{name}.json")
    with open(fp, "w") as f:
        json.dump({
            "waypoints": list(_recorded_waypoints),
            "home": list(home_pos),
            "saved_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        }, f)
    socketio.emit("map_status", {"message": f"Path saved: {name}"})
    return fp


def load_and_run_path(name: str = "path_001") -> bool:
    fp = os.path.join(MAPS_DIR, f"{name}.json")
    if not os.path.exists(fp):
        return False
    with open(fp) as f:
        data = json.load(f)
    waypoints = data.get("waypoints", [])
    if not waypoints:
        return False
    threading.Thread(target=_run_waypoints, args=(waypoints,), daemon=True).start()
    return True


def _run_waypoints(waypoints: List[dict]) -> None:
    for i, wp in enumerate(waypoints):
        socketio.emit("path_status", {
            "status": "started",
            "message": f"Waypoint {i+1}/{len(waypoints)}: ({wp['x_mm']:.0f}, {wp['y_mm']:.0f}) mm",
        })
        ok = navigate_to(wp["x_mm"], wp["y_mm"])
        if not ok:
            continue
        while _executor_running:
            time.sleep(0.2)
    socketio.emit("path_status", {"status": "arrived", "message": "Path complete"})


def list_paths() -> List[str]:
    return [f.replace(".json", "") for f in os.listdir(MAPS_DIR)
            if f.startswith("path_") and f.endswith(".json")]


# ═══════════════════════════════════════════════════════════════
#  A* PATHFINDER
# ═══════════════════════════════════════════════════════════════

def _heuristic(a: Tuple, b: Tuple) -> float:
    dr, dc = abs(a[0] - b[0]), abs(a[1] - b[1])
    return max(dr, dc) + (math.sqrt(2) - 1) * min(dr, dc)


def astar(start: Tuple, goal: Tuple) -> List[Tuple]:
    if start == goal:
        return []
    with grid_lock:
        if (0 <= goal[0] < GRID_SIZE and 0 <= goal[1] < GRID_SIZE
                and grid[goal[0]][goal[1]] == 1):
            return []
    heap = [(0.0, start)]
    came_from = {start: None}
    g = {start: 0.0}
    neighbours = [
        (-1, 0, 1.0), (1, 0, 1.0), (0, -1, 1.0), (0, 1, 1.0),
        (-1, -1, 1.414), (-1, 1, 1.414), (1, -1, 1.414), (1, 1, 1.414),
    ]
    while heap:
        _, cur = heapq.heappop(heap)
        if cur == goal:
            path, node = [], goal
            while node != start:
                path.append(node)
                node = came_from[node]
            return list(reversed(path))
        cr, cc = cur
        for dr, dc, cost in neighbours:
            nr, nc = cr + dr, cc + dc
            if not (0 <= nr < GRID_SIZE and 0 <= nc < GRID_SIZE):
                continue
            with grid_lock:
                if grid[nr][nc] == 1:
                    continue
            ng = g[cur] + cost
            nb = (nr, nc)
            if ng < g.get(nb, float("inf")):
                came_from[nb] = cur
                g[nb] = ng
                heapq.heappush(heap, (ng + _heuristic(nb, goal), nb))
    return []


# ═══════════════════════════════════════════════════════════════
#  PATH EXECUTOR
# ═══════════════════════════════════════════════════════════════

def _bearing(a: Tuple, b: Tuple) -> float:
    return math.degrees(math.atan2(b[1] - a[1], -(b[0] - a[0]))) % 360


def _cmd(target_hdg: float, cur_hdg: float) -> str:
    diff = (target_hdg - cur_hdg + 360) % 360
    if diff > 180:
        diff -= 360
    if abs(diff) <= 20:
        return "forward"
    return "right" if diff > 0 else "left"


def execute_path(path: List[Tuple]) -> None:
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
        tc = current_path[i]
        with grid_lock:
            blocked = grid[tc[0]][tc[1]] == 1
        if blocked:
            goal = current_path[-1]
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
        b = _bearing(tuple(robot_pos), tc)
        esp32.move(_cmd(b, robot_heading), 60)
        time.sleep(STEP_DELAY)
        robot_pos[:] = list(tc)
        robot_heading = b
        i += 1

    esp32.move("stop")
    with _executor_lock:
        _executor_running = False
    status = "arrived" if i >= len(current_path) else "stopped"
    socketio.emit("path_status", {"status": status, "message": "Done"})
    with path_lock:
        current_path = []
    socketio.emit("path_update", {"path": []})


# ═══════════════════════════════════════════════════════════════
#  PUBLIC API
# ═══════════════════════════════════════════════════════════════

def navigate_to(x_mm: float, y_mm: float) -> bool:
    col = ORIGIN + int(x_mm / CELL_MM)
    row = ORIGIN - int(y_mm / CELL_MM)
    return navigate_to_cell((
        max(0, min(GRID_SIZE - 1, row)),
        max(0, min(GRID_SIZE - 1, col)),
    ))


def navigate_to_cell(goal: Tuple) -> bool:
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
