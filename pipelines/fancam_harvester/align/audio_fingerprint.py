"""
Audio-based clip-to-source alignment via librosa chroma features.

Strategy:
  1. Extract audio from both source and clip (FFmpeg → WAV mono 22050Hz)
  2. Compute chroma features (12-bin pitch class profiles) for both
  3. Slide a window of clip-length over the source chroma matrix
  4. Cross-correlate each window with clip chroma → find best offset

Does NOT require fpcalc / libchromaprint — pure Python via librosa.

Dependencies:
    pip install librosa
    ffmpeg on PATH
"""

from __future__ import annotations

import logging
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import numpy as np

logger = logging.getLogger(__name__)


class AlignmentError(Exception):
    """Raised when audio alignment fails or confidence is too low."""


@dataclass
class AlignResult:
    offset_sec: float       # clip starts at this timestamp in the source
    confidence: float       # 0.0–1.0
    method: str = "audio"   # "audio" | "visual"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _extract_wav(video_path: Path, out_wav: Path, sr: int = 22050) -> None:
    """Extract mono WAV at `sr` Hz from video using FFmpeg."""
    result = subprocess.run([
        "ffmpeg", "-y", "-i", str(video_path),
        "-vn", "-ac", "1", "-ar", str(sr),
        "-f", "wav", str(out_wav)
    ], capture_output=True)
    if result.returncode != 0:
        raise AlignmentError(
            f"ffmpeg audio extract failed: {result.stderr.decode()[:200]}"
        )


def _chroma(audio: np.ndarray, sr: int, hop_length: int = 512) -> np.ndarray:
    """Compute normalised chroma feature matrix (12 x T)."""
    import librosa
    c = librosa.feature.chroma_cqt(y=audio, sr=sr, hop_length=hop_length)
    # Normalise each frame to unit L2 norm
    norms = np.linalg.norm(c, axis=0, keepdims=True)
    norms[norms == 0] = 1
    return c / norms


def _chroma_similarity(a: np.ndarray, b: np.ndarray) -> float:
    """
    Mean frame-wise cosine similarity between two chroma matrices.
    Both must have the same number of columns (time frames).
    """
    min_t = min(a.shape[1], b.shape[1])
    if min_t == 0:
        return 0.0
    dots = np.sum(a[:, :min_t] * b[:, :min_t], axis=0)  # (T,)
    return float(np.mean(dots))


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def find_offset(
    source_video: Path,
    clip_video: Path,
    step_sec: float = 2.0,
    min_confidence: float = 0.5,
    sr: int = 22050,
    hop_length: int = 512,
    clip_speed_factor: float = 1.0,
) -> AlignResult:
    """
    Find where `clip_video` starts inside `source_video` using chroma features.

    Args:
        source_video:   Full concert/fancam video.
        clip_video:     Short clip to locate.
        step_sec:       Sliding window step in seconds.
        min_confidence: If best similarity < this, raise AlignmentError.
        sr:             Sample rate for audio extraction.
        hop_length:     Hop length for chroma computation.

    Returns:
        AlignResult(offset_sec, confidence, method="audio")

    Raises:
        AlignmentError: if no audio or confidence below threshold.
    """
    import librosa

    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        src_wav = tmp / "source.wav"
        clip_wav = tmp / "clip.wav"

        _extract_wav(source_video, src_wav, sr)
        _extract_wav(clip_video, clip_wav, sr)

        src_audio, _ = librosa.load(str(src_wav), sr=sr, mono=True)
        clip_audio, _ = librosa.load(str(clip_wav), sr=sr, mono=True)

    if len(clip_audio) < sr:
        raise AlignmentError("Clip audio too short (< 1s) for fingerprinting")

    # Speed factor: resample clip audio to simulate faster playback
    # clip_speed_factor=2.0 → treat clip as if played at 2× speed
    if clip_speed_factor != 1.0:
        import librosa
        target_len = int(len(clip_audio) / clip_speed_factor)
        clip_audio = librosa.resample(clip_audio, orig_sr=sr,
                                      target_sr=int(sr * clip_speed_factor))
        clip_audio = clip_audio[:target_len] if len(clip_audio) > target_len else clip_audio

    src_chroma = _chroma(src_audio, sr, hop_length)
    clip_chroma = _chroma(clip_audio, sr, hop_length)

    step_frames = max(1, int(step_sec * sr / hop_length))
    frames_per_sec = sr / hop_length

    # If clip is longer than source, search for source inside clip instead,
    # then negate the offset (source starts -offset into the clip).
    swapped = False
    if clip_chroma.shape[1] > src_chroma.shape[1]:
        logger.debug("Clip longer than source — swapping for search")
        src_chroma, clip_chroma = clip_chroma, src_chroma
        swapped = True

    clip_frames = clip_chroma.shape[1]
    total_source_frames = src_chroma.shape[1]

    logger.debug(
        f"Search: long={total_source_frames} frames, short={clip_frames} frames, "
        f"step={step_frames} frames, swapped={swapped}"
    )

    best_offset = 0.0
    best_sim = 0.0

    t = 0
    while t + clip_frames <= total_source_frames:
        window = src_chroma[:, t:t + clip_frames]
        sim = _chroma_similarity(window, clip_chroma)
        if sim > best_sim:
            best_sim = sim
            best_offset = t / frames_per_sec
            logger.debug(f"  t={best_offset:.1f}s sim={sim:.3f}")
        if sim > 0.95:
            break  # early exit on high confidence
        t += step_frames

    if best_sim < min_confidence:
        raise AlignmentError(
            f"Best chroma similarity {best_sim:.3f} < threshold {min_confidence}"
        )

    # If we swapped, the offset means: "source starts at best_offset inside clip"
    # → clip starts at -best_offset relative to source
    if swapped:
        best_offset = -best_offset

    logger.info(
        f"Audio aligned: {clip_video.name} → offset={best_offset:.2f}s "
        f"confidence={best_sim:.3f}"
    )
    return AlignResult(offset_sec=best_offset, confidence=best_sim, method="audio")
