"""
Merged clip detection and Pixeldrain source identification.

Reddit fancam posters often upload:
  - clip1.mp4, clip2.mp4, clip3.mp4  ← individual segments
  - clip(3,1,2).mp4                  ← merged: segments 1,2,3 in that order
  - clip(1,2,3).mp4                  ← same concept

Also common:
  - fancam_pt1.mp4, fancam_pt2.mp4, fancam_full.mp4
  - 직캠_1.mp4, 직캠_2.mp4, 직캠_full.mp4

Source detection (Pixeldrain album):
  When a poster's source is on RedNote/TikTok (which can't be scraped reliably),
  they often upload the original source video to the same Pixeldrain album.
  We identify it by: largest file + no clip-markers in name + duration > 30s + has audio.
"""

from __future__ import annotations

import logging
import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Patterns
# ---------------------------------------------------------------------------

# clip(3,1,2) or clip(1) etc.
_PAREN_INDICES_RE = re.compile(r"\((\d+(?:,\s*\d+)+)\)", re.IGNORECASE)

# pt1 / pt2 / part1 / part2 (numbered part → individual)
_PT_NUM_RE = re.compile(r"(?:_|-|\s)?(?:pt|part)[\s_-]?(\d+)", re.IGNORECASE)

# _1 / _2 / -1 / -2 at end of stem (before extension) → individual
_SUFFIX_NUM_RE = re.compile(r"(?:_|-)(\d+)$", re.IGNORECASE)

# _full / -full / _complete / (full) / Merged / Combined → merged
_FULL_RE = re.compile(
    r"(?:_|-|\s|\()?(?:full|complete|merged|combined)(?:\))?",
    re.IGNORECASE,
)

# "Clip N" or "clip_N" or "clipN" pattern → individual (numbered clip)
_CLIP_NUM_RE = re.compile(r"\bclip[\s_-]?(\d+)\b", re.IGNORECASE)


# ---------------------------------------------------------------------------
# Dataclass
# ---------------------------------------------------------------------------

@dataclass
class MergedClipInfo:
    is_merged: bool
    component_indices: list[int] = field(default_factory=list)
    component_names: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def parse_clip_filename(filename: str) -> MergedClipInfo:
    """
    Inspect *filename* and return a MergedClipInfo describing whether it looks
    like a merged (multi-segment) clip and what segment indices it contains.

    Rules (in priority order):
      1. Parenthesised index list — e.g. ``clip(3,1,2).mp4``  → merged, indices=[3,1,2]
      2. ``_full`` / ``_complete`` suffix                       → merged, indices=[]
      3. ``pt1`` / ``part2`` numbered pattern                   → NOT merged (individual)
      4. ``_1`` / ``_2`` numeric suffix                         → NOT merged (individual)
      5. Anything else                                           → NOT merged
    """
    stem = Path(filename).stem

    # 1. Parenthesised index list → always merged
    m = _PAREN_INDICES_RE.search(stem)
    if m:
        indices = [int(x.strip()) for x in m.group(1).split(",")]
        return MergedClipInfo(is_merged=True, component_indices=indices)

    # 2. explicit merge keyword → merged
    if _FULL_RE.search(stem):
        return MergedClipInfo(is_merged=True)

    # 3. "Clip N" pattern → individual (numbered segment)
    if _CLIP_NUM_RE.search(stem):
        return MergedClipInfo(is_merged=False)

    # 4. pt1/part1 numbered → individual
    if _PT_NUM_RE.search(stem):
        return MergedClipInfo(is_merged=False)

    # 5. trailing _N / -N → individual
    if _SUFFIX_NUM_RE.search(stem):
        return MergedClipInfo(is_merged=False)

    # 5. fallback — not merged
    return MergedClipInfo(is_merged=False)


