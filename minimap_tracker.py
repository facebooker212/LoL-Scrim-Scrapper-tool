"""
minimap_tracker.py
──────────────────
Tracks champion positions by scanning the League of Legends minimap in real time
using screen capture and OpenCV template matching.

STANDALONE usage:
    python minimap_tracker.py --folder scrim_2024-01-15_18-30

    If --folder is omitted a timestamped folder is created automatically.

INTEGRATED usage (called from main.py):
    from minimap_tracker import MinimapTracker
    tracker = MinimapTracker(folder="scrim_2024-01-15_18-30", champions=["Syndra", ...])
    tracker.start()   # non-blocking, runs in background thread
    tracker.stop()    # call when game ends

CALIBRATION:
    python minimap_tracker.py --calibrate
    Click the TOP-LEFT corner of your minimap, then the BOTTOM-RIGHT corner.
    Coordinates are saved to minimap_config.json and reused on future runs.

OUTPUT:
    <folder>/minimap_positions.csv
    Columns: timestamp, champion, team, pixel_x, pixel_y, map_x, map_y

DEPENDENCIES:
    pip install opencv-python mss numpy

RESOLUTION SUPPORT:
    1080p (1920x1080) — primary, tested
    1440p (2560x1440) — supported via config scaling, set resolution in minimap_config.json
"""

import cv2
import numpy as np
import mss
import json
import os
import csv
import time
import threading
import argparse
import urllib.request
from datetime import datetime, UTC
from pathlib import Path


# ── Constants ─────────────────────────────────────────────────────────────────

CONFIG_FILE = "minimap_config.json"
CAPTURE_INTERVAL = 2.0       # seconds between minimap scans
CHAMPION_ICON_SIZE = 24      # pixels — minimap icon size at 1080p default scale
MIN_CONTOUR_AREA = 40        # filter out noise smaller than this
MAX_CONTOUR_AREA = 900       # filter out large blobs that aren't champion icons

# Data Dragon base URL for downloading champion portrait crops
# Icons are fetched once per session and cached locally in champion_icons/
DDG_VERSION_URL = "https://ddragon.leagueoflegends.com/api/versions.json"
DDG_ICON_URL    = "https://ddragon.leagueoflegends.com/cdn/{version}/img/champion/{name}.png"

# HSV color ranges for champion icon borders
# ORDER = blue team, CHAOS = red team
# These are tuned for the standard League minimap palette.
# Widen the ranges if detections are missed; narrow them if false positives appear.
TEAM_COLOR_RANGES = {
    "ORDER": {
        "lower": np.array([100, 100, 100], dtype=np.uint8),
        "upper": np.array([130, 255, 255], dtype=np.uint8),
    },
    "CHAOS": {
        "lower": np.array([0,   100, 100], dtype=np.uint8),
        "upper": np.array([10,  255, 255], dtype=np.uint8),
    },
}

# Summoner's Rift logical map dimensions (game coordinate space)
# Used to normalise pixel positions to map coordinates for the dashboard
MAP_WIDTH  = 14870
MAP_HEIGHT = 14870


# ── Resolution profiles ────────────────────────────────────────────────────────
# minimap_region: (left, top, right, bottom) in screen pixels at default minimap scale
# icon_size: expected champion icon diameter in pixels
# Calibration overrides these values.

RESOLUTION_PROFILES = {
    "1920x1080": {
        "minimap_region": (1645, 805, 1920, 1080),
        "icon_size": 24,
    },
    "2560x1440": {
        "minimap_region": (2193, 1073, 2560, 1440),
        "icon_size": 32,
    },
}

DEFAULT_RESOLUTION = "1920x1080"


# ── Config helpers ─────────────────────────────────────────────────────────────

def load_config():
    if os.path.exists(CONFIG_FILE):
        with open(CONFIG_FILE) as f:
            return json.load(f)
    return {}


def save_config(cfg):
    with open(CONFIG_FILE, "w") as f:
        json.dump(cfg, f, indent=2)
    print(f"Config saved to {CONFIG_FILE}")


