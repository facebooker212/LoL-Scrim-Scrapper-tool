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
    pip install pywin32   (optional -- enables window capture so you can alt-tab)

WINDOW CAPTURE:
    When pywin32 is installed, the tracker captures only the League of Legends
    window by name rather than the full screen. This means you can alt-tab freely
    while tracking. Requires League to run in Borderless Windowed mode (not
    true fullscreen) -- fullscreen DirectX windows return black frames.
    If pywin32 is not installed the tracker falls back to full-screen mss capture
    and calibrated screen coordinates work as before.

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

# pywin32 is optional -- enables capturing a specific window by name
try:
    import win32gui
    import win32ui
    import win32con
    import win32api
    _win32_available = True
except ImportError:
    _win32_available = False

# League of Legends window title (spectator mode uses the same title)
LOL_WINDOW_TITLE = "League of Legends"


# ── Window capture ────────────────────────────────────────────────────────────

class WindowCapture:
    """
    Captures screenshots of a specific window by name using pywin32.
    Works even when the window is not in focus (alt-tabbed), as long as
    League is running in Borderless Windowed mode -- true fullscreen DirectX
    windows return black frames regardless of this approach.

    Falls back to mss full-screen capture if pywin32 is not available.
    """

    def __init__(self, window_title=LOL_WINDOW_TITLE):
        self.window_title = window_title
        self.hwnd = None
        self.window_rect = None   # (left, top, right, bottom) in screen coords
        self._use_win32 = _win32_available
        if self._use_win32:
            self._find_window()

    def _find_window(self):
        """Locate the window handle by partial title match."""
        results = []

        def enum_handler(hwnd, _):
            if win32gui.IsWindowVisible(hwnd):
                title = win32gui.GetWindowText(hwnd)
                if self.window_title.lower() in title.lower():
                    results.append((hwnd, title))

        win32gui.EnumWindows(enum_handler, None)

        if not results:
            print(f"[capture] Window '{self.window_title}' not found -- falling back to mss")
            self._use_win32 = False
            return

        self.hwnd = results[0][0]
        rect = win32gui.GetWindowRect(self.hwnd)
        # GetWindowRect includes window borders -- trim standard border sizes
        # For borderless windowed this is 0, for windowed mode it's ~8px
        self.window_rect = rect
        print(f"[capture] Found window: '{results[0][1]}' at {rect}")

    def get_window_rect(self):
        """Return current (left, top, right, bottom) of the League window."""
        if self.hwnd and self._use_win32:
            try:
                return win32gui.GetWindowRect(self.hwnd)
            except Exception:
                pass
        return None

    def screenshot(self):
        """
        Capture the full League window as a BGR numpy array.
        Returns None on failure.

        Uses BitBlt to read directly from the window device context.
        BitBlt works for borderless windowed games even when unfocused,
        unlike PrintWindow which captures the foreground window on some
        systems when the target window loses focus.
        """
        if not self._use_win32 or not self.hwnd:
            return self._screenshot_mss_fullscreen()

        try:
            rect = win32gui.GetWindowRect(self.hwnd)
            left, top, right, bottom = rect
            w = right - left
            h = bottom - top

            hwnd_dc  = win32gui.GetWindowDC(self.hwnd)
            mfc_dc   = win32ui.CreateDCFromHandle(hwnd_dc)
            save_dc  = mfc_dc.CreateCompatibleDC()
            bitmap   = win32ui.CreateBitmap()
            bitmap.CreateCompatibleBitmap(mfc_dc, w, h)
            save_dc.SelectObject(bitmap)

            # BitBlt reads directly from the window DC — works when unfocused
            # SRCCOPY = 0x00CC0020
            save_dc.BitBlt((0, 0), (w, h), mfc_dc, (0, 0), win32con.SRCCOPY)

            bmp_info = bitmap.GetInfo()
            bmp_str  = bitmap.GetBitmapBits(True)
            img = np.frombuffer(bmp_str, dtype=np.uint8).reshape(
                bmp_info["bmHeight"], bmp_info["bmWidth"], 4
            )

            win32gui.DeleteObject(bitmap.GetHandle())
            save_dc.DeleteDC()
            mfc_dc.DeleteDC()
            win32gui.ReleaseDC(self.hwnd, hwnd_dc)

            # Sanity check — if the capture is mostly black, BitBlt got a blank
            # frame (can happen briefly during window transitions). Fall back to mss.
            import numpy as _np
            if _np.mean(img) < 5:
                return self._screenshot_mss_window(rect)

            return cv2.cvtColor(img, cv2.COLOR_BGRA2BGR)

        except Exception as e:
            print(f"[capture] win32 capture failed: {e} -- falling back to mss")
            return self._screenshot_mss_fullscreen()

    def screenshot_region(self, region_in_window):
        """
        Capture a sub-region within the window.
        region_in_window is (left, top, right, bottom) relative to the window's
        top-left corner (not screen coordinates).
        Returns a BGR numpy array cropped to the region.
        """
        full = self.screenshot()
        if full is None:
            return None
        l, t, r, b = region_in_window
        return full[t:b, l:r]

    def _screenshot_mss_fullscreen(self):
        with mss.mss() as sct:
            shot = sct.grab(sct.monitors[1])
            img = np.array(shot)
            return cv2.cvtColor(img, cv2.COLOR_BGRA2BGR)

    def _screenshot_mss_window(self, rect):
        left, top, right, bottom = rect
        with mss.mss() as sct:
            monitor = {"left": left, "top": top,
                       "width": right - left, "height": bottom - top}
            shot = sct.grab(monitor)
            img = np.array(shot)
            return cv2.cvtColor(img, cv2.COLOR_BGRA2BGR)

    @property
    def using_window_capture(self):
        return self._use_win32 and self.hwnd is not None


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

