"""
label_crops.py
──────────────
Manual review tool for labeling champion icon crops collected by
collect_training_data.py.

Shows each unlabeled crop from raw_crops/ in a window. The suggested label
(from template matching) is shown — press Enter to confirm it, or type a
correction. Labeled crops are moved to training_data/<champion_name>/.

USAGE:
    python label_crops.py

CONTROLS:
    Enter          — accept the suggested label
    Type a name    — override the suggestion (tab-completes known champions)
    s              — skip this crop (leave it unlabeled for now)
    d              — delete this crop (bad quality, clear false positive)
    q              — quit and save progress

OUTPUT:
    training_data/
        <ChampionName>/
            <original_filename>

    skipped_crops/    — crops you pressed S on, reviewable later
    deleted_crops/    — crops you pressed D on, kept for reference

PROGRESS:
    Progress is saved after every crop so you can quit and resume at any time.
    Already-labeled crops are skipped automatically on restart.

DEPENDENCIES:
    pip install opencv-python numpy
"""

import cv2
import numpy as np
import os
import shutil
import json
import argparse
from pathlib import Path


# ── Settings ──────────────────────────────────────────────────────────────────

RAW_CROPS_DIR    = "raw_crops"
TRAINING_DIR     = "training_data"
SKIPPED_DIR      = "skipped_crops"
DELETED_DIR      = "deleted_crops"
PROGRESS_FILE    = "labeling_progress.json"

DISPLAY_SCALE    = 8     # enlarge 32x32 crops to 256x256 for visibility
FONT             = cv2.FONT_HERSHEY_SIMPLEX


# ── Progress tracking ─────────────────────────────────────────────────────────

def load_progress():
    if os.path.exists(PROGRESS_FILE):
        with open(PROGRESS_FILE) as f:
            return json.load(f)
    return {"labeled": [], "skipped": [], "deleted": []}


def save_progress(progress):
    with open(PROGRESS_FILE, "w") as f:
        json.dump(progress, f, indent=2)


# ── Champion name helpers ──────────────────────────────────────────────────────

def load_known_champions():
    """
    Build a set of known champion names from the training_data folder
    plus any names seen in raw_crops filenames.
    """
    known = set()

    if os.path.isdir(TRAINING_DIR):
        for d in os.listdir(TRAINING_DIR):
            if os.path.isdir(os.path.join(TRAINING_DIR, d)):
                known.add(d)

    if os.path.isdir(RAW_CROPS_DIR):
        for f in os.listdir(RAW_CROPS_DIR):
            parts = f.split("_")
            # Filename format: TEAM_suggestion_cXXX_mapx_mapy_ts.png
            if len(parts) >= 2:
                suggestion = parts[1]
                if suggestion != "unknown":
                    known.add(suggestion)

    return sorted(known)


def autocomplete(partial, known_champions):
    """Return first champion name starting with partial (case-insensitive)."""
    partial_lower = partial.lower()
    for name in known_champions:
        if name.lower().startswith(partial_lower):
            return name
    return None


def parse_suggestion_from_filename(fname):
    """Extract the template-match suggestion from a crop filename."""
    parts = Path(fname).stem.split("_")
    # Format: TEAM_ChampionName_cXXX_mapx_mapy_ts
    if len(parts) >= 2:
        return parts[1]
    return "unknown"


def parse_team_from_filename(fname):
    parts = Path(fname).stem.split("_")
    if parts:
        return parts[0]
    return "unknown"


# ── Display helpers ────────────────────────────────────────────────────────────

def make_display(crop_bgr, suggestion, current_input, total, idx, team):
    """
    Build the display frame shown to the user.
    Top: enlarged crop
    Bottom: suggestion, current input, controls
    """
    size = DISPLAY_SCALE * 32
    enlarged = cv2.resize(crop_bgr, (size, size), interpolation=cv2.INTER_NEAREST)

    # Color border by team
    border_color = (255, 100, 50) if team == "ORDER" else (50, 100, 255)
    enlarged = cv2.copyMakeBorder(enlarged, 4, 4, 4, 4,
                                  cv2.BORDER_CONSTANT, value=border_color)

    # Info panel below the crop
    panel_h = 160
    panel   = np.zeros((panel_h, enlarged.shape[1], 3), dtype=np.uint8)

    # Progress
    prog_text = f"{idx + 1} / {total}"
    cv2.putText(panel, prog_text, (10, 22), FONT, 0.55, (180, 180, 180), 1)

    # Team
    team_color = (255, 180, 100) if team == "ORDER" else (100, 100, 255)
    cv2.putText(panel, f"Team: {team}", (10, 48), FONT, 0.55, team_color, 1)

    # Suggestion
    sugg_color = (100, 220, 100) if suggestion != "unknown" else (120, 120, 120)
    cv2.putText(panel, f"Suggestion: {suggestion}", (10, 74),
                FONT, 0.55, sugg_color, 1)

    # Current input
    display_input = current_input + "|"
    cv2.putText(panel, f"Your input: {display_input}", (10, 100),
                FONT, 0.55, (220, 220, 50), 1)

    # Controls
    cv2.putText(panel, "Enter=confirm  2=skip  3=delete  4=quit  Tab=autocomplete",
                (10, 130), FONT, 0.42, (140, 140, 140), 1)

    return np.vstack([enlarged, panel])


# ── Main labeler ──────────────────────────────────────────────────────────────