def get_minimap_region(cfg):
    """
    Return (left, top, right, bottom) for the minimap region.
    Priority: calibrated value in config > resolution profile > 1080p default.
    """
    if "minimap_region" in cfg:
        r = cfg["minimap_region"]
        return tuple(r)

    res = cfg.get("resolution", DEFAULT_RESOLUTION)
    profile = RESOLUTION_PROFILES.get(res, RESOLUTION_PROFILES[DEFAULT_RESOLUTION])
    return profile["minimap_region"]


def get_icon_size(cfg):
    if "icon_size" in cfg:
        return cfg["icon_size"]
    res = cfg.get("resolution", DEFAULT_RESOLUTION)
    profile = RESOLUTION_PROFILES.get(res, RESOLUTION_PROFILES[DEFAULT_RESOLUTION])
    return profile["icon_size"]


# ── Calibration ────────────────────────────────────────────────────────────────

def run_calibration():
    """
    Interactive calibration: takes a screenshot and asks the user to click
    the top-left and bottom-right corners of the minimap.
    Saves result to minimap_config.json.
    """
    print("\n=== MINIMAP CALIBRATION ===")
    print("A screenshot will be taken. Click TOP-LEFT corner of minimap, then BOTTOM-RIGHT.")
    print("Press any key after taking note of coordinates if GUI is unavailable.\n")

    with mss.mss() as sct:
        screenshot = sct.grab(sct.monitors[1])
        img = np.array(screenshot)
        img_bgr = cv2.cvtColor(img, cv2.COLOR_BGRA2BGR)

    points = []

    def on_click(event, x, y, flags, param):
        if event == cv2.EVENT_LBUTTONDOWN:
            points.append((x, y))
            print(f"  Point {len(points)}: ({x}, {y})")
            cv2.circle(img_bgr, (x, y), 5, (0, 255, 0), -1)
            cv2.imshow("Calibration — click TOP-LEFT then BOTTOM-RIGHT of minimap", img_bgr)
            if len(points) == 2:
                print("  Both corners captured. Press any key to save.")

    cv2.namedWindow("Calibration — click TOP-LEFT then BOTTOM-RIGHT of minimap",
                    cv2.WINDOW_NORMAL)
    cv2.resizeWindow("Calibration — click TOP-LEFT then BOTTOM-RIGHT of minimap", 1280, 720)
    cv2.setMouseCallback(
        "Calibration — click TOP-LEFT then BOTTOM-RIGHT of minimap", on_click
    )
    cv2.imshow("Calibration — click TOP-LEFT then BOTTOM-RIGHT of minimap", img_bgr)
    cv2.waitKey(0)
    cv2.destroyAllWindows()

    if len(points) < 2:
        print("Calibration cancelled — fewer than 2 points captured.")
        return

    left, top   = points[0]
    right, bottom = points[1]
    region = [left, top, right, bottom]

    # Estimate icon size from minimap width — champion icons are ~8.5% of minimap width
    mm_width = right - left
    icon_size = max(12, int(mm_width * 0.085))

    cfg = load_config()
    cfg["minimap_region"] = region
    cfg["icon_size"] = icon_size
    save_config(cfg)

    print(f"\nCalibration saved:")
    print(f"  Region:    {region}")
    print(f"  Icon size: {icon_size}px (estimated from minimap width {mm_width}px)")
    print("\nRe-run calibration any time with: python minimap_tracker.py --calibrate")


# ── Champion icon management ───────────────────────────────────────────────────

def fetch_ddragon_version():
    try:
        with urllib.request.urlopen(DDG_VERSION_URL, timeout=5) as r:
            versions = json.loads(r.read())
            return versions[0]
    except Exception as e:
        print(f"[icons] Could not fetch DDragon version: {e}")
        return "14.1.1"  # fallback


