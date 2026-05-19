"""
Clip-to-source alignment.

Usage:
    from fancam_harvester.align import align_clip, AlignResult, AlignmentError
"""

from .audio_fingerprint import AlignResult, AlignmentError
from .audio_fingerprint import find_offset as _audio_find_offset
from .visual_match import find_offset as _visual_find_offset


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
