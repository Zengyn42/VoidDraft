"""
Clip-to-source alignment.

Usage:
    from fancam_harvester.align import align_clip, AlignResult, AlignmentError
"""

import subprocess
from .audio_fingerprint import AlignResult, AlignmentError
from .audio_fingerprint import find_offset as _audio_find_offset
from .visual_match import find_offset as _visual_find_offset


def _has_audio(video_path) -> bool:
    """Return True if the video file contains at least one audio stream."""
    from pathlib import Path
    result = subprocess.run(
        ["ffmpeg", "-hide_banner", "-i", str(video_path)],
        capture_output=True, text=True,
    )
    return "Audio:" in result.stderr


def align_clip(
    source_video,
    clip_video,
    strategy: str = "auto",
    audio_confidence_threshold: float = 0.6,
    visual_ssim_threshold: float = 0.85,
    visual_sample_frames: int = 5,
) -> AlignResult:
    """
    Align clip to source using the specified strategy.

    strategy:
        "audio"  — audio fingerprint only
        "visual" — visual SSIM only
        "auto"   — audio first, fall back to visual on failure
    """
    if strategy in ("audio", "auto"):
        if not _has_audio(clip_video):
            import logging
            logging.getLogger(__name__).info(
                f"{clip_video.name} has no audio — skipping audio alignment"
            )
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
                import logging
                logging.getLogger(__name__).warning(
                    f"Audio alignment failed ({e}), trying visual..."
                )

    return _visual_find_offset(
        source_video, clip_video,
        n_samples=visual_sample_frames,
        ssim_threshold=visual_ssim_threshold,
    )


__all__ = ["align_clip", "AlignResult", "AlignmentError"]