def find_merge_groups(filenames: list[str]) -> list[list[str]]:
    """
    Given a flat list of filenames, group them into merge families.

    Each group is a list of filenames that belong together:
      [individual_seg, individual_seg, ..., merged_file]

    Algorithm:
      - For each merged file (is_merged=True with component_indices), find matching
        individual files whose stem ends with those indices.
      - Merged files with no explicit indices are matched heuristically: they share
        the same alphabetic base as numbered individual files.
      - Ungrouped files that are individual segments (numbered) are paired with any
        merged file that shares their base.
      - Remaining files are each returned as a single-element group.

    Returns:
        List of groups, each group being a list of filenames.
    """
    parsed = {fn: parse_clip_filename(fn) for fn in filenames}

    # Bare trailing number pattern: "clip1" → base "clip", index 1
    _BARE_NUM_RE = re.compile(r"^(.*?)(\d+)$")

    # Build a set of base stems for heuristic matching
    # e.g. "clip1" → base "clip", index 1
    def _base_and_index(fn: str) -> tuple[str, int] | None:
        stem = Path(fn).stem
        # "Clip N" / "clip_N" explicit pattern (highest priority for named clips)
        m = _CLIP_NUM_RE.search(stem)
        if m:
            base = stem[: m.start()].rstrip("_- ")
            return base, int(m.group(1))
        # pt/part numbered
        m = _PT_NUM_RE.search(stem)
        if m:
            base = stem[: m.start()].rstrip("_- ")
            return base, int(m.group(1))
        # trailing _N or -N
        m = _SUFFIX_NUM_RE.search(stem)
        if m:
            base = stem[: m.start()]
            return base, int(m.group(1))
        # bare trailing number, e.g. "clip1"
        m = _BARE_NUM_RE.match(stem)
        if m and m.group(1):  # ensure there's a non-empty base
            return m.group(1), int(m.group(2))
        return None

    # Map base → list of individual filenames
    base_to_individuals: dict[str, list[str]] = {}
    for fn, info in parsed.items():
        if not info.is_merged:
            bi = _base_and_index(fn)
            if bi:
                base, _ = bi
                base_to_individuals.setdefault(base, []).append(fn)

    used: set[str] = set()
    groups: list[list[str]] = []

    for fn, info in parsed.items():
        if not info.is_merged or fn in used:
            continue

        group_members: list[str] = []

        if info.component_indices:
            # Try to find individual files by index suffix
            # Build a lookup: index → filename for all individuals
            idx_to_fn: dict[int, str] = {}
            for other_fn, other_info in parsed.items():
                if other_info.is_merged or other_fn in used:
                    continue
                bi = _base_and_index(other_fn)
                if bi:
                    _, idx = bi
                    idx_to_fn[idx] = other_fn

            for idx in info.component_indices:
                if idx in idx_to_fn:
                    member = idx_to_fn[idx]
                    group_members.append(member)
                    used.add(member)

        else:
            # Heuristic: find individuals sharing the same alphabetic base
            stem = Path(fn).stem
            # Strip merge keywords (full/complete/merged/combined) to get base
            base_candidate = _FULL_RE.sub("", stem).rstrip("_- ")
            if base_candidate in base_to_individuals:
                for member in base_to_individuals[base_candidate]:
                    if member not in used:
                        group_members.append(member)
                        used.add(member)

        group_members.append(fn)
        used.add(fn)
        groups.append(group_members)

    # Remaining ungrouped files → individual single-element groups
    for fn in filenames:
        if fn not in used:
            groups.append([fn])

    return groups


# ---------------------------------------------------------------------------
# Duration-based merge detection (for timestamp-named files)
# ---------------------------------------------------------------------------

