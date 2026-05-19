"""
Audio fingerprint alignment — find where a clip appears in the source video.

Strategy:
  1. Extract audio from both source and clip (FFmpeg → WAV)
  2. Generate chromaprint fingerprints
  3. Slide a window of clip-length over the source, compute fingerprint
     similarity at each position
  4. Return the offset (seconds) with the highest similarity

Dependencies:
    pip install chromaprint-python  # or fpcalc binary on PATH
    ffmpeg on PATH

Fallback: if chromaprint is unavailable, raises AlignmentError so
          visual_match.py can be tried instead.
"""

from __future__ import annotations

import json
import logging
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)


class AlignmentError(Exception):
    """Raised when fingerprint alignment fails or is unavailable."""


@dataclass
class AlignResult:
    offset_sec: float       # clip starts at this timestamp in the source
    confidence: float       # 0.0–1.0
    method: str = "audio"   # "audio" | "visual"


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _extract_audio(video_path: Path, out_wav: Path, duration: Optional[float] = None) -> None:
    """Extract mono 22050 Hz WAV from video using FFmpeg."""
    cmd = [
        "ffmpeg", "-y", "-i", str(video_path),
        "-vn", "-ac", "1", "-ar", "22050",
        "-f", "wav",
    ]
    if duration:
        cmd += ["-t", str(duration)]
    cmd.append(str(out_wav))
    result = subprocess.run(cmd, capture_output=True)
    if result.returncode != 0:
        raise AlignmentError(f"ffmpeg audio extract failed: {result.stderr.decode()[:200]}")


def _get_fingerprint(wav_path: Path) -> list[int]:
    """
    Run fpcalc (chromaprint CLI) and return the raw fingerprint as int list.
    fpcalc must be installed: sudo apt install libchromaprint-tools
    """
    result = subprocess.run(
        ["fpcalc", "-raw", "-json", str(wav_path)],
        capture_output=True, text=True
    )
    if result.returncode != 0:
        raise AlignmentError(
            f"fpcalc failed (is chromaprint-tools installed?): {result.stderr[:200]}"
        )
    data = json.loads(result.stdout)
    return data["fingerprint"]


def _bit_error_rate(fp_a: list[int], fp_b: list[int]) -> float:
    """
    Compute bit error rate between two fingerprints of the same length.
    Returns 0.0 (identical) … 1.0 (completely different).
    """
    length = min(len(fp_a), len(fp_b))
    if length == 0:
        return 1.0
    diff_bits = sum(bin(a ^ b).count("1") for a, b in zip(fp_a[:length], fp_b[:length]))
    return diff_bits / (length * 32)


def _similarity(fp_a: list[int], fp_b: list[int]) -> float:
    return 1.0 - _bit_error_rate(fp_a, fp_b)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def find_offset(
    source_video: Path,
    clip_video: Path,
    step_sec: float = 2.0,
    min_confidence: float = 0.5,
) -> AlignResult:
    """
    Find where `clip_video` starts inside `source_video` by audio fingerprinting.

    Args:
        source_video: Full concert/fancam video.
        clip_video:   Short clip extracted from the source.
        step_sec:     Sliding-window step in seconds.
        min_confidence: If best match is below this, raise AlignmentError.

    Returns:
        AlignResult with offset_sec and confidence.

    Raises:
        AlignmentError: if chromaprint tools unavailable or confidence too low.
    """
    import shutil
    if not shutil.which("fpcalc"):
        raise AlignmentError("fpcalc binary not found — install chromaprint-tools")

    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)

        # Extract full source audio + clip audio
        source_wav = tmp / "source.wav"
        clip_wav = tmp / "clip.wav"
        _extract_audio(source_video, source_wav)
        _extract_audio(clip_video, clip_wav)

        clip_fp = _get_fingerprint(clip_wav)
        clip_len_fp = len(clip_fp)

        if clip_len_fp == 0:
            raise AlignmentError("Clip fingerprint is empty — clip may have no audio")

        # Get clip duration from fingerprint length
        # fpcalc encodes ~8.27ms per fingerprint element (chromaprint default)
        fp_frame_sec = 8.27e-3
        clip_duration = clip_len_fp * fp_frame_sec

        logger.debug(
            f"Source: {source_video.name}, Clip: {clip_video.name}, "
            f"clip_duration≈{clip_duration:.1f}s, step={step_sec}s"
        )

        # Slide window over source audio in `step_sec` chunks
        best_offset = 0.0
        best_sim = 0.0

        # Get source duration
        probe = subprocess.run(
            ["ffprobe", "-v", "quiet", "-print_format", "json",
             "-show_streams", str(source_video)],
            capture_output=True, text=True
        )
        import re
        dur_match = re.search(r'"duration":\s*"([0-9.]+)"', probe.stdout)
        source_duration = float(dur_match.group(1)) if dur_match else 3600.0

        offset = 0.0
        while offset + clip_duration <= source_duration:
            window_wav = tmp / f"window_{int(offset)}.wav"
            _extract_audio(source_video, window_wav, duration=clip_duration + step_sec)
            try:
                window_fp = _get_fingerprint(window_wav)
            except AlignmentError:
                offset += step_sec
                continue

            # Trim window fingerprint to clip length for comparison
            sim = _similarity(clip_fp, window_fp[:clip_len_fp])
            if sim > best_sim:
                best_sim = sim
                best_offset = offset
                logger.debug(f"  offset={offset:.1f}s sim={sim:.3f}")

            # Early exit if very high confidence
            if sim > 0.95:
                break

            offset += step_sec
            # Clean up intermediate wav to save disk
            window_wav.unlink(missing_ok=True)

        if best_sim < min_confidence:
            raise AlignmentError(
                f"Best audio similarity {best_sim:.3f} below threshold {min_confidence}"
            )

        logger.info(
            f"Audio aligned: {clip_video.name} → offset={best_offset:.2f}s "
            f"confidence={best_sim:.3f}"
        )
        return AlignResult(offset_sec=best_offset, confidence=best_sim, method="audio")