def download_champion_icon(champion_name, version, icons_dir):
    """
    Download a champion's square portrait from Data Dragon.
    Saves as icons_dir/<champion_name>.png
    Returns the local path or None on failure.
    """
    path = os.path.join(icons_dir, f"{champion_name}.png")
    if os.path.exists(path):
        return path

    # Data Dragon uses specific capitalisation — try exact name first
    url = DDG_ICON_URL.format(version=version, name=champion_name)
    try:
        urllib.request.urlretrieve(url, path)
        return path
    except Exception:
        pass

    # Some champions have different DDragon keys (e.g. Wukong=MonkeyKing)
    DDRAGON_NAME_MAP = {
        "Wukong": "MonkeyKing",
        "Renata Glasc": "Renata",
        "Bel'Veth": "Belveth",
        "Cho'Gath": "Chogath",
        "Kha'Zix": "Khazix",
        "Kog'Maw": "KogMaw",
        "LeBlanc": "Leblanc",
        "Lee Sin": "LeeSin",
        "Master Yi": "MasterYi",
        "Miss Fortune": "MissFortune",
        "Nunu & Willump": "Nunu",
        "Rek'Sai": "RekSai",
        "Tahm Kench": "TahmKench",
        "Twisted Fate": "TwistedFate",
        "Vel'Koz": "Velkoz",
        "Xin Zhao": "XinZhao",
    }
    alt_name = DDRAGON_NAME_MAP.get(champion_name)
    if alt_name:
        url = DDG_ICON_URL.format(version=version, name=alt_name)
        try:
            urllib.request.urlretrieve(url, path)
            return path
        except Exception:
            pass

    print(f"[icons] Could not download icon for {champion_name}")
    return None


def prepare_champion_templates(champions, icon_size, icons_dir="champion_icons"):
    """
    Download and resize champion portrait crops to match the expected icon size
    on the minimap. Returns a dict: {champion_name: (team, template_array)}.
    champions is a list of {"name": ..., "team": ...} dicts from metadata.
    """
    os.makedirs(icons_dir, exist_ok=True)
    version = fetch_ddragon_version()
    print(f"[icons] Using DDragon version {version}")

    templates = {}
    for p in champions:
        name = p["name"]
        team = p["team"]
        path = download_champion_icon(name, version, icons_dir)
        if path is None:
            continue

        img = cv2.imread(path)
        if img is None:
            continue

        # Crop to square and resize to expected icon size
        h, w = img.shape[:2]
        side = min(h, w)
        img = img[:side, :side]
        template = cv2.resize(img, (icon_size, icon_size))
        templates[name] = (team, template)
        print(f"[icons] Loaded template: {name} ({team})")

    return templates


# ── Detection pipeline ─────────────────────────────────────────────────────────

def capture_minimap(region):
    """
    Capture the minimap region from the screen.
    region is (left, top, right, bottom).
    Returns a BGR numpy array.
    """
    left, top, right, bottom = region
    with mss.mss() as sct:
        monitor = {"left": left, "top": top, "width": right - left, "height": bottom - top}
        screenshot = sct.grab(monitor)
        img = np.array(screenshot)
        return cv2.cvtColor(img, cv2.COLOR_BGRA2BGR)


def find_champion_candidates(minimap_bgr, icon_size):
    """
    Find all circular champion icon candidates using color filtering + contour detection.
    Returns list of (x, y, w, h, team_hint) in minimap-local pixel coordinates.
    team_hint is 'ORDER', 'CHAOS', or None if color didn't match.
    """
    hsv = cv2.cvtColor(minimap_bgr, cv2.COLOR_BGR2HSV)
    candidates = []

    for team, ranges in TEAM_COLOR_RANGES.items():
        mask = cv2.inRange(hsv, ranges["lower"], ranges["upper"])

        # Slight dilation to connect broken icon borders
        kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (3, 3))
        mask = cv2.dilate(mask, kernel, iterations=1)

        contours, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)

        for cnt in contours:
            area = cv2.contourArea(cnt)
            if area < MIN_CONTOUR_AREA or area > MAX_CONTOUR_AREA:
                continue

            x, y, w, h = cv2.boundingRect(cnt)

            # Champion icons are roughly square — reject very elongated shapes
            aspect = w / max(h, 1)
            if aspect < 0.5 or aspect > 2.0:
                continue

            candidates.append((x, y, w, h, team))

    return candidates