def find_merge_by_duration(
    duration_map: dict[str, float],
    tolerance: float = 2.0,
) -> list[list[str]]:
    """
    Detect merged clips by comparing file durations.

    Logic: if one file's duration ≈ sum of all other files' durations,
    it is likely the merged/combined version.

    Args:
        duration_map: {filename: duration_seconds}
        tolerance:    Acceptable difference in seconds between merged duration
                      and sum of parts (accounts for re-encoding, padding).

    Returns:
        List of groups: [[part1, part2, ..., merged], ...]
        Files that don't fit any group are returned as single-element groups.

    Example:
        {
          "clip1.mp4": 7.3,
          "clip2.mp4": 3.7,
          "clip3.mp4": 2.8,
          "full.mov":  13.8,   # 7.3 + 3.7 + 2.8 = 13.8 ✓
        }
        → [["clip1.mp4", "clip2.mp4", "clip3.mp4", "full.mov"]]
    """
    if not duration_map:
        return []

    filenames = list(duration_map.keys())
    durations = {fn: duration_map[fn] for fn in filenames}
    total_duration = sum(durations.values())

    used: set[str] = set()
    groups: list[list[str]] = []

    # Sort by duration descending — largest file is most likely the merge
    sorted_by_dur = sorted(filenames, key=lambda f: durations[f], reverse=True)

    for candidate in sorted_by_dur:
        if candidate in used:
            continue

        candidate_dur = durations[candidate]
        others = [f for f in filenames if f != candidate and f not in used]

        if not others:
            break

        # Check if candidate ≈ sum of all others
        others_sum = sum(durations[f] for f in others)
        if abs(candidate_dur - others_sum) <= tolerance:
            # Full group: all others + this merged file
            group = others + [candidate]
            for f in group:
                used.add(f)
            groups.append(group)
            logger.info(
                f"Duration-based merge: {candidate} ({candidate_dur:.1f}s) "
                f"≈ sum of {len(others)} parts ({others_sum:.1f}s)"
            )
            continue

        # Check if candidate ≈ sum of any subset
        # (handles case where some individual clips are missing)
        best_subset = None
        best_diff = float("inf")
        # Only try subsets of reasonable size (avoid O(2^n))
        if len(others) <= 12:
            from itertools import combinations
            for r in range(2, len(others) + 1):
                for combo in combinations(others, r):
                    s = sum(durations[f] for f in combo)
                    diff = abs(candidate_dur - s)
                    if diff < best_diff and diff <= tolerance:
                        best_diff = diff
                        best_subset = list(combo)

        if best_subset:
            group = best_subset + [candidate]
            for f in group:
                used.add(f)
            groups.append(group)
            logger.info(
                f"Duration-based partial merge: {candidate} ({candidate_dur:.1f}s) "
                f"≈ {len(best_subset)} parts"
            )

    # Remaining files → single-element groups
    for fn in filenames:
        if fn not in used:
            groups.append([fn])

    return groups


# ---------------------------------------------------------------------------
# Pixeldrain source identification
# ---------------------------------------------------------------------------

# Patterns that indicate an INDIVIDUAL clip (not a source video)
_CLIP_MARKER_RE = re.compile(
    r"""
    (?:^|[\s_\-\(])          # word boundary
    (?:
        clip[\s_\-]?\d+       # "Clip 1", "clip_2", "clip3"
      | \d+[\s_\-]?clip       # "1clip"
      | pt[\s_\-]?\d+         # "pt1", "pt_2"
      | part[\s_\-]?\d+       # "part1"
      | [\-_]\d+$             # trailing "-1", "_2"
    )
    """,
    re.IGNORECASE | re.VERBOSE,
)

# Platforms whose source videos are commonly re-uploaded to Pixeldrain
_REDNOTE_TIKTOK_RE = re.compile(r"rednote|xhs|小红书|tiktok|douyin|抖音", re.IGNORECASE)


@dataclass
class PixeldrainFile:
    """Metadata for a single file in a Pixeldrain album."""
    file_id: str
    name: str
    size_bytes: int
    # populated after download / probing:
    duration_sec: float = 0.0
    has_audio: bool = True          # assume True until probed
    width: int = 0
    height: int = 0
    fps: float = 0.0


@dataclass
class SourceCandidateResult:
    """Result of `identify_source_in_album`."""
    file: Optional[PixeldrainFile]   # None if no candidate found
    reason: str                      # human-readable explanation


def _has_clip_marker(name: str) -> bool:
    """Return True if *name* looks like an individual clip (not a source)."""
    stem = Path(name).stem
    return bool(_CLIP_MARKER_RE.search(stem))


