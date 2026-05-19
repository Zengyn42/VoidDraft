"""
VoidDraft shared helper: Pulsify import bridge.

Usage:
    from voiddraft_lib.pulsify import (
        JDownloaderHelper,
        VideoDownloader,
        VideoClipper,
        BaseAnalyzer, AnalysisResult, FrameData,
        Renderer, Timeline, TimelineSegment,
    )

The Pulsify repo lives at <Foundation>/Projects/Pulsify/src.
We add it to sys.path once so downstream modules import as normal.
"""

from __future__ import annotations

import sys
from pathlib import Path

_FOUNDATION = Path(__file__).parent.parent.parent   # VoidDraft → Foundation
_PULSIFY_SRC = _FOUNDATION.parent / "Projects" / "Pulsify" / "src"

if not _PULSIFY_SRC.exists():
    raise ImportError(
        f"Pulsify src not found at {_PULSIFY_SRC}. "
        "Clone the repo or set PULSIFY_SRC env var."
    )

if str(_PULSIFY_SRC) not in sys.path:
    sys.path.insert(0, str(_PULSIFY_SRC))

# -- Re-export commonly used symbols ------------------------------------------

from fetcher.jdownloader_helper import JDownloaderHelper          # noqa: E402
from fetcher.downloader import VideoDownloader                    # noqa: E402
from video_clipper import VideoClipper                            # noqa: E402
from analyzer.base import BaseAnalyzer, AnalysisResult, FrameData  # noqa: E402
from merge.renderer import Renderer                               # noqa: E402
from merge.timeline_builder import Timeline, TimelineSegment      # noqa: E402

__all__ = [
    "JDownloaderHelper",
    "VideoDownloader",
    "VideoClipper",
    "BaseAnalyzer",
    "AnalysisResult",
    "FrameData",
    "Renderer",
    "Timeline",
    "TimelineSegment",
    "_PULSIFY_SRC",
]