# ── Config helpers (defined early — used by color range loader below) ─────────

def load_config():
    if os.path.exists(CONFIG_FILE):
        with open(CONFIG_FILE) as f:
            return json.load(f)
    return {}


def save_config(cfg):
    with open(CONFIG_FILE, "w") as f:
        json.dump(cfg, f, indent=2)
    print(f"Config saved to {CONFIG_FILE}")


# HSV color ranges for champion icon borders
# ORDER = blue team, CHAOS = red team
# These are DEFAULTS — run --sample-colors to measure your actual minimap values
# and save them to minimap_config.json, which overrides these at runtime.
# Widen the ranges if detections are missed; narrow them if false positives appear.
_DEFAULT_COLOR_RANGES = {
    "ORDER": {"lower": [100, 80,  80],  "upper": [130, 255, 255]},
    "CHAOS": {"lower": [0,   120, 120], "upper": [10,  255, 255]},
}

def get_color_ranges():
    """Load HSV ranges from config if present, otherwise use defaults."""
    cfg = load_config()
    saved = cfg.get("color_ranges")
    if saved:
        return {
            team: {
                "lower": np.array(v["lower"], dtype=np.uint8),
                "upper": np.array(v["upper"], dtype=np.uint8),
            }
            for team, v in saved.items()
        }
    return {
        team: {
            "lower": np.array(v["lower"], dtype=np.uint8),
            "upper": np.array(v["upper"], dtype=np.uint8),
        }
        for team, v in _DEFAULT_COLOR_RANGES.items()
    }