def identify_source_in_album(
    files: list[PixeldrainFile],
    source_platform: str = "",
    min_duration_sec: float = 20.0,
) -> SourceCandidateResult:
    """
    Given a Pixeldrain album file list, identify which file is likely the
    original source video.

    Candidate selection:
      - Exclude files that are merged compilations (parse_clip_filename().is_merged)
        OR have clip-markers in their name.
      - Among remaining candidates, pick the one with highest pixel count
        (width × height) if probed, otherwise fall back to largest file size.

    Rules applied to the winning candidate:
      3. duration_sec > min_duration_sec  (if probed; skip if 0.0).
      4. has_audio is True  (if probed; skip if not probed yet).

    Returns the candidate file, or None with a reason string.
    """
    if not files:
        return SourceCandidateResult(file=None, reason="empty album")

    # Filter out merged compilations and clip-indexed files
    candidates = [
        f for f in files
        if not parse_clip_filename(f.name).is_merged
        and not _has_clip_marker(f.name)
    ]

    if not candidates:
        return SourceCandidateResult(
            file=None,
            reason="all files are merged compilations or have clip-markers",
        )

    # Pick best candidate: highest resolution if probed, else largest file size
    def _score(f: PixeldrainFile) -> tuple:
        pixels = f.width * f.height  # 0 if not probed
        return (pixels, f.size_bytes)

    best = max(candidates, key=_score)

    # Rule 3: duration (only checked if probed)
    if best.duration_sec > 0 and best.duration_sec < min_duration_sec:
        return SourceCandidateResult(
            file=None,
            reason=(
                f"best candidate '{best.name}' duration "
                f"{best.duration_sec:.1f}s < {min_duration_sec}s"
            ),
        )

    # Rule 4: has audio (only checked if probed)
    if best.duration_sec > 0 and not best.has_audio:
        return SourceCandidateResult(
            file=None,
            reason=f"best candidate '{best.name}' has no audio track",
        )

    # Platform hint (informational only)
    if source_platform and not _REDNOTE_TIKTOK_RE.search(source_platform):
        logger.debug(
            "source_platform='%s' is not RedNote/TikTok; "
            "still checking if best candidate qualifies",
            source_platform,
        )

    reason = "highest resolution" if best.width > 0 else "largest + no clip-marker/merged"
    logger.info(
        "Source candidate identified: '%s' (%d MB, %dx%d)",
        best.name,
        best.size_bytes // (1024 * 1024),
        best.width, best.height,
    )
    return SourceCandidateResult(file=best, reason=reason)




def _check_has_audio(path: Path) -> bool:
    """
    Return True if the video file has at least one audio stream.
    Uses ffmpeg stderr (the same binary Pulsify relies on).
    Falls back to True (assume audio) if detection fails.
    """
    import shutil
    ffmpeg = shutil.which("ffmpeg") or "/home/kingy/.local/bin/ffmpeg"
    if not Path(ffmpeg).exists():
        return True  # can't check → assume yes
    try:
        proc = subprocess.run(
            [ffmpeg, "-hide_banner", "-i", str(path)],
            capture_output=True, text=True, timeout=15,
        )
        return bool(re.search(r"Stream.*Audio:", proc.stderr))
    except Exception:
        return True  # assume audio on error


def probe_video(path: Path) -> dict:
    """
    Probe *path* and return a dict with keys:
      duration_sec, has_audio, width, height, fps

    Delegates to ``pulsify.utils.video_info.get_video_info()`` for video
    stream metadata, then adds ``has_audio`` via a separate ffmpeg check.

    Returns empty dict on failure.
    """
    try:
        from pulsify.utils.video_info import get_video_info
        info = get_video_info(path)
    except Exception as exc:
        logger.warning("probe_video: get_video_info failed for %s: %s", path, exc)
        return {}

    if not info or info.get("duration_sec", 0) == 0:
        logger.warning("probe_video: no video info for %s", path)
        return {}

    info["has_audio"] = _check_has_audio(path)
    return info


def fill_probe_info(pf: PixeldrainFile, path: Path) -> None:
    """Probe *path* with ffprobe and populate *pf* fields in-place."""
    info = probe_video(path)
    if info:
        pf.duration_sec = info.get("duration_sec", 0.0)
        pf.has_audio    = info.get("has_audio", True)
        pf.width        = info.get("width", 0)
        pf.height       = info.get("height", 0)
        pf.fps          = info.get("fps", 0.0)


