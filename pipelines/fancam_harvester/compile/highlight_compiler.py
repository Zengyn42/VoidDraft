"""
Highlight reel compiler — reuses Pulsify's Renderer + TimelineSegment.

Given a query (idol + song + date), retrieves matching clips from the
library and compiles them into a highlight video.

Pulsify reuse:
    Renderer          — FFmpeg concat + segment trimming
    TimelineSegment   — segment descriptor
    Timeline          — ordered segment list
"""

from __future__ import annotations

import json
import logging
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

# Pulsify bridge
_LIB = Path(__file__).parent.parent.parent.parent / "lib"
if str(_LIB) not in sys.path:
    sys.path.insert(0, str(_LIB))

try:
    from pulsify.merge.renderer import Renderer
    from pulsify.merge.timeline_builder import Timeline, TimelineSegment
    _PULSIFY_AVAILABLE = True
except ImportError:
    _PULSIFY_AVAILABLE = False
    logger.warning("Pulsify not available — highlight compilation disabled")


@dataclass
class HighlightQuery:
    group: Optional[str] = None     # e.g. "twice"
    idol: Optional[str] = None      # e.g. "tzuyu"
    song: Optional[str] = None      # e.g. "tt"
    date: Optional[str] = None      # YYYYMMDD or None = any
    max_duration: float = 180.0     # seconds
    output_name: str = "highlight"


def find_clips(library_dir: Path, query: HighlightQuery) -> list[Path]:
    """
    Walk library_dir and find clips matching the query.
    Path structure: {group}/{date}/{idol}/{song}_{ts}_{quality}.mp4
    """
    matches: list[Path] = []

    search_root = library_dir
    if query.group:
        g = query.group.lower().replace(" ", "_")
        search_root = library_dir / g
        if not search_root.exists():
            logger.warning(f"Group dir not found: {search_root}")
            return []

    for mp4 in search_root.rglob("*.mp4"):
        parts = mp4.parts
        # Quick string match against path
        path_str = str(mp4).lower()
        if query.idol and query.idol.lower().replace(" ", "_") not in path_str:
            continue
        if query.song and query.song.lower().replace(" ", "_") not in path_str:
            continue
        if query.date and query.date not in path_str:
            continue
        matches.append(mp4)

    # Sort: 4K > 1080p > others, then alphabetical
    def quality_key(p: Path) -> int:
        name = p.name.lower()
        if "4k" in name:
            return 0
        if "1080" in name:
            return 1
        if "720" in name:
            return 2
        return 3

    matches.sort(key=lambda p: (quality_key(p), p.name))
    logger.info(f"Found {len(matches)} clips for query: {query}")
    return matches


def compile_highlight(
    library_dir: Path,
    output_dir: Path,
    query: HighlightQuery,
    audio_path: Optional[str] = None,
) -> Optional[Path]:
    """
    Compile matching clips into a highlight reel.

    Args:
        library_dir:  Root of organised clip library.
        output_dir:   Where to write the output .mp4.
        query:        HighlightQuery filter.
        audio_path:   Optional background audio track (replaces clip audio).

    Returns:
        Path to output .mp4, or None if compilation failed.
    """
    if not _PULSIFY_AVAILABLE:
        logger.error("Pulsify unavailable — cannot compile highlight")
        return None

    clips = find_clips(library_dir, query)
    if not clips:
        logger.warning("No clips found for query")
        return None

    # Build Timeline from clips
    # TimelineSegment: beat_index, start_time, end_time, clip, clip_in, clip_out
    # We create a simple sequential timeline (no beat-syncing for now)
    import subprocess, re

    def _clip_duration(p: Path) -> float:
        result = subprocess.run(
            ["ffprobe", "-v", "quiet", "-print_format", "json",
             "-show_streams", str(p)],
            capture_output=True, text=True
        )
        try:
            streams = json.loads(result.stdout).get("streams", [])
            for s in streams:
                if s.get("codec_type") == "video":
                    return float(s.get("duration", 0))
        except Exception:
            pass
        return 0.0

    # Minimal ClipInfo-like namedtuple that Renderer expects
    from merge.clip_manager import ClipInfo

    segments: list[TimelineSegment] = []
    cursor = 0.0
    total = 0.0

    for clip_path in clips:
        dur = _clip_duration(clip_path)
        if dur <= 0:
            continue
        if total + dur > query.max_duration:
            dur = query.max_duration - total

        clip_info = ClipInfo(
            path=str(clip_path),
            name=clip_path.stem,
            score=1.0,
            start=0.0,
            end=dur,
            duration=dur,
        )
        seg = TimelineSegment(
            beat_index=len(segments),
            start_time=cursor,
            end_time=cursor + dur,
            clip=clip_info,
            clip_in=0.0,
            clip_out=dur,
        )
        segments.append(seg)
        cursor += dur
        total += dur

        if total >= query.max_duration:
            break

    if not segments:
        logger.warning("No valid segments built")
        return None

    timeline = Timeline(
        segments=segments,
        duration=cursor,
        target_audio_path=audio_path or "",
    )

    output_dir.mkdir(parents=True, exist_ok=True)
    output_path = output_dir / f"{query.output_name}.mp4"

    renderer = Renderer()
    success = renderer.render(
        timeline=timeline,
        output_path=str(output_path),
        progress_callback=lambda pct, msg: logger.info(f"Render {pct:.0%} — {msg}"),
    )

    if success and output_path.exists():
        logger.info(f"Highlight compiled: {output_path}")
        return output_path
    else:
        logger.error("Renderer returned failure")
        return None
