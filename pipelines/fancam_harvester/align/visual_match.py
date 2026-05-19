"""
Visual frame matching — fallback alignment when clip has no audio.

Strategy:
  1. Sample N frames from the clip (first, last, and evenly-spaced middle)
  2. For each sample frame, slide over the source video, compute SSIM
  3. Aggregate votes across sample frames → best offset

Dependencies:
    pip install opencv-python scikit-image
    ffmpeg on PATH
"""

from __future__ import annotations

import logging
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import cv2
import numpy as np

from .audio_fingerprint import AlignResult, AlignmentError

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _video_duration(video_path: Path) -> float:
    """Return duration in seconds using ffprobe."""
    import json
    result = subprocess.run(
        ["ffprobe", "-v", "quiet", "-print_format", "json",
         "-show_streams", str(video_path)],
        capture_output=True, text=True
    )
    try:
        data = json.loads(result.stdout)
        for stream in data.get("streams", []):
            if "duration" in stream:
                return float(stream["duration"])
    except Exception:
        pass
    return 60.0  # fallback


def _extract_frame(video_path: Path, timestamp: float) -> Optional[np.ndarray]:
    """Extract a single BGR frame at `timestamp` seconds."""
    cap = cv2.VideoCapture(str(video_path))
    cap.set(cv2.CAP_PROP_POS_MSEC, timestamp * 1000)
    ok, frame = cap.read()
    cap.release()
    return frame if ok else None


def _ssim_score(a: np.ndarray, b: np.ndarray, size: int = 128) -> float:
    """Resize both to `size`x`size` grayscale and compute SSIM."""
    from skimage.metrics import structural_similarity as ssim
    a_gray = cv2.cvtColor(cv2.resize(a, (size, size)), cv2.COLOR_BGR2GRAY)
    b_gray = cv2.cvtColor(cv2.resize(b, (size, size)), cv2.COLOR_BGR2GRAY)
    score, _ = ssim(a_gray, b_gray, full=True)
    return float(score)


def _sample_clip_frames(
    clip_path: Path,
    n_samples: int,
) -> list[tuple[float, np.ndarray]]:
    """
    Sample `n_samples` frames from clip.
    Returns list of (relative_timestamp, frame).
    """
    duration = _video_duration(clip_path)
    if n_samples <= 1:
        ts_list = [duration / 2]
    else:
        ts_list = [i * duration / (n_samples - 1) for i in range(n_samples)]

    frames = []
    for ts in ts_list:
        frame = _extract_frame(clip_path, ts)
        if frame is not None:
            frames.append((ts, frame))
    return frames


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def find_offset(
    source_video: Path,
    clip_video: Path,
    step_sec: float = 1.0,
    n_samples: int = 5,
    ssim_threshold: float = 0.80,
    min_confidence: float = 0.5,
) -> AlignResult:
    """
    Find where `clip_video` starts in `source_video` using frame-level SSIM.

    Args:
        source_video:   Full source video.
        clip_video:     Short clip to locate.
        step_sec:       Search step in seconds.
        n_samples:      Number of frames sampled from clip.
        ssim_threshold: SSIM score considered a "match" for one frame.
        min_confidence: Fraction of sample frames that must match.

    Returns:
        AlignResult with method="visual".

    Raises:
        AlignmentError: if confidence below threshold or deps missing.
    """
    try:
        import cv2  # noqa — just check availability
        from skimage.metrics import structural_similarity  # noqa
    except ImportError as e:
        raise AlignmentError(f"Visual match deps missing: {e}")

    clip_duration = _video_duration(clip_video)
    source_duration = _video_duration(source_video)
    sample_frames = _sample_clip_frames(clip_video, n_samples)

    if not sample_frames:
        raise AlignmentError("Could not extract any frames from clip")

    logger.debug(
        f"Visual match: {clip_video.name} ({clip_duration:.1f}s) in "
        f"{source_video.name} ({source_duration:.1f}s), "
        f"{len(sample_frames)} sample frames, step={step_sec}s"
    )

    # For each candidate offset, score how many sample frames match
    best_offset = 0.0
    best_score = 0.0

    offset = 0.0
    while offset + clip_duration <= source_duration:
        match_count = 0
        total_ssim = 0.0

        for rel_ts, clip_frame in sample_frames:
            source_ts = offset + rel_ts
            src_frame = _extract_frame(source_video, source_ts)
            if src_frame is None:
                continue
            s = _ssim_score(clip_frame, src_frame)
            total_ssim += s
            if s >= ssim_threshold:
                match_count += 1

        confidence = match_count / len(sample_frames)
        avg_ssim = total_ssim / len(sample_frames)

        if avg_ssim > best_score:
            best_score = avg_ssim
            best_offset = offset
            logger.debug(f"  offset={offset:.1f}s ssim={avg_ssim:.3f} matches={match_count}/{len(sample_frames)}")

        if confidence >= min_confidence and avg_ssim > 0.90:
            break  # early exit on confident match

        offset += step_sec

    final_confidence = best_score  # use avg SSIM as confidence proxy
    if final_confidence < min_confidence:
        raise AlignmentError(
            f"Visual match confidence {final_confidence:.3f} below threshold {min_confidence}"
        )

    logger.info(
        f"Visual aligned: {clip_video.name} → offset={best_offset:.2f}s "
        f"confidence={final_confidence:.3f}"
    )
    return AlignResult(
        offset_sec=best_offset,
        confidence=final_confidence,
        method="visual",
    )