def better_quality(path_a: Path, path_b: Path) -> str:
    """
    Compare video quality (resolution × fps) of two files.

    Returns:
        "a"     — path_a is better or equal
        "b"     — path_b is better
        "equal" — same quality
    """
    info_a = probe_video(path_a)
    info_b = probe_video(path_b)

    def score(info: dict) -> float:
        w = info.get("width",  0)
        h = info.get("height", 0)
        f = info.get("fps",    0.0) or 30.0
        return w * h * f

    sa = score(info_a)
    sb = score(info_b)

    if sa == 0 and sb == 0:
        return "equal"
    # treat scores within 5% as equal (e.g. 59.97 vs 60.0 fps)
    if sa > 0 and sb > 0 and abs(sa - sb) / max(sa, sb) < 0.05:
        return "equal"
    return "a" if sa >= sb else "b"


# ---------------------------------------------------------------------------
# cv2 optional import (guarded — used only by zoom_detect)
# ---------------------------------------------------------------------------

try:
    import cv2 as _cv2
    _CV2_AVAILABLE = True
except ImportError:
    _cv2 = None  # type: ignore[assignment]
    _CV2_AVAILABLE = False


# ---------------------------------------------------------------------------
# Zoom detection
# ---------------------------------------------------------------------------

@dataclass
class ZoomDetectResult:
    """Result of ``zoom_detect``."""
    is_zoom_in: bool
    ratio_clip: float    # face_area / frame_area in clip (0.0 if no face detected)
    ratio_source: float  # face_area / frame_area in source (0.0 if no face detected)
    zoom_factor: float   # ratio_clip / ratio_source (1.0 if unknown)
    method: str          # "face" | "aspect_ratio" | "no_source"


def _grab_frame(path: Path, offset_sec: float = 0.0):
    """
    Grab a single frame from *path* at *offset_sec* using cv2.VideoCapture.

    Returns the frame as a numpy array (BGR), or None on failure.
    Requires cv2 to be available.
    """
    cap = _cv2.VideoCapture(str(path))
    try:
        if not cap.isOpened():
            logger.warning("zoom_detect: cannot open video %s", path)
            return None
        if offset_sec > 0.0:
            fps = cap.get(_cv2.CAP_PROP_FPS) or 25.0
            target_frame = int(offset_sec * fps)
            cap.set(_cv2.CAP_PROP_POS_FRAMES, target_frame)
        ok, frame = cap.read()
        if not ok or frame is None:
            logger.warning(
                "zoom_detect: failed to read frame at %.2fs from %s", offset_sec, path
            )
            return None
        return frame
    finally:
        cap.release()


def _largest_face_ratio(frame) -> float:
    """
    Detect faces in *frame* with Haar cascade and return the ratio of the
    largest face area to the total frame area.  Returns 0.0 if no face found.
    """
    gray = _cv2.cvtColor(frame, _cv2.COLOR_BGR2GRAY)
    classifier = _cv2.CascadeClassifier(
        _cv2.data.haarcascades + "haarcascade_frontalface_default.xml"
    )
    faces = classifier.detectMultiScale(
        gray,
        scaleFactor=1.1,
        minNeighbors=4,
        minSize=(30, 30),
    )
    if len(faces) == 0:
        return 0.0

    frame_h, frame_w = frame.shape[:2]
    frame_area = frame_w * frame_h
    if frame_area == 0:
        return 0.0

    largest_area = max(int(w) * int(h) for (_, _, w, h) in faces)
    return largest_area / frame_area


