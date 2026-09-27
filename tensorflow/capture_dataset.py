#!/usr/bin/env python3
"""
capture_dataset.py

Builds an image dataset (clear / snowy / blizzard, or whatever classes you
define) by downloading YouTube videos with yt-dlp and auto-sampling frames
from them, filtering out blurry and near-duplicate frames.

USAGE:
    1. Fill in the URLS dict below with a handful of YouTube links per class.
    2. Run: python3 capture_dataset.py
    3. Frames land in dataset/<class_name>/, capped at TARGET_PER_CLASS each.

Requires: pip install yt-dlp opencv-python-headless --break-system-packages
"""

import os
import sys
import cv2
import subprocess
import glob
import shutil

# ---------------------------------------------------------------------------
# CONFIG — edit this section
# ---------------------------------------------------------------------------

URLS = {
    "clear": [
         "https://www.youtube.com/watch?v=g7CYxspyvXQ",
         "https://www.youtube.com/watch?v=bEy3hcO9Hyc",
         "https://www.youtube.com/watch?v=FtrK-ykzr-I",
         "https://www.youtube.com/watch?v=76lbgTjO82M",
         "https://www.youtube.com/watch?v=rZAcx4o6H0A"
    ],
    "snowy": [
        "https://www.youtube.com/watch?v=wNooehBV2bQ",
        {"url": "https://www.youtube.com/watch?v=I_4muV__I4o", "start": "0:20"},
        "https://www.youtube.com/watch?v=YOpCi0jLpnY",
        "https://www.youtube.com/watch?v=qDwMTmTEaAY",
        "https://www.youtube.com/watch?v=XURyTJv8n3k",
    ],
    "blizzard": [
        {"url": "https://www.youtube.com/watch?v=lVhhSnFJXaE", "start": "4:00", "end": "5:30"},
        "https://www.youtube.com/shorts/ByqLbCbs310?feature=share",
        {"url": "https://www.youtube.com/watch?v=KnqTfcs5dwQ", "start": "0:39", "end": "3:40"},
        {"url": "https://www.youtube.com/watch?v=qGyHtUZAcDw", "start": "4:35", "end": "9:15"},
        "https://www.youtube.com/watch?v=iJnrqbYlO_Y",
    ],
}

TARGET_PER_CLASS = 250        # aim within your 200-300 range
FRAME_STRIDE_SEC = 1.0        # grab one candidate frame every N seconds
OUTPUT_SIZE = (640, 360)      # 16:9, matches Camera Module 3's native aspect ratio
BLUR_THRESHOLD = 60.0         # lower = more permissive (Laplacian variance)
# Per-class override: whiteout/blizzard footage is naturally low-contrast and
# will get wrongly rejected as "blurry" at the default threshold. Lower this
# a lot (or set to 0 to disable blur filtering) for that class specifically.
BLUR_THRESHOLD_OVERRIDES = {
    "blizzard": 15.0,
}
DUP_HIST_THRESHOLD = 0.97     # higher = stricter dedup (correlation)
VIDEO_DOWNLOAD_DIR = "raw_videos"
DATASET_DIR = "dataset"
YTDLP_FORMAT = "bestvideo[height<=480]"  # video-only: no audio, no merge, no ffmpeg needed here

# ---------------------------------------------------------------------------


def normalize_entry(entry):
    """Accepts a plain URL string or {"url", "start", "end"} dict; returns
    (url, start, end) with start/end as None if not specified."""
    if isinstance(entry, str):
        return entry, None, None
    return entry["url"], entry.get("start"), entry.get("end")