def match_champion(minimap_bgr, x, y, w, h, templates, icon_size, threshold=0.4):
    """
    Given a candidate bounding box, crop the minimap and template-match
    against all known champion portraits.
    Returns (champion_name, team, score) or (None, hint_team, 0) if no match.
    """
    # Expand crop slightly and clamp to minimap bounds
    pad = 4
    mm_h, mm_w = minimap_bgr.shape[:2]
    x1 = max(0, x - pad)
    y1 = max(0, y - pad)
    x2 = min(mm_w, x + w + pad)
    y2 = min(mm_h, y + h + pad)

    crop = minimap_bgr[y1:y2, x1:x2]
    if crop.size == 0:
        return None, None, 0.0

    crop_resized = cv2.resize(crop, (icon_size, icon_size))

    best_name  = None
    best_team  = None
    best_score = 0.0

    for name, (team, template) in templates.items():
        result = cv2.matchTemplate(crop_resized, template, cv2.TM_CCOEFF_NORMED)
        _, score, _, _ = cv2.minMaxLoc(result)
        if score > best_score:
            best_score = score
            best_name  = name
            best_team  = team

    if best_score >= threshold:
        return best_name, best_team, best_score

    return None, None, 0.0


def pixel_to_map_coords(px, py, minimap_w, minimap_h):
    """
    Convert minimap pixel (px, py) to Summoner's Rift logical map coordinates.
    The minimap (0,0) is top-left = map top-left.
    Map Y is inverted — in-game Y increases upward, screen Y increases downward.
    """
    map_x = int((px / minimap_w) * MAP_WIDTH)
    map_y = int((1.0 - py / minimap_h) * MAP_HEIGHT)
    return map_x, map_y


# ── Tracker class ──────────────────────────────────────────────────────────────

