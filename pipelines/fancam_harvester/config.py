"""FancamHarvester pipeline configuration (dataclass, safe for importlib)."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class FancamConfig:
    # --- Reddit ---
    subreddit: str = "kpopfancams"
    sort: str = "new"
    max_posts: int = 20
    request_delay: float = 1.5

    # --- Storage ---
    # Base workspace; all sub-dirs are relative to this
    workspace: str = "/tmp/fancam_harvester"

    @property
    def raw_dir(self) -> Path:
        return Path(self.workspace) / "raw"

    @property
    def source_dir(self) -> Path:
        return Path(self.workspace) / "sources"

    @property
    def clips_dir(self) -> Path:
        return Path(self.workspace) / "clips"

    @property
    def aligned_dir(self) -> Path:
        return Path(self.workspace) / "aligned"

    @property
    def library_dir(self) -> Path:
        return Path(self.workspace) / "library"

    @property
    def unidentified_dir(self) -> Path:
        return Path(self.workspace) / "unidentified"

    @property
    def highlights_dir(self) -> Path:
        return Path(self.workspace) / "highlights"

    # --- JDownloader ---
    jd_email: str = ""
    jd_password: str = ""
    jd_device_name: str = ""
    use_jdownloader: bool = True

    # --- Pixeldrain ---
    pixeldrain_api_key: str = ""

    # --- Alignment ---
    # "audio" | "visual" | "auto"  (auto = audio first, visual fallback)
    align_strategy: str = "auto"
    audio_confidence_threshold: float = 0.6
    visual_ssim_threshold: float = 0.85
    visual_sample_frames: int = 5

    # --- Quality ---
    # Re-extract from source if source quality is >= clip quality * this factor
    quality_upgrade_factor: float = 1.2

    # --- Identification ---
    # LLM confidence threshold; below this → unidentified
    id_confidence_threshold: float = 0.65
    # YouTube Data API key (optional; enriches metadata)
    youtube_api_key: str = ""

    # --- Compilation ---
    # Minimum clips required to compile a highlight reel
    min_clips_for_highlight: int = 3
    highlight_max_duration: float = 180.0   # seconds

    def ensure_dirs(self) -> None:
        for d in [
            self.raw_dir, self.source_dir, self.clips_dir,
            self.aligned_dir, self.library_dir, self.unidentified_dir,
            self.highlights_dir,
        ]:
            d.mkdir(parents=True, exist_ok=True)
