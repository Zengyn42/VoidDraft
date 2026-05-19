"""
Organise processed clips into the library directory structure.

Library layout:
    library/
        {group}/
            {YYYYMMDD}/
                {idol}/           ← single-idol frame
                    {song}_{ts}_{quality}.mp4
                group/            ← multi-idol frame
                    {song}_{ts}_{quality}.mp4

    unidentified/
        {reddit_post_id}/
            {clip_filename}

Quality string: e.g. "4K60fps" | "1080p30fps" | "720p"
"""

from __future__ import annotations

import logging
import re
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)


@dataclass
class StoredRecord:
    clip_id: str
    final_path: str        # absolute path in library or unidentified dir
    category: str          # "identified" | "unidentified"
    group: Optional[str] = None
    idol: Optional[str] = None
    song: Optional[str] = None
    performance_date: Optional[str] = None


def _quality_tag(video_path: Path) -> str:
    """
    Extract quality tag from video via ffprobe.
    Returns e.g. "4K60fps", "1080p30fps", "720p".
    """
    import subprocess, json
    result = subprocess.run(
        ["ffprobe", "-v", "quiet", "-print_format", "json",
         "-show_streams", str(video_path)],
        capture_output=True, text=True,
    )
    try:
        streams = json.loads(result.stdout).get("streams", [])
        for s in streams:
            if s.get("codec_type") == "video":
                h = int(s.get("height", 0))
                fps_str = s.get("r_frame_rate", "0/1")
                num, den = fps_str.split("/")
                fps = int(round(float(num) / float(den))) if float(den) else 0

                res = (
                    "4K" if h >= 2160
                    else "1080p" if h >= 1080
                    else "720p" if h >= 720
                    else f"{h}p"
                )
                fps_tag = f"{fps}fps" if fps else ""
                return f"{res}{fps_tag}"
    except Exception:
        pass
    return "unknown"


def _safe_name(s: str) -> str:
    """Make a string safe for filesystem use."""
    return re.sub(r"[^\w가-힣\-]", "_", s).strip("_")


def store_clip(
    clip_path: Path,
    library_dir: Path,
    unidentified_dir: Path,
    clip_id: str,
    post_id: str,
    identity,   # IdentityRecord
    align_offset_sec: Optional[float] = None,
) -> StoredRecord:
    """
    Move/copy a clip to its final destination based on identity.

    Args:
        clip_path:        Source clip file (best-quality version).
        library_dir:      Root of the organised library.
        unidentified_dir: Root of the unidentified holding area.
        clip_id:          Unique clip identifier.
        post_id:          Reddit post ID (used for unidentified dir).
        identity:         IdentityRecord from the LLM identify node.
        align_offset_sec: Timestamp in source video (for filename).

    Returns:
        StoredRecord describing the final location.
    """
    quality = _quality_tag(clip_path)

    if not identity.is_identified or identity.confidence < 0.0:
        # → unidentified
        dest_dir = unidentified_dir / _safe_name(post_id)
        dest_dir.mkdir(parents=True, exist_ok=True)
        dest_path = dest_dir / clip_path.name
        shutil.copy2(str(clip_path), str(dest_path))
        logger.info(f"Stored (unidentified): {dest_path}")
        return StoredRecord(
            clip_id=clip_id,
            final_path=str(dest_path),
            category="unidentified",
        )

    # Build library path
    parts = identity.storage_path_parts()  # [group, date, idol_or_group]
    dest_dir = library_dir
    for p in parts:
        dest_dir = dest_dir / _safe_name(p)
    dest_dir.mkdir(parents=True, exist_ok=True)

    # Filename: {song}_{timestamp}_{quality}.mp4
    ts_tag = ""
    if align_offset_sec is not None:
        minutes = int(align_offset_sec // 60)
        seconds = int(align_offset_sec % 60)
        ts_tag = f"_{minutes:02d}m{seconds:02d}s"

    song_tag = _safe_name(identity.song or "unknown_song")
    filename = f"{song_tag}{ts_tag}_{quality}.mp4"

    # Avoid collisions
    candidate = dest_dir / filename
    n = 1
    while candidate.exists():
        candidate = dest_dir / f"{song_tag}{ts_tag}_{quality}_{n}.mp4"
        n += 1

    shutil.copy2(str(clip_path), str(candidate))
    logger.info(f"Stored (identified): {candidate}")

    return StoredRecord(
        clip_id=clip_id,
        final_path=str(candidate),
        category="identified",
        group=identity.group,
        idol=identity.idol,
        song=identity.song,
        performance_date=identity.performance_date,
    )
