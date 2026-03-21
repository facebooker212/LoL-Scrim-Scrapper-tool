"""
collect_training_data.py
────────────────────────
Runs while spectating a League of Legends game and collects champion icon
crops for CNN classifier training data.

Does NOT require main.py or a pre-existing scrim folder. It queries the
Live Client Data API directly at startup to get the champion list, then
collects crops immediately.

USAGE:
    python collect_training_data.py

    Optional overrides:
    --champions "Syndra,Jinx,Thresh"   manually specify champions
    --no-api                           skip API fetch, collect without suggestions

OUTPUT:
    raw_crops/
        <team>_<suggestion>_c<conf>_<mapx>_<mapy>_<ts>.png

    Each crop is 32x32px, border-stripped, ready for label_crops.py

WORKFLOW:
    1. Start spectating a game in League
    2. Run this script -- it auto-detects the 10 champions via the API
    3. Press Ctrl+C when done spectating
    4. Run label_crops.py to label what was collected
    5. Repeat across 10-15 games, then run train_classifier.py

DEDUPLICATION:
    A detection is skipped if a crop was already saved within DEDUP_RADIUS
    map units in the last DEDUP_SECONDS seconds.

DEPENDENCIES:
    pip install opencv-python mss numpy requests urllib3
    pip install pywin32   (optional, enables alt-tab window capture)
"""

import cv2
import numpy as np
import json
import os
import time
import threading
import argparse
import requests
import urllib3
from datetime import datetime
from pathlib import Path

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

from minimap_tracker import (
    load_config,
    get_minimap_region,
    get_icon_size,
    capture_minimap,
    find_champion_candidates,
    _merge_nearby_candidates,
    _strip_border,
    prepare_champion_templates,
    match_champion,
    pixel_to_map_coords,
    WindowCapture,
)

# ── Settings ──────────────────────────────────────────────────────────────────

CAPTURE_INTERVAL       = 2.0
COMPARE_SIZE           = 32
DEDUP_RADIUS           = 500
DEDUP_SECONDS          = 10.0
AUTO_LABEL_THRESHOLD   = 0.38

LCS_API_URL  = "https://127.0.0.1:2999/liveclientdata/allgamedata"
API_TIMEOUT  = 2
RAW_CROPS_DIR = "raw_crops"

_api_session = requests.Session()
_api_session.verify = False


# ── Live Client API helpers ───────────────────────────────────────────────────

def fetch_champions_from_api():
    """
    Poll the Live Client Data API until a game is detected and return the
    10 champions as a list of {"name": ..., "team": ...} dicts.
    Retries every 3 seconds. Raises KeyboardInterrupt if user cancels.
    """
    print("[api] Waiting for League game to be detected...")
    while True:
        try:
            r = _api_session.get(LCS_API_URL, timeout=API_TIMEOUT)
            if r.status_code == 200:
                data    = r.json()
                players = data.get("allPlayers", [])
                if players:
                    champions = [
                        {
                            "name": p.get("championName", ""),
                            "team": p.get("team", "unknown"),
                        }
                        for p in players
                        if p.get("championName")
                    ]
                    print(f"[api] Game detected — {len(champions)} champions:")
                    for c in champions:
                        print(f"      {c['name']:20s} ({c['team']})")
                    return champions
        except Exception:
            pass

        print("[api] Not detected yet, retrying in 3s... (Ctrl+C to skip)")
        time.sleep(3)


# ── Deduplication ─────────────────────────────────────────────────────────────

class RecentDetections:
    def __init__(self):
        self._entries = []
        self._lock    = threading.Lock()

    def is_duplicate(self, map_x, map_y, team):
        now = time.time()
        with self._lock:
            self._entries = [e for e in self._entries if now - e[0] < DEDUP_SECONDS]
            for _, ex, ey, et in self._entries:
                if et == team:
                    if ((map_x - ex)**2 + (map_y - ey)**2)**0.5 < DEDUP_RADIUS:
                        return True
        return False

    def add(self, map_x, map_y, team):
        with self._lock:
            self._entries.append((time.time(), map_x, map_y, team))