# Module-level reference — refreshed on each tracker start
TEAM_COLOR_RANGES = get_color_ranges()

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

    # Prefer capturing just the League window so calibration works after alt-tab
    wc = WindowCapture()
    if wc.using_window_capture:
        print(f"[calibrate] Capturing League window (window-relative coordinates).")
        print(f"[calibrate] You can alt-tab freely during calibration.\n")
        img_bgr = wc.screenshot()
        # Store that calibration coords are window-relative
        _calibration_window_relative = True
    else:
        print("[calibrate] pywin32 not available or window not found -- capturing full screen.")
        print("[calibrate] Keep League in focus during calibration.\n")
        with mss.mss() as sct:
            screenshot = sct.grab(sct.monitors[1])
            img = np.array(screenshot)
            img_bgr = cv2.cvtColor(img, cv2.COLOR_BGRA2BGR)
        _calibration_window_relative = False

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
    cfg["window_relative"] = _calibration_window_relative
    save_config(cfg)

    print(f"\nCalibration saved:")
    print(f"  Region:          {region}")
    print(f"  Icon size:       {icon_size}px (estimated from minimap width {mm_width}px)")
    print(f"  Window-relative: {_calibration_window_relative}")
    if _calibration_window_relative:
        print("  Alt-tab is supported -- League window will be captured directly.")
    else:
        print("  Full-screen mode -- keep League focused during tracking.")
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
        "Xin Zhao":   "XinZhao",
        "K'Sante":    "KSante",
        "Kai'Sa":     "Kaisa",
        "Kha'Zix":    "Khazix",
        "Kog'Maw":    "KogMaw",
        "Rek'Sai":    "RekSai",
        "Vel'Koz":    "Velkoz",
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

        # Crop to square, strip the outer ~18% to match what match_champion sees
        # (the minimap icon has a border ring; the DDragon portrait is a square face crop —
        # by insetting both sides equally the comparison becomes face vs face)
        h, w = img.shape[:2]
        side = min(h, w)
        img = img[:side, :side]
        img = _strip_border(img)
        COMPARE_SIZE = 32
        template = cv2.resize(img, (COMPARE_SIZE, COMPARE_SIZE))
        templates[name] = (team, template)
        print(f"[icons] Loaded template: {name} ({team})")

    return templates


# ── Detection pipeline ─────────────────────────────────────────────────────────

def capture_minimap(region, window_capture=None):
    """
    Capture the minimap region.
    - If window_capture is a WindowCapture instance using win32, region is
      relative to the window top-left (so it works after alt-tab).
    - Otherwise region is absolute screen coordinates and mss is used.
    Returns a BGR numpy array, or None on failure.
    """
    if window_capture is not None and window_capture.using_window_capture:
        return window_capture.screenshot_region(region)

    # Fallback: absolute screen coordinates via mss
    left, top, right, bottom = region
    with mss.mss() as sct:
        monitor = {"left": left, "top": top, "width": right - left, "height": bottom - top}
        screenshot = sct.grab(monitor)
        img = np.array(screenshot)
        return cv2.cvtColor(img, cv2.COLOR_BGRA2BGR)


def find_champion_candidates(minimap_bgr, icon_size):
    """
    Find champion icon candidates using color filtering + contour detection.
    Reloads HSV ranges from config on each call so --sample-colors changes
    take effect immediately without restarting.
    Returns list of (x, y, w, h, team_hint) in minimap-local pixel coordinates.
    """
    color_ranges = get_color_ranges()
    hsv = cv2.cvtColor(minimap_bgr, cv2.COLOR_BGR2HSV)
    candidates = []

    # Dynamic area bounds based on icon_size
    # Champion icons are roughly icon_size x icon_size pixels
    min_area = max(20, int((icon_size * 0.3) ** 2))
    max_area = int((icon_size * 1.8) ** 2)

    for team, ranges in color_ranges.items():
        mask = cv2.inRange(hsv, ranges["lower"], ranges["upper"])

        # Slight dilation to connect broken icon borders
        kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (3, 3))
        mask = cv2.dilate(mask, kernel, iterations=1)

        contours, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)

        for cnt in contours:
            area = cv2.contourArea(cnt)
            if area < min_area or area > max_area:
                continue

            x, y, w, h = cv2.boundingRect(cnt)

            # Champion icons are roughly square — reject elongated shapes
            aspect = w / max(h, 1)
            if aspect < 0.5 or aspect > 2.0:
                continue

            # Circularity check — champion icon borders are circular/rounded
            # Turret icons, ping arrows, and ward diamonds score much lower
            perimeter = cv2.arcLength(cnt, True)
            if perimeter > 0:
                circularity = (4 * np.pi * area) / (perimeter ** 2)
                if circularity < 0.35:
                    continue

            candidates.append((x, y, w, h, team))

    return candidates