def download_video(url, out_dir, start=None, end=None):
    """Download a video (optionally just a time section) with yt-dlp."""
    os.makedirs(out_dir, exist_ok=True)

    # Wipe out_dir before every download instead of trying to parse a video
    # ID out of the URL (breaks on Shorts/youtu.be links) — we only ever
    # process one video at a time here, so a clean folder + "what's new
    # afterward" is far more robust than guessing filenames.
    for stale in glob.glob(os.path.join(out_dir, "*")):
        try:
            os.remove(stale)
        except OSError:
            pass

    out_template = os.path.join(out_dir, "%(id)s.%(ext)s")
    cmd = [
        sys.executable, "-m", "yt_dlp",
        "-f", YTDLP_FORMAT,
        "--no-playlist",
        "--newline",  # force progress onto real newlines so it's never swallowed by buffering
        "--socket-timeout", "30",   # give up fast on a stalled connection instead of hanging
        "--retries", "3",
        "--fragment-retries", "3",
        "-o", out_template,
    ]

    if start or end:
        # yt-dlp section syntax: "*START-END" (accepts H:MM:SS or seconds).
        # No --force-keyframes-at-cuts: that forces a full re-encode (very
        # slow, especially vp9) for frame-exact cuts we don't actually need.
        # Without it, yt-dlp trims at the nearest keyframe via stream copy,
        # off by at most a second or two — irrelevant for our purposes.
        section_start = start if start else "0"
        section_end = end if end else "inf"
        cmd += ["--download-sections", f"*{section_start}-{section_end}"]

    cmd.append(url)

    label = f"{url} [{start or '0'}-{end or 'end'}]" if (start or end) else url
    print(f"  Downloading {label} ...")
    try:
        # Stream yt-dlp's own output live instead of swallowing it, so you can
        # see download progress (or exactly where it stalls) in real time.
        result = subprocess.run(cmd, timeout=600)
    except subprocess.TimeoutExpired:
        print("  FAILED: download timed out after 10 minutes (stalled connection?)")
        return None
    if result.returncode != 0:
        print(f"  FAILED: yt-dlp exited with code {result.returncode}")
        return None

    # out_dir was empty before this call, so whatever's here now is what
    # yt-dlp just wrote. Exclude .part (incomplete) files and, if somehow
    # more than one real file landed, take the largest.
    matches = [
        f for f in glob.glob(os.path.join(out_dir, "*"))
        if not f.endswith(".part")
    ]
    if len(matches) > 1:
        print(f"  NOTE: multiple files produced, using largest: {matches}")
        matches.sort(key=os.path.getsize, reverse=True)
    return matches[0] if matches else None


def is_blurry(frame, threshold=BLUR_THRESHOLD):
    gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
    return cv2.Laplacian(gray, cv2.CV_64F).var() < threshold


def is_near_duplicate(frame, recent_hists, threshold=DUP_HIST_THRESHOLD):
    """Compare a frame's color histogram against the last few saved frames."""
    hsv = cv2.cvtColor(frame, cv2.COLOR_BGR2HSV)
    hist = cv2.calcHist([hsv], [0, 1], None, [50, 60], [0, 180, 0, 256])
    cv2.normalize(hist, hist)
    for prev_hist in recent_hists:
        similarity = cv2.compareHist(hist, prev_hist, cv2.HISTCMP_CORREL)
        if similarity > threshold:
            return True, hist
    return False, hist


def extract_frames(video_path, class_dir, class_name, start_index, target_total):
    """Sample frames from a video, filter blur/dupes, save until target hit."""
    blur_threshold = BLUR_THRESHOLD_OVERRIDES.get(class_name, BLUR_THRESHOLD)
    cap = cv2.VideoCapture(video_path)
    if not cap.isOpened():
        print(f"  Could not open {video_path}")
        return start_index

    fps = cap.get(cv2.CAP_PROP_FPS) or 30
    if fps <= 0 or fps != fps:  # NaN check
        print(f"  WARNING: unreliable FPS reading ({fps}) for {video_path}, assuming 30")
        fps = 30
    frame_interval = max(1, int(fps * FRAME_STRIDE_SEC))

    total_frames = cap.get(cv2.CAP_PROP_FRAME_COUNT)
    # Safety net: if grab() somehow keeps succeeding well past the video's
    # reported length (corrupt/odd container), bail instead of spinning forever.
    max_frame_num = int(total_frames * 1.5) if total_frames and total_frames > 0 else 200_000

    saved = start_index
    frame_num = 0
    recent_hists = []
    MAX_RECENT = 8
    HEARTBEAT_EVERY = 500  # print a liveness update every N frames scanned
    candidates = 0
    blurry_rejected = 0
    dup_rejected = 0

    while saved < target_total:
        if frame_num > max_frame_num:
            print(f"  WARNING: scanned {frame_num} frames without reaching target "
                  f"(video reports ~{int(total_frames)} frames) — bailing out on this video")
            break

        ret = cap.grab()
        if not ret:
            break

        if frame_num % HEARTBEAT_EVERY == 0 and frame_num > 0:
            print(f"    ...scanned {frame_num} frames, saved {saved} so far")

        if frame_num % frame_interval == 0:
            ret, frame = cap.retrieve()
            if not ret:
                frame_num += 1
                continue

            candidates += 1
            if is_blurry(frame, threshold=blur_threshold):
                blurry_rejected += 1
            else:
                dup, hist = is_near_duplicate(frame, recent_hists)
                if dup:
                    dup_rejected += 1
                else:
                    frame = cv2.resize(frame, OUTPUT_SIZE)
                    out_path = os.path.join(
                        class_dir, f"{class_name}_{saved:04d}.jpg"
                    )
                    cv2.imwrite(out_path, frame, [cv2.IMWRITE_JPEG_QUALITY, 90])
                    saved += 1
                    recent_hists.append(hist)
                    if len(recent_hists) > MAX_RECENT:
                        recent_hists.pop(0)
        frame_num += 1

    cap.release()

    gained = saved - start_index
    if candidates > 0 and gained < candidates * 0.5:
        # Low yield — tell the user WHY instead of leaving them to guess.
        print(f"    [{os.path.basename(video_path)}] {candidates} candidates: "
              f"{gained} saved, {blurry_rejected} rejected as blurry, "
              f"{dup_rejected} rejected as near-duplicate")

    return saved