def zoom_detect(
    clip_path: Path,
    source_path: Optional[Path],
    align_offset_sec: float = 0.0,
    zoom_threshold: float = 1.5,
) -> ZoomDetectResult:
    """
    Determine whether *clip_path* is spatially zoomed-in relative to
    *source_path* (i.e. the subject/face appears larger in the clip), as
    opposed to merely being a temporal trim.

    Algorithm
    ---------
    1. Grab the first frame of *clip_path* and the frame at *align_offset_sec*
       from *source_path*.
    2. Run Haar-cascade face detection on both frames.
    3. Compute ``face_ratio = largest_face_area / frame_area`` for each.
    4. If both ratios are non-zero:
       ``zoom_factor = ratio_clip / ratio_source``
       ``is_zoom_in  = zoom_factor > zoom_threshold``   (method="face")
    5. Fallback (any frame has no detectable face):
       Compare aspect ratios — a significant AR difference (> 0.15) suggests
       cropping/zooming.  (method="aspect_ratio")
    6. If *source_path* is None, return ``is_zoom_in=False, method="no_source"``.

    Parameters
    ----------
    clip_path        : Path to the Pixeldrain clip file.
    source_path      : Path to the source video, or None.
    align_offset_sec : Time offset (seconds) at which to sample the source frame.
    zoom_threshold   : Minimum ratio_clip/ratio_source to call it a zoom-in.

    Returns
    -------
    ZoomDetectResult
    """
    if source_path is None:
        return ZoomDetectResult(
            is_zoom_in=False,
            ratio_clip=0.0,
            ratio_source=0.0,
            zoom_factor=1.0,
            method="no_source",
        )

    # --- Fallback: no cv2 available → use aspect-ratio heuristic only ----------
    if not _CV2_AVAILABLE:
        logger.debug("zoom_detect: cv2 not available, falling back to aspect_ratio")
        return _zoom_detect_ar(clip_path, source_path)

    # --- Grab frames -----------------------------------------------------------
    frame_clip = _grab_frame(clip_path, offset_sec=0.0)
    frame_src  = _grab_frame(source_path, offset_sec=align_offset_sec)

    if frame_clip is None or frame_src is None:
        logger.debug(
            "zoom_detect: could not grab frames (clip=%s, src=%s), "
            "falling back to aspect_ratio",
            frame_clip is None, frame_src is None,
        )
        return _zoom_detect_ar(clip_path, source_path)

    # --- Face ratios -----------------------------------------------------------
    ratio_clip   = _largest_face_ratio(frame_clip)
    ratio_source = _largest_face_ratio(frame_src)

    if ratio_clip > 0.0 and ratio_source > 0.0:
        zoom_factor = ratio_clip / ratio_source
        return ZoomDetectResult(
            is_zoom_in=zoom_factor > zoom_threshold,
            ratio_clip=ratio_clip,
            ratio_source=ratio_source,
            zoom_factor=zoom_factor,
            method="face",
        )

    # --- Fallback: at least one frame had no detected face ---------------------
    logger.debug(
        "zoom_detect: face not detected (ratio_clip=%.4f, ratio_source=%.4f), "
        "falling back to aspect_ratio",
        ratio_clip, ratio_source,
    )
    ar_result = _zoom_detect_ar_from_frames(frame_clip, frame_src)
    # Preserve the ratios we did manage to compute
    ar_result.ratio_clip   = ratio_clip
    ar_result.ratio_source = ratio_source
    return ar_result


def _zoom_detect_ar(clip_path: Path, source_path: Path) -> ZoomDetectResult:
    """
    Aspect-ratio fallback when cv2 is unavailable (no frame grabbing).
    Uses probe_video for width/height.
    """
    info_clip = probe_video(clip_path)
    info_src  = probe_video(source_path)

    clip_ar = _safe_ar(info_clip.get("width", 0), info_clip.get("height", 0))
    src_ar  = _safe_ar(info_src.get("width",  0), info_src.get("height",  0))

    is_zoom = abs(clip_ar - src_ar) > 0.15 if (clip_ar and src_ar) else False
    return ZoomDetectResult(
        is_zoom_in=is_zoom,
        ratio_clip=0.0,
        ratio_source=0.0,
        zoom_factor=1.0,
        method="aspect_ratio",
    )


def _zoom_detect_ar_from_frames(frame_clip, frame_src) -> ZoomDetectResult:
    """Aspect-ratio fallback computed directly from grabbed frames."""
    clip_h, clip_w = frame_clip.shape[:2]
    src_h,  src_w  = frame_src.shape[:2]

    clip_ar = _safe_ar(clip_w, clip_h)
    src_ar  = _safe_ar(src_w,  src_h)

    is_zoom = abs(clip_ar - src_ar) > 0.15 if (clip_ar and src_ar) else False
    return ZoomDetectResult(
        is_zoom_in=is_zoom,
        ratio_clip=0.0,
        ratio_source=0.0,
        zoom_factor=1.0,
        method="aspect_ratio",
    )


def _safe_ar(width: int, height: int) -> float:
    """Return width/height aspect ratio, or 0.0 if either dimension is zero."""
    return width / height if height > 0 else 0.0