# Border inset fraction — how much of each side to strip to remove the
# circular border ring before template matching. 0.18 = 18% per side.
BORDER_INSET = 0.18


def _strip_border(img):
    """
    Crop out the circular icon border ring by insetting BORDER_INSET from each edge.
    Both the detected crop and the template are processed identically so the
    comparison is border-free face vs border-free face.
    """
    h, w = img.shape[:2]
    inset_x = max(1, int(w * BORDER_INSET))
    inset_y = max(1, int(h * BORDER_INSET))
    return img[inset_y:h - inset_y, inset_x:w - inset_x]


def match_champion(minimap_bgr, x, y, w, h, templates, icon_size, threshold=0.35):
    """
    Given a candidate bounding box, crop the minimap and template-match
    against known champion portraits.
    Both the crop and the template have their border ring stripped before
    comparison so the match is face-to-face rather than circle-to-square.
    Returns (champion_name, team, score) or (None, None, 0.0) if below threshold.
    """
    mm_h, mm_w = minimap_bgr.shape[:2]
    # Use the bounding box directly — no outward padding since the border is
    # already included in the detected contour bounds
    x1 = max(0, x)
    y1 = max(0, y)
    x2 = min(mm_w, x + w)
    y2 = min(mm_h, y + h)

    crop = minimap_bgr[y1:y2, x1:x2]
    if crop.size == 0:
        return None, None, 0.0

    # Strip border ring and resize to a fixed comparison size
    COMPARE_SIZE = 32
    crop_inner = _strip_border(crop)
    if crop_inner.size == 0:
        return None, None, 0.0
    crop_cmp = cv2.resize(crop_inner, (COMPARE_SIZE, COMPARE_SIZE))

    best_name  = None
    best_team  = None
    best_score = 0.0

    for name, (team, template) in templates.items():
        result = cv2.matchTemplate(crop_cmp, template, cv2.TM_CCOEFF_NORMED)
        _, score, _, _ = cv2.minMaxLoc(result)
        if score > best_score:
            best_score = score
            best_name  = name
            best_team  = team

    if best_score >= threshold:
        return best_name, best_team, best_score

    return None, None, 0.0


# ── CNN Classifier ────────────────────────────────────────────────────────────

MODEL_PATH = "champion_classifier.pth"

# Torch is optional — falls back to template matching if not installed
try:
    import torch
    import torch.nn as nn
    from torchvision import transforms as T
    _torch_available = True
except ImportError:
    _torch_available = False


class _ChampionCNN(nn.Module if _torch_available else object):
    """Mirror of the architecture in train_classifier.py."""
    def __init__(self, num_classes):
        super().__init__()
        self.features = nn.Sequential(
            nn.Conv2d(3, 32, kernel_size=3, padding=1),
            nn.BatchNorm2d(32), nn.ReLU(inplace=True), nn.MaxPool2d(2),
            nn.Conv2d(32, 64, kernel_size=3, padding=1),
            nn.BatchNorm2d(64), nn.ReLU(inplace=True), nn.MaxPool2d(2),
            nn.Conv2d(64, 128, kernel_size=3, padding=1),
            nn.BatchNorm2d(128), nn.ReLU(inplace=True), nn.MaxPool2d(2),
        )
        self.classifier = nn.Sequential(
            nn.Dropout(0.4),
            nn.Linear(128 * 4 * 4, 256),
            nn.ReLU(inplace=True),
            nn.Dropout(0.3),
            nn.Linear(256, num_classes),
        )

    def forward(self, x):
        x = self.features(x)
        x = x.view(x.size(0), -1)
        return self.classifier(x)