class MinimapTracker:
    """
    Captures minimap screenshots at regular intervals and writes detected
    champion positions to a CSV file.

    Can be used standalone (via main()) or embedded in main.py:

        tracker = MinimapTracker(
            folder="scrim_2024-01-15_18-30",
            champions=[{"name": "Syndra", "team": "ORDER"}, ...]
        )
        tracker.start()
        # ... game runs ...
        tracker.stop()
    """

    def __init__(self, folder=None, champions=None):
        self.cfg     = load_config()
        self.region  = get_minimap_region(self.cfg)
        self.icon_size = get_icon_size(self.cfg)

        # Create output folder
        if folder is None:
            folder = f"scrim_{datetime.now().strftime('%Y-%m-%d_%H-%M')}"
        os.makedirs(folder, exist_ok=True)
        self.folder = folder

        self.output_path = os.path.join(folder, "minimap_positions.csv")
        self.champions   = champions or []  # list of {"name":..., "team":...}
        self.templates   = {}
        self._thread     = None
        self._stop_event = threading.Event()
        self._csv_lock   = threading.Lock()
        self._csv_file   = None
        self._csv_writer = None

    def _init_output(self):
        self._csv_file = open(self.output_path, "w", newline="", encoding="utf-8")
        self._csv_writer = csv.DictWriter(self._csv_file, fieldnames=[
            "timestamp", "champion", "team",
            "pixel_x", "pixel_y",
            "map_x", "map_y",
            "confidence",
        ])
        self._csv_writer.writeheader()

    def _load_templates(self):
        if not self.champions:
            print("[tracker] No champions provided — identity matching disabled.")
            print("[tracker] Will detect positions by team color only.")
            return
        self.templates = prepare_champion_templates(
            self.champions, self.icon_size
        )

    def _run(self):
        print(f"[tracker] Starting — region {self.region}, icon size {self.icon_size}px")
        print(f"[tracker] Output: {self.output_path}")

        self._init_output()
        self._load_templates()

        mm_left, mm_top, mm_right, mm_bottom = self.region
        mm_w = mm_right  - mm_left
        mm_h = mm_bottom - mm_top

        while not self._stop_event.is_set():
            t_start = time.time()

            try:
                minimap = capture_minimap(self.region)
                game_time = time.time()  # wall clock — replace with API gameTime if integrated

                candidates = find_champion_candidates(minimap, self.icon_size)

                # Deduplicate candidates that are too close together
                # (same champion detected by both color passes)
                merged = _merge_nearby_candidates(candidates, min_dist=self.icon_size // 2)

                for (x, y, w, h, team_hint) in merged:
                    cx = x + w // 2
                    cy = y + h // 2

                    if self.templates:
                        name, team, score = match_champion(
                            minimap, x, y, w, h, self.templates, self.icon_size
                        )
                    else:
                        name, team, score = None, team_hint, 0.0

                    map_x, map_y = pixel_to_map_coords(cx, cy, mm_w, mm_h)

                    with self._csv_lock:
                        self._csv_writer.writerow({
                            "timestamp":  round(game_time, 3),
                            "champion":   name or "unknown",
                            "team":       team or team_hint or "unknown",
                            "pixel_x":    cx,
                            "pixel_y":    cy,
                            "map_x":      map_x,
                            "map_y":      map_y,
                            "confidence": round(score, 3),
                        })
                    self._csv_file.flush()

            except Exception as e:
                print(f"[tracker] Error during capture: {e}")

            elapsed = time.time() - t_start
            sleep_for = max(0.0, CAPTURE_INTERVAL - elapsed)
            self._stop_event.wait(timeout=sleep_for)

        if self._csv_file:
            self._csv_file.close()
        print("[tracker] Stopped.")

    def start(self):
        """Start tracking in a background thread (non-blocking)."""
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()
        print(f"[tracker] Background thread started.")

    def stop(self):
        """Signal the tracker to stop and wait for it to finish writing."""
        self._stop_event.set()
        if self._thread:
            self._thread.join(timeout=5)
        print(f"[tracker] Positions saved to {self.output_path}")

    def run_blocking(self):
        """Run in the current thread — used when running standalone."""
        self._run()


def _merge_nearby_candidates(candidates, min_dist):
    """
    Remove duplicate candidates whose centres are within min_dist pixels.
    Keeps the candidate with the larger contour area.
    """
    if not candidates:
        return []

    kept = []
    for cand in sorted(candidates, key=lambda c: c[2] * c[3], reverse=True):
        cx = cand[0] + cand[2] // 2
        cy = cand[1] + cand[3] // 2
        too_close = False
        for k in kept:
            kx = k[0] + k[2] // 2
            ky = k[1] + k[3] // 2
            if ((cx - kx) ** 2 + (cy - ky) ** 2) ** 0.5 < min_dist:
                too_close = True
                break
        if not too_close:
            kept.append(cand)
    return kept


# ── Standalone entry point ─────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="League of Legends minimap position tracker"
    )
    parser.add_argument(
        "--calibrate", action="store_true",
        help="Run interactive calibration to set minimap screen coordinates"
    )
    parser.add_argument(
        "--folder", type=str, default=None,
        help="Scrim folder to write output into (created if missing)"
    )
    parser.add_argument(
        "--champions", type=str, default=None,
        help="Comma-separated champion names to load templates for, "
             "e.g. 'Syndra,Jinx,Thresh'. If omitted, loads from folder/metadata.json"
    )
    args = parser.parse_args()

    if args.calibrate:
        run_calibration()
        return

    # Resolve champion list
    champions = []
    if args.champions:
        for name in args.champions.split(","):
            name = name.strip()
            if name:
                # team unknown when passed via CLI — tag as unknown
                champions.append({"name": name, "team": "unknown"})
    elif args.folder:
        meta_path = os.path.join(args.folder, "metadata.json")
        if os.path.exists(meta_path):
            with open(meta_path) as f:
                meta = json.load(f)
            for p in meta.get("players", []):
                champions.append({
                    "name": p.get("champion", ""),
                    "team": p.get("team", "unknown"),
                })
            print(f"[tracker] Loaded {len(champions)} champions from metadata.json")
        else:
            print(f"[tracker] No metadata.json found in {args.folder} — running without champion ID")

    tracker = MinimapTracker(folder=args.folder, champions=champions)

    print("Press Ctrl+C to stop tracking.\n")
    try:
        tracker.run_blocking()
    except KeyboardInterrupt:
        print("\n[tracker] Interrupted.")
        if tracker._csv_file:
            tracker._csv_file.close()
        print(f"[tracker] Positions saved to {tracker.output_path}")


if __name__ == "__main__":
    main()