# ── Collector ─────────────────────────────────────────────────────────────────

class TrainingDataCollector:

    def __init__(self, champions=None):
        self.cfg        = load_config()
        self.region     = get_minimap_region(self.cfg)
        self.icon_size  = get_icon_size(self.cfg)
        self.champions  = champions or []
        self.templates  = {}
        self.recent     = RecentDetections()
        self._stop_event = threading.Event()
        self._saved     = 0
        self._thread    = None
        os.makedirs(RAW_CROPS_DIR, exist_ok=True)

    def _load_templates(self):
        if not self.champions:
            print("[collector] No champions — saving without suggestions.")
            return
        self.templates = prepare_champion_templates(self.champions, self.icon_size)

    def _run(self):
        print(f"\n[collector] Region: {self.region}  Icon size: {self.icon_size}px")
        print(f"[collector] Saving crops to: {RAW_CROPS_DIR}/\n")

        self._load_templates()

        window_relative = self.cfg.get("window_relative", False)
        wc = WindowCapture() if window_relative else None
        if wc and not wc.using_window_capture:
            wc = None

        mm_left, mm_top, mm_right, mm_bottom = self.region
        mm_w = mm_right - mm_left
        mm_h = mm_bottom - mm_top

        frames_captured = 0

        while not self._stop_event.is_set():
            t_start = time.time()

            # ── Capture ───────────────────────────────────────────────────────
            try:
                minimap = capture_minimap(self.region, window_capture=wc)
            except Exception as e:
                print(f"[collector] Capture error: {e}")
                time.sleep(0.5)
                continue

            if minimap is None or minimap.size == 0 or minimap.shape[0] == 0 or minimap.shape[1] == 0:
                time.sleep(0.5)
                continue

            frames_captured += 1

            # ── Detect ────────────────────────────────────────────────────────
            try:
                candidates = find_champion_candidates(minimap, self.icon_size)
                merged     = _merge_nearby_candidates(candidates, min_dist=self.icon_size // 2)
            except Exception as e:
                print(f"[collector] Detection error: {e}")
                continue

            # Print every 10th frame so we can see it is running
            if frames_captured % 10 == 1:
                print(f"[collector] frame={frames_captured}  candidates={len(merged)}  saved={self._saved}")

            # ── Save crops ────────────────────────────────────────────────────
            for (x, y, w, h, team_hint) in merged:
                try:
                    cx, cy       = x + w // 2, y + h // 2
                    map_x, map_y = pixel_to_map_coords(cx, cy, mm_w, mm_h)

                    if self.recent.is_duplicate(map_x, map_y, team_hint):
                        continue

                    mh, mw = minimap.shape[:2]
                    crop = minimap[max(0, y):min(mh, y + h), max(0, x):min(mw, x + w)]
                    if crop.size == 0:
                        print(f"[collector] Empty crop at ({x},{y}) size ({w}x{h}) — skipping")
                        continue

                    crop_inner = _strip_border(crop)
                    if crop_inner.size == 0:
                        print(f"[collector] Empty after strip at ({x},{y}) — skipping")
                        continue

                    crop_32 = cv2.resize(crop_inner, (COMPARE_SIZE, COMPARE_SIZE))

                    suggestion = "unknown"
                    conf       = 0.0
                    if self.templates:
                        name, _, score = match_champion(
                            minimap, x, y, w, h,
                            self.templates, self.icon_size,
                            threshold=AUTO_LABEL_THRESHOLD,
                        )
                        if name:
                            suggestion = name
                            conf       = score

                    ts_ms    = int(time.time() * 1000)
                    conf_str = f"{conf:.2f}".replace(".", "")
                    fname    = (
                        f"{team_hint}_{suggestion}_c{conf_str}"
                        f"_{map_x}_{map_y}_{ts_ms}.png"
                    )
                    out_path = os.path.join(RAW_CROPS_DIR, fname)
                    result   = cv2.imwrite(out_path, crop_32)
                    if not result:
                        print(f"[collector] imwrite failed: {out_path}")
                        continue

                    self.recent.add(map_x, map_y, team_hint)
                    self._saved += 1

                    if self._saved % 20 == 0:
                        print(f"[collector] {self._saved} crops saved")

                except Exception as e:
                    import traceback
                    print(f"[collector] Error saving crop at ({x},{y}): {e}")
                    traceback.print_exc()

            elapsed = time.time() - t_start
            self._stop_event.wait(timeout=max(0.0, CAPTURE_INTERVAL - elapsed))

        print(f"\n[collector] Stopped. {self._saved} crops saved to {RAW_CROPS_DIR}/")
        print(f"[collector] Run:  python label_crops.py  to label them.")

    def run_blocking(self):
        self._run()


# ── Live Client API ───────────────────────────────────────────────────────────

import urllib3
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

API_BASE = "https://127.0.0.1:2999/liveclientdata"


def _api_get(endpoint):
    import requests
    try:
        r = requests.get(f"{API_BASE}/{endpoint}", timeout=2, verify=False)
        if r.status_code == 200:
            return r.json()
    except Exception:
        pass
    return None


def fetch_champions_from_api():
    """
    Query the Live Client Data API for the current game champion list.
    Waits until the API is available — just start spectating and it will
    detect the game automatically, no folder or metadata.json needed.
    Returns a list of {"name": ..., "team": ...} dicts.
    """
    print("[collector] Waiting for Live Client API (start spectating a game)...")
    while True:
        data = _api_get("allgamedata")
        if data and "allPlayers" in data:
            champions = []
            for p in data["allPlayers"]:
                name = p.get("championName", "").strip()
                team = p.get("team", "unknown")
                if name:
                    champions.append({"name": name, "team": team})
            if champions:
                print(f"[collector] Detected {len(champions)} champions:")
                for c in champions:
                    print(f"  {c['team']:6s}  {c['name']}")
                print()
                return champions
        time.sleep(2)


# ── Entry point ───────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="Collect champion icon crops for CNN training data. "
                    "Reads champions directly from the Live Client API — "
                    "no folder or metadata.json required."
    )
    parser.add_argument(
        "--champions", type=str, default=None,
        help="Optional: comma-separated champion names to skip API fetch, "
             "e.g. 'Syndra,Jinx,Thresh'. Useful for testing without a live game."
    )
    parser.add_argument(
        "--no-suggestions", action="store_true",
        help="Collect crops without template match suggestions. "
             "Faster startup, all crops labeled as unknown."
    )
    args = parser.parse_args()

    champions = []

    if args.champions:
        # Manual champion list — skip API entirely
        for name in args.champions.split(","):
            name = name.strip()
            if name:
                champions.append({"name": name, "team": "unknown"})
        print(f"[collector] Using manual champion list: {[c['name'] for c in champions]}")

    elif args.no_suggestions:
        # Collect without any suggestions — labeler handles everything
        print("[collector] Running without suggestions.")

    else:
        # Default: fetch from Live Client API, wait until game is detected
        try:
            champions = fetch_champions_from_api()
        except KeyboardInterrupt:
            print("\n[collector] Skipping API fetch — collecting without suggestions.")
            champions = []

    collector = TrainingDataCollector(champions=champions)
    print("Collecting crops. Press Ctrl+C to stop.\n")

    try:
        collector.run_blocking()
    except KeyboardInterrupt:
        collector._stop_event.set()
        print(f"\n[collector] {collector._saved} crops saved to {RAW_CROPS_DIR}/")
        print("[collector] Run:  python label_crops.py  to label them.")


if __name__ == "__main__":
    main()