class ChampionClassifier:
    """
    Wraps the trained CNN for inference on minimap icon crops.
    Loaded once at tracker startup. Falls back gracefully if the model
    file or torch is not available.
    """

    # Normalisation must match train_classifier.py
    _transform = T.Compose([
        T.ToPILImage(),
        T.Resize((32, 32)),
        T.ToTensor(),
        T.Normalize(mean=[0.485, 0.456, 0.406],
                    std=[0.229, 0.224, 0.225]),
    ]) if _torch_available else None

    def __init__(self, model_path=MODEL_PATH, team_map=None):
        """
        team_map: dict of {champion_name: team} built from current game metadata.
        Used to restrict predictions to only the 10 champions in the game when
        provided, improving accuracy significantly.
        """
        self.ready      = False
        self.class_names = []
        self.team_map   = team_map or {}
        self._model     = None
        self._device    = None

        if not _torch_available:
            print("[classifier] torch not installed — using template matching fallback")
            return

        if not os.path.exists(model_path):
            print(f"[classifier] {model_path} not found — using template matching fallback")
            return

        try:
            self._device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
            checkpoint   = torch.load(model_path, map_location=self._device,
                                      weights_only=False)
            self.class_names = checkpoint["class_names"]
            num_classes      = checkpoint["num_classes"]

            self._model = _ChampionCNN(num_classes=num_classes).to(self._device)
            self._model.load_state_dict(checkpoint["model_state"])
            self._model.eval()

            self.ready = True
            print(f"[classifier] Loaded CNN — {num_classes} champions, "
                  f"device={self._device}, val_acc={checkpoint.get('val_acc', 0):.1f}%")
        except Exception as e:
            print(f"[classifier] Failed to load model: {e} — using template matching fallback")

    def predict(self, crop_bgr, team_hint=None, threshold=0.5):
        """
        Run inference on a border-stripped BGR crop.
        If team_map is set, restricts candidates to champions on team_hint's team.
        Returns (champion_name, confidence) or (None, 0.0) if below threshold.
        """
        if not self.ready or self._transform is None:
            return None, 0.0

        try:
            img_rgb = cv2.cvtColor(crop_bgr, cv2.COLOR_BGR2RGB)
            tensor  = self._transform(img_rgb).unsqueeze(0).to(self._device)

            with torch.no_grad():
                logits = self._model(tensor)
                probs  = torch.softmax(logits, dim=1)[0]

            # If we know which team this detection belongs to, restrict to that
            # team's champions — this significantly reduces misclassifications
            if team_hint and self.team_map:
                team_champions = {
                    name for name, team in self.team_map.items()
                    if team == team_hint
                }
                valid_indices = [
                    i for i, name in enumerate(self.class_names)
                    if name in team_champions
                ]
                if valid_indices:
                    # Zero out other classes and renormalise
                    mask = torch.zeros_like(probs)
                    for i in valid_indices:
                        mask[i] = probs[i]
                    if mask.sum() > 0:
                        probs = mask / mask.sum()

            best_idx  = probs.argmax().item()
            best_conf = probs[best_idx].item()
            best_name = self.class_names[best_idx]

            if best_conf >= threshold:
                return best_name, best_conf

            return None, 0.0

        except Exception as e:
            print(f"[classifier] Inference error: {e}")
            return None, 0.0


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
        self.classifier  = None  # set in _load_templates
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
        # Build team map from champions list for team-restricted inference
        team_map = {p["name"]: p["team"] for p in self.champions if p.get("name")}

        # Try CNN classifier first
        self.classifier = ChampionClassifier(team_map=team_map)

        if self.classifier.ready:
            print("[tracker] Using CNN classifier for champion identification.")
            return

        # Fallback to template matching
        if not self.champions:
            print("[tracker] No champions provided — identity matching disabled.")
            print("[tracker] Will detect positions by team color only.")
            return
        print("[tracker] Falling back to template matching.")
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

                    if self.classifier and self.classifier.ready:
                        # CNN path — strip border and run inference
                        mm_h_px, mm_w_px = minimap.shape[:2]
                        x1 = max(0, x); y1 = max(0, y)
                        x2 = min(mm_w_px, x + w); y2 = min(mm_h_px, y + h)
                        crop = minimap[y1:y2, x1:x2]
                        crop_inner = _strip_border(crop) if crop.size > 0 else crop
                        name, score = self.classifier.predict(
                            crop_inner, team_hint=team_hint
                        )
                        team = team_hint  # team comes from HSV color detection
                    elif self.templates:
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