def main():
    for class_name, urls in URLS.items():
        if not urls:
            print(f"[{class_name}] no URLs configured, skipping.")
            continue

        print(f"\n=== Class: {class_name} ===")
        class_dir = os.path.join(DATASET_DIR, class_name)
        os.makedirs(class_dir, exist_ok=True)
        video_dir = os.path.join(VIDEO_DOWNLOAD_DIR, class_name)

        saved_count = len(glob.glob(os.path.join(class_dir, "*.jpg")))

        # Give every video an even quota first, so a single long video can't
        # eat the whole class target before the others are even touched.
        per_video_quota = -(-TARGET_PER_CLASS // len(urls))  # ceil division
        shortfall_entries = []  # videos that hit their own quota and could give more later

        for entry in urls:
            if saved_count >= TARGET_PER_CLASS:
                print(f"  Target reached ({saved_count}/{TARGET_PER_CLASS}).")
                break

            url, start, end = normalize_entry(entry)
            video_path = download_video(url, video_dir, start, end)
            if not video_path:
                continue

            video_cap = min(saved_count + per_video_quota, TARGET_PER_CLASS)
            before = saved_count
            saved_count = extract_frames(
                video_path, class_dir, class_name, saved_count, video_cap
            )
            got = saved_count - before
            print(f"  +{got} frames (total {saved_count}/{TARGET_PER_CLASS})")

            if got >= per_video_quota:
                shortfall_entries.append(entry)  # likely has more frames available

            os.remove(video_path)  # save disk space between downloads

        # Top-off pass: if some videos were short (blurry/short clips) and we're
        # still under target, pull extra frames from videos that had headroom.
        for entry in shortfall_entries:
            if saved_count >= TARGET_PER_CLASS:
                break
            url, start, end = normalize_entry(entry)
            video_path = download_video(url, video_dir, start, end)
            if not video_path:
                continue
            before = saved_count
            saved_count = extract_frames(
                video_path, class_dir, class_name, saved_count, TARGET_PER_CLASS
            )
            print(f"  top-off +{saved_count - before} frames "
                  f"(total {saved_count}/{TARGET_PER_CLASS})")
            os.remove(video_path)

        print(f"[{class_name}] done: {saved_count} images saved to {class_dir}/")

    if os.path.isdir(VIDEO_DOWNLOAD_DIR):
        shutil.rmtree(VIDEO_DOWNLOAD_DIR, ignore_errors=True)

    print("\nAll done. Dataset layout:")
    for class_name in URLS:
        class_dir = os.path.join(DATASET_DIR, class_name)
        if os.path.isdir(class_dir):
            count = len(glob.glob(os.path.join(class_dir, "*.jpg")))
            print(f"  {class_dir}/  ({count} images)")


if __name__ == "__main__":
    main()