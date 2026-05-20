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

    Rules (all must pass):
      1. Largest file by size_bytes in the album.
      2. No clip-marker in filename ("Clip 1", "pt2", "_1", etc.).
      3. duration_sec > min_duration_sec  (if probed; skip if 0.0).
      4. has_audio is True  (if probed; skip if not probed yet).
      5. source_platform matches RedNote/TikTok  (if provided; skip if empty).

    Returns the candidate file, or None with a reason string.
    """
    if not files:
        return SourceCandidateResult(file=None, reason="empty album")

    # Rule 1: largest file
    largest = max(files, key=lambda f: f.size_bytes)

    # Rule 2: no clip marker in name
    if _has_clip_marker(largest.name):
        return SourceCandidateResult(
            file=None,
            reason=f"largest file '{largest.name}' has clip-marker in name",
        )

    # Rule 3: duration (only checked if probed)
    if largest.duration_sec > 0 and largest.duration_sec < min_duration_sec:
        return SourceCandidateResult(
            file=None,
            reason=(
                f"largest file '{largest.name}' duration "
                f"{largest.duration_sec:.1f}s < {min_duration_sec}s"
            ),
        )

    # Rule 4: has audio (only checked if probed)
    if largest.duration_sec > 0 and not largest.has_audio:
        return SourceCandidateResult(
            file=None,
            reason=f"largest file '{largest.name}' has no audio track",
        )

    # Rule 5: platform hint (optional)
    if source_platform and not _REDNOTE_TIKTOK_RE.search(source_platform):
        # Platform is specified but is NOT RedNote/TikTok — less confident
        logger.debug(
            "source_platform='%s' is not RedNote/TikTok; "
            "still checking if largest file qualifies",
            source_platform,
        )

    logger.info(
        "Source candidate identified: '%s' (%d MB)",
        largest.name,
        largest.size_bytes // (1024 * 1024),
    )
    return SourceCandidateResult(file=largest, reason="largest + no clip-marker")




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