# ── Color sampler ─────────────────────────────────────────────────────────────

def run_color_sampler():
    """
    Interactive tool to measure the exact HSV values of champion icon borders
    on your minimap. Opens the current minimap capture and lets you click on
    the colored border of a champion icon. Saves the measured ranges to
    minimap_config.json so the tracker uses them automatically.

    Usage: python minimap_tracker.py --sample-colors

    Instructions:
      1. Have League open and spectating so the minimap is visible
      2. Run this command
      3. Click on the BLUE border of an ORDER champion icon (3+ clicks)
      4. Press N to switch to CHAOS
      5. Click on the RED border of a CHAOS champion icon (3+ clicks)
      6. Press S to save and exit
      7. Press Q to quit without saving
    """
    cfg    = load_config()
    region = get_minimap_region(cfg)

    wc = WindowCapture()
    window_relative = cfg.get("window_relative", False)
    if not window_relative:
        wc = None

    minimap = capture_minimap(region, window_capture=wc)
    if minimap is None:
        print("[sampler] Failed to capture minimap.")
        return

    # Enlarge for easier clicking
    scale  = 3
    display = cv2.resize(minimap, (minimap.shape[1] * scale, minimap.shape[0] * scale),
                         interpolation=cv2.INTER_LINEAR)
    hsv_full = cv2.cvtColor(minimap, cv2.COLOR_BGR2HSV)

    current_team = ["ORDER"]
    samples = {"ORDER": [], "CHAOS": []}

    def on_click(event, x, y, flags, param):
        if event != cv2.EVENT_LBUTTONDOWN:
            return
        # Map display coords back to minimap coords
        mx, my = x // scale, y // scale
        mx = min(mx, minimap.shape[1] - 1)
        my = min(my, minimap.shape[0] - 1)
        h_val, s_val, v_val = hsv_full[my, mx]
        team = current_team[0]
        samples[team].append((int(h_val), int(s_val), int(v_val)))
        b, g, r = minimap[my, mx]
        print(f"  [{team}] pixel ({mx},{my})  BGR=({b},{g},{r})  HSV=({h_val},{s_val},{v_val})")
        # Mark the click on display
        color = (255, 100, 0) if team == "ORDER" else (0, 100, 255)
        cv2.circle(display, (x, y), 4, color, -1)
        cv2.imshow(win_name, display)

    win_name = "Color Sampler -- click icon borders | N=switch team | S=save | Q=quit"
    cv2.namedWindow(win_name, cv2.WINDOW_NORMAL)
    cv2.resizeWindow(win_name, minimap.shape[1] * scale, minimap.shape[0] * scale)
    cv2.setMouseCallback(win_name, on_click)

    print("\n=== COLOR SAMPLER ===")
    print("Click on the BLUE border of ORDER champion icons.")
    print("Press N to switch to CHAOS (red borders), S to save, Q to quit.\n")

    while True:
        label = f"Sampling: {current_team[0]} ({len(samples[current_team[0]])} points) | N=switch S=save Q=quit"
        disp_copy = display.copy()
        cv2.putText(disp_copy, label, (10, 20),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.5,
                    (0, 255, 0), 1)
        cv2.imshow(win_name, disp_copy)

        key = cv2.waitKey(100) & 0xFF
        if key == ord("n") or key == ord("N"):
            current_team[0] = "CHAOS" if current_team[0] == "ORDER" else "ORDER"
            print(f"\nSwitched to {current_team[0]}")
        elif key == ord("s") or key == ord("S"):
            break
        elif key == ord("q") or key == ord("Q"):
            cv2.destroyAllWindows()
            print("Quit without saving.")
            return

    cv2.destroyAllWindows()

    # Compute ranges from samples with a tolerance margin
    MARGIN_H = 12   # hue tolerance
    MARGIN_S = 60   # saturation tolerance
    MARGIN_V = 60   # value tolerance

    color_ranges = {}
    for team, pts in samples.items():
        if not pts:
            print(f"[sampler] No samples for {team} — keeping default.")
            default = _DEFAULT_COLOR_RANGES[team]
            color_ranges[team] = default
            continue

        hs = [p[0] for p in pts]
        ss = [p[1] for p in pts]
        vs = [p[2] for p in pts]

        lower = [max(0,   min(hs) - MARGIN_H),
                 max(0,   min(ss) - MARGIN_S),
                 max(0,   min(vs) - MARGIN_V)]
        upper = [min(179, max(hs) + MARGIN_H),
                 255,
                 255]

        color_ranges[team] = {"lower": lower, "upper": upper}
        print(f"\n[{team}] {len(pts)} samples")
        print(f"  H range: {min(hs)}–{max(hs)}  →  lower H={lower[0]}, upper H={upper[0]}")
        print(f"  S range: {min(ss)}–{max(ss)}")
        print(f"  V range: {min(vs)}–{max(vs)}")
        print(f"  Saved:   lower={lower}  upper={upper}")

    cfg["color_ranges"] = color_ranges
    save_config(cfg)
    print("\nColor ranges saved to minimap_config.json.")
    print("Run --debug again to verify the masks now match champion icons correctly.")


