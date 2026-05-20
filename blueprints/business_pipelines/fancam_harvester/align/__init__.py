"""
Clip-to-source alignment.

Usage:
    from fancam_harvester.align import align_clip, AlignResult, AlignmentError

Dance classification
--------------------
Call `is_dance_clip(title, filename)` to decide whether alignment is worth
attempting. Non-dance content (vlogs, interviews, behind-the-scenes) has no
temporal correspondence with a stage source video, so alignment is skipped.

Alignment strategy
------------------
  1. Audio fingerprint (librosa chroma)  — for clips with audio
  2. Motion/pose (YOLO keypoints)        — for portrait fancams without audio
  3. Visual SSIM                         — last resort, rarely useful

Most portrait phone fancams end up `unmatched` — that is normal. They are
stored as independent clips in the library without a source offset.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path
from typing import Optional

from .audio_fingerprint import AlignResult, AlignmentError
from .audio_fingerprint import find_offset as _audio_find_offset
from .visual_match import find_offset as _visual_find_offset


# ---------------------------------------------------------------------------
# Dance classification
# ---------------------------------------------------------------------------

# Korean/English keywords indicating a stage performance (danceable content)
_DANCE_KEYWORDS_RE = re.compile(
    r"(직캠|fancam|fan\s*cam|무대|stage|쇼케이스|showcase|콘서트|concert"
    r"|공연|performance|음악방송|뮤직뱅크|music\s*bank|음중|음뱅|엠카|엠카운트다운"
    r"|inkigayo|인기가요|show\s*champion|쇼챔|엠뮤|뮤직쇼|뮤직 뱅크"
    r"|안무|choreograph|dance\s*(ver|practice|cover)"
    r"|뮤직뱅크|뮤직뱅|쇼케이스|쇼챔피언|쇼챔"
    r"|mcountdown|m\s*countdown|simply\s*k-?pop|the\s*show)",
    re.IGNORECASE,
)

# Keywords that strongly suggest NON-dance content (skip alignment entirely)
_NON_DANCE_KEYWORDS_RE = re.compile(
    r"(브이로그|vlog|인터뷰|interview|behind|비하인드|먹방|mukbang"
    r"|일상|daily|reaction|리액션|unboxing|언박싱|q&a|meet\s*&?\s*greet)",
    re.IGNORECASE,
)


def is_dance_clip(title: str = "", filename: str = "") -> bool:
    """
    Heuristic: return True if the clip is likely a dance/stage performance.

    Checks both post title and filename. Non-dance clips are skipped during
    alignment (stored as independent library clips without a source offset).
    """
    text = f"{title} {filename}"

    # Non-dance takes priority
    if _NON_DANCE_KEYWORDS_RE.search(text):
        return False

    # If explicit dance keyword found → dance
    if _DANCE_KEYWORDS_RE.search(text):
        return True

    # Default: assume dance for k-pop fancam context (conservative)
    # Most clips harvested from r/kpopfap ARE stage fancams
    return True


# ---------------------------------------------------------------------------
# Audio helper
# ---------------------------------------------------------------------------

def _has_audio(video_path: Path) -> bool:
    """Return True if the video file contains at least one audio stream."""
    result = subprocess.run(
        ["ffmpeg", "-hide_banner", "-i", str(video_path)],
        capture_output=True, text=True,
    )
    return "Audio:" in result.stderr


# ---------------------------------------------------------------------------
# Motion alignment (Pulsify delegate)
# ---------------------------------------------------------------------------

def _try_motion_align(
    source_video: Path,
    clip_video: Path,
    min_confidence: float = 0.40,
) -> AlignResult:
    """
    Attempt motion-based alignment via Pulsify's YOLO pose analyzer.
    Converts MotionAlignResult → AlignResult for a unified return type.
    Raises AlignmentError on failure.
    """
    try:
        from pulsify.align.motion_align import find_offset as _motion_find_offset, MotionAlignError
    except ImportError as e:
        raise AlignmentError(f"pulsify.align not available: {e}")

    try:
        r = _motion_find_offset(
            source_video, clip_video,
            min_confidence=min_confidence,
        )
        return AlignResult(offset_sec=r.offset_sec, confidence=r.confidence, method="motion")
    except Exception as e:
        raise AlignmentError(str(e))


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def align_clip(
    source_video: Path,
    clip_video: Path,
    strategy: str = "auto",
    title: str = "",
    audio_confidence_threshold: float = 0.6,
    motion_confidence_threshold: float = 0.40,
    visual_ssim_threshold: float = 0.85,
    visual_sample_frames: int = 5,
) -> AlignResult:
    """
    Align clip to source using the specified strategy.

    strategy:
        "audio"  — audio fingerprint only
        "motion" — YOLO motion/pose only
        "visual" — visual SSIM only
        "auto"   — audio first → motion → visual (skips motion for non-dance)

    title:
        Reddit post title (used for dance classification in "auto" mode).

    Raises AlignmentError if all attempted strategies fail.
    """
    import logging
    _log = logging.getLogger(__name__)

    clip_name = clip_video.name if hasattr(clip_video, "name") else str(clip_video)

    # ── audio ───────────────────────────────────────────────────────────────
    if strategy in ("audio", "auto"):
        if not _has_audio(clip_video):
            _log.info(f"{clip_name} has no audio — skipping audio alignment")
            if strategy == "audio":
                raise AlignmentError("Clip has no audio stream")
        else:
            try:
                return _audio_find_offset(
                    source_video, clip_video,
                    min_confidence=audio_confidence_threshold,
                )
            except AlignmentError as e:
                if strategy == "audio":
                    raise
                _log.warning(f"Audio alignment failed ({e}), trying next strategy...")

    # ── motion ──────────────────────────────────────────────────────────────
    if strategy in ("motion", "auto"):
        dance = is_dance_clip(title=title, filename=clip_name)
        if not dance:
            _log.info(f"{clip_name} classified as non-dance — skipping motion alignment")
            if strategy == "motion":
                raise AlignmentError("Non-dance clip, motion alignment skipped")
        else:
            try:
                return _try_motion_align(
                    source_video, clip_video,
                    min_confidence=motion_confidence_threshold,
                )
            except AlignmentError as e:
                if strategy == "motion":
                    raise
                _log.warning(f"Motion alignment failed ({e}), trying visual...")

    # ── visual ──────────────────────────────────────────────────────────────
    return _visual_find_offset(
        source_video, clip_video,
        n_samples=visual_sample_frames,
        ssim_threshold=visual_ssim_threshold,
    )


__all__ = ["align_clip", "is_dance_clip", "AlignResult", "AlignmentError"]