def run_labeler():
    # Setup output directories
    for d in [TRAINING_DIR, SKIPPED_DIR, DELETED_DIR]:
        os.makedirs(d, exist_ok=True)

    progress       = load_progress()
    known_champs   = load_known_champions()
    done_files     = set(progress["labeled"] + progress["skipped"] + progress["deleted"])

    # Collect unlabeled crops
    if not os.path.isdir(RAW_CROPS_DIR):
        print(f"No raw_crops/ folder found. Run collect_training_data.py first.")
        return

    all_crops = sorted([
        f for f in os.listdir(RAW_CROPS_DIR)
        if f.endswith(".png") and f not in done_files
    ])

    if not all_crops:
        print("No unlabeled crops found. Either all are labeled or raw_crops/ is empty.")
        return

    print(f"\nFound {len(all_crops)} unlabeled crops.")
    print("Controls: Enter=confirm suggestion | type to override | S=skip | D=delete | Q=quit\n")

    win_name = "Champion Labeler"
    cv2.namedWindow(win_name, cv2.WINDOW_NORMAL)

    labeled_count  = 0
    skipped_count  = 0
    deleted_count  = 0
    current_input  = ""

    for idx, fname in enumerate(all_crops):
        fpath      = os.path.join(RAW_CROPS_DIR, fname)
        crop       = cv2.imread(fpath)

        if crop is None:
            progress["deleted"].append(fname)
            continue

        suggestion = parse_suggestion_from_filename(fname)
        team       = parse_team_from_filename(fname)
        current_input = ""

        while True:
            # Show autocomplete hint
            display_input = current_input
            if current_input:
                hint = autocomplete(current_input, known_champs)
                if hint and hint.lower() != current_input.lower():
                    display_input = current_input  # show raw input, hint shown below

            frame = make_display(crop, suggestion, display_input,
                                 len(all_crops), idx, team)
            cv2.imshow(win_name, frame)
            key = cv2.waitKey(0) & 0xFF

            if key == 13:  # Enter — confirm
                final_label = current_input.strip() if current_input.strip() else suggestion
                if final_label == "unknown" or not final_label:
                    # No label given and suggestion is unknown — treat as skip
                    print(f"  Skipped (no label): {fname}")
                    skipped_count += 1
                    progress["skipped"].append(fname)
                    dest_dir = SKIPPED_DIR
                    shutil.move(fpath, os.path.join(dest_dir, fname))
                else:
                    # Move to training_data/<label>/
                    label_dir = os.path.join(TRAINING_DIR, final_label)
                    os.makedirs(label_dir, exist_ok=True)
                    shutil.move(fpath, os.path.join(label_dir, fname))
                    progress["labeled"].append(fname)
                    labeled_count += 1
                    known_champs = load_known_champions()
                    if idx % 10 == 0:
                        print(f"  [{idx+1}/{len(all_crops)}] '{final_label}'  "
                              f"(labeled={labeled_count} skipped={skipped_count})")
                break

            elif key == ord("2"):  # 2 = Skip
                shutil.move(fpath, os.path.join(SKIPPED_DIR, fname))
                progress["skipped"].append(fname)
                skipped_count += 1
                current_input = ""
                break

            elif key == ord("3"):  # 3 = Delete
                shutil.move(fpath, os.path.join(DELETED_DIR, fname))
                progress["deleted"].append(fname)
                deleted_count += 1
                current_input = ""
                break

            elif key == ord("4"):  # 4 = Quit
                save_progress(progress)
                cv2.destroyAllWindows()
                print(f"\nSaved progress. Labeled={labeled_count} "
                      f"Skipped={skipped_count} Deleted={deleted_count}")
                return

            elif key == 8 or key == 127:  # Backspace
                current_input = current_input[:-1]

            elif key == 9:  # Tab — autocomplete
                hint = autocomplete(current_input, known_champs)
                if hint:
                    current_input = hint

            elif 32 <= key <= 126:  # Printable character
                current_input += chr(key)

        save_progress(progress)

    cv2.destroyAllWindows()

    print(f"\n=== Labeling complete ===")
    print(f"Labeled:  {labeled_count}")
    print(f"Skipped:  {skipped_count}")
    print(f"Deleted:  {deleted_count}")
    print(f"\nTraining data saved to: {TRAINING_DIR}/")
    print(f"Run train_classifier.py when you have enough data.")

    # Summary of classes collected
    if os.path.isdir(TRAINING_DIR):
        print(f"\nClasses collected:")
        total_crops = 0
        for champ in sorted(os.listdir(TRAINING_DIR)):
            champ_dir = os.path.join(TRAINING_DIR, champ)
            if os.path.isdir(champ_dir):
                count = len(os.listdir(champ_dir))
                total_crops += count
                print(f"  {champ:25s} {count} crops")
        print(f"\n  Total: {total_crops} labeled crops across "
              f"{len(os.listdir(TRAINING_DIR))} champions")


def main():
    global RAW_CROPS_DIR, TRAINING_DIR

    parser = argparse.ArgumentParser(
        description="Label champion icon crops for CNN training"
    )
    parser.add_argument(
        "--crops-dir", type=str, default=RAW_CROPS_DIR,
        help=f"Directory containing raw crops (default: {RAW_CROPS_DIR})"
    )
    parser.add_argument(
        "--output-dir", type=str, default=TRAINING_DIR,
        help=f"Output directory for labeled data (default: {TRAINING_DIR})"
    )
    args = parser.parse_args()

    RAW_CROPS_DIR = args.crops_dir
    TRAINING_DIR  = args.output_dir

    run_labeler()


if __name__ == "__main__":
    main()