# ── Debug mode ────────────────────────────────────────────────────────────────

def debug_once(folder, champions):
    """
    Capture one minimap frame and save a full diagnostic image set to debug/.
    Shows:
      - The raw minimap crop
      - The ORDER and CHAOS HSV masks
      - Each detected candidate crop + the best matching template + score
      - Console output of icon_size, region, and all per-candidate scores

    Run with: python minimap_tracker.py --debug --folder <folder>
    Then inspect the debug/ folder to understand what the tracker sees.
    """
    cfg        = load_config()
    region     = get_minimap_region(cfg)
    icon_size  = get_icon_size(cfg)

    debug_dir = os.path.join(folder, "debug")
    os.makedirs(debug_dir, exist_ok=True)

    print(f"\n=== DEBUG MODE ===")
    print(f"Region:    {region}")
    print(f"Icon size: {icon_size}px")
    print(f"Output:    {debug_dir}/\n")

    # ── Capture ──────────────────────────────────────────────────────────────
    wc = WindowCapture()
    window_relative = cfg.get("window_relative", False)
    if not window_relative:
        wc = None

    minimap = capture_minimap(region, window_capture=wc)
    if minimap is None:
        print("[debug] Failed to capture minimap.")
        return

    cv2.imwrite(os.path.join(debug_dir, "01_minimap_raw.png"), minimap)
    print(f"Saved: 01_minimap_raw.png  ({minimap.shape[1]}x{minimap.shape[0]}px)")

    # ── HSV masks ─────────────────────────────────────────────────────────────
    hsv = cv2.cvtColor(minimap, cv2.COLOR_BGR2HSV)
    for team, ranges in TEAM_COLOR_RANGES.items():
        mask = cv2.inRange(hsv, ranges["lower"], ranges["upper"])
        kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (3, 3))
        mask = cv2.dilate(mask, kernel, iterations=1)
        fname = f"02_mask_{team.lower()}.png"
        cv2.imwrite(os.path.join(debug_dir, fname), mask)
        pixel_count = int(mask.sum() / 255)
        print(f"Saved: {fname}  ({pixel_count} active pixels)")

    # ── Candidates ────────────────────────────────────────────────────────────
    candidates = find_champion_candidates(minimap, icon_size)
    merged     = _merge_nearby_candidates(candidates, min_dist=icon_size // 2)
    print(f"\nCandidates found: {len(candidates)}  after merge: {len(merged)}")

    # ── Templates ─────────────────────────────────────────────────────────────
    templates = {}
    if champions:
        templates = prepare_champion_templates(champions, icon_size)
    else:
        print("[debug] No champions provided — skipping template matching.")

    # ── Per-candidate diagnostics ─────────────────────────────────────────────
    annotated = minimap.copy()

    for i, (x, y, w, h, team_hint) in enumerate(merged):
        cx, cy = x + w // 2, y + h // 2

        # Draw bounding box on annotated minimap
        color = (255, 80, 80) if team_hint == "ORDER" else (80, 80, 255)
        cv2.rectangle(annotated, (x, y), (x + w, y + h), color, 1)
        cv2.putText(annotated, str(i), (x, y - 2),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.35, color, 1)

        # Save crop
        pad = 4
        mm_h, mm_w = minimap.shape[:2]
        x1, y1 = max(0, x - pad), max(0, y - pad)
        x2, y2 = min(mm_w, x + w + pad), min(mm_h, y + h + pad)
        crop = minimap[y1:y2, x1:x2]
        COMPARE_SIZE = 32
        crop_inner = _strip_border(crop) if crop.size > 0 else crop
        crop_resized = cv2.resize(crop_inner, (COMPARE_SIZE, COMPARE_SIZE)) if crop_inner.size > 0 else crop

        crop_fname = os.path.join(debug_dir, f"crop_{i:02d}_{team_hint}.png")
        # Save enlarged version for easy inspection
        crop_big = cv2.resize(crop_resized, (COMPARE_SIZE * 4, COMPARE_SIZE * 4),
                              interpolation=cv2.INTER_NEAREST)
        cv2.imwrite(crop_fname, crop_big)

        print(f"\nCandidate {i:02d} [{team_hint}] at pixel ({cx},{cy})  bbox ({w}x{h})")

        if not templates:
            print(f"  -> No templates to match against")
            continue

        # Score against every template and print all results
        scores = []
        for name, (team, template) in templates.items():
            result = cv2.matchTemplate(crop_resized, template, cv2.TM_CCOEFF_NORMED)
            _, score, _, _ = cv2.minMaxLoc(result)
            scores.append((score, name, team))

        scores.sort(reverse=True)
        best_score, best_name, best_team = scores[0]

        print(f"  Best match: {best_name} ({best_team})  score={best_score:.3f}")
        print(f"  Top 3 matches:")
        for score, name, team in scores[:3]:
            print(f"    {name:20s} {team:6s}  {score:.3f}")

        # Save best template alongside crop for visual comparison
        _, best_template = templates[best_name]
        template_big = cv2.resize(best_template, (COMPARE_SIZE * 4, COMPARE_SIZE * 4),
                                  interpolation=cv2.INTER_NEAREST)
        compare = np.hstack([crop_big, template_big])
        compare_fname = os.path.join(debug_dir, f"compare_{i:02d}_{best_name}_s{best_score:.2f}.png")
        cv2.imwrite(compare_fname, compare)

    cv2.imwrite(os.path.join(debug_dir, "03_annotated.png"), annotated)
    print(f"\nSaved: 03_annotated.png  (all {len(merged)} candidates boxed)")
    print(f"\n=== DEBUG COMPLETE ===")
    print(f"Inspect images in: {debug_dir}/")
    print(f"Key things to check:")
    print(f"  01_minimap_raw.png     — does the crop match your actual minimap?")
    print(f"  02_mask_order.png      — are champion icon borders showing as white blobs?")
    print(f"  02_mask_chaos.png      — same for red team")
    print(f"  03_annotated.png       — are bounding boxes on actual champion icons?")
    print(f"  compare_NN_*.png       — left=detected crop, right=template, scores printed above")


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
    parser.add_argument(
        "--debug", action="store_true",
        help="Capture one frame and save full diagnostic images to <folder>/debug/. "
             "Use this to tune icon size, HSV ranges, and template matching."
    )
    parser.add_argument(
        "--sample-colors", action="store_true",
        help="Interactive color sampler — click on champion icon borders to measure "
             "exact HSV values and save them to minimap_config.json."
    )
    args = parser.parse_args()

    if args.calibrate:
        run_calibration()
        return

    if args.sample_colors:
        run_color_sampler()
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

    if args.debug:
        debug_once(folder=args.folder or "debug_output", champions=champions)
        return

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