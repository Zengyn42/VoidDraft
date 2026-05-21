"""PipelineConfig for content_retriever."""
from __future__ import annotations
from dataclasses import dataclass, field
from pathlib import Path

@dataclass
class PipelineConfig:
    source_type: str                # "reddit" / "rednote"
    source_config: dict             # passed to source constructor
    download_dir: str               # string path
    max_posts: int = 50
    analyzer: str = "none"          # "ollama" / "claude" / "gemini" / "none"
    analyze_model: str | None = None
    frame_interval: int = 10
    max_frames: int = 8
    pixeldrain_api_key: str | None = None
    summarize: bool = True                      # enable LLM summarisation node
    summarize_backend: str = "none"             # "ollama" | "claude" | "none"
    summarize_model: str = ""                   # model name for chosen backend
    ollama_url: str = "http://localhost:11434"  # Ollama base URL
    credentials_file: str = ""                  # rednote account credentials (EdenGateway)
    gemini_api_key: str = ""                    # Gemini API key (falls back to GEMINI_API_KEY env var)
    # Auto backend selection — if transcript exceeds threshold, switch to fallback backend
    summarize_fallback_backend: str = ""        # "gemini" | "claude" | "" (disabled)
    summarize_fallback_model: str = ""          # model for fallback (e.g. gemini-2.5-flash-preview-05-20)
    summarize_auto_threshold_chars: int = 60000 # ~30K tokens for mixed Chinese; above this → fallback
    # Retry config for LLM calls
    summarize_max_retries: int = 3              # attempts per transcript before giving up
    summarize_retry_delay: float = 5.0          # base delay in seconds (doubles each retry)
    max_video_duration_sec: int = 1200          # skip videos longer than this (0 = no limit, default 20 min)

    @classmethod
    def from_yaml(cls, path: str) -> "PipelineConfig":
        import yaml
        raw = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
        source_block = raw.get("source", {})
        source_type = source_block.pop("type", "reddit")
        return cls(
            source_type=source_type,
            source_config=source_block,
            download_dir=raw.get("download_dir", "./downloads"),
            max_posts=int(raw.get("max_posts", 50)),
            analyzer=raw.get("analyzer", "none"),
            analyze_model=raw.get("analyze_model", None),
            frame_interval=int(raw.get("frame_interval", 10)),
            max_frames=int(raw.get("max_frames", 8)),
            pixeldrain_api_key=raw.get("pixeldrain_api_key", None),
            summarize=raw.get("summarize", True),
            summarize_backend=raw.get("summarize_backend", "none"),
            summarize_model=raw.get("summarize_model", ""),
            ollama_url=raw.get("ollama_url", "http://localhost:11434"),
            credentials_file=source_block.get("credentials_file", ""),
            gemini_api_key=raw.get("gemini_api_key", ""),
            summarize_fallback_backend=raw.get("summarize_fallback_backend", ""),
            summarize_fallback_model=raw.get("summarize_fallback_model", ""),
            summarize_auto_threshold_chars=int(raw.get("summarize_auto_threshold_chars", 60000)),
            summarize_max_retries=int(raw.get("summarize_max_retries", 3)),
            summarize_retry_delay=float(raw.get("summarize_retry_delay", 5.0)),
            max_video_duration_sec=int(raw.get("max_video_duration_sec", 1200)),
        )

    def to_dict(self) -> dict:
        return {
            "source_type": self.source_type,
            "source_config": self.source_config,
            "download_dir": self.download_dir,
            "max_posts": self.max_posts,
            "analyzer": self.analyzer,
            "analyze_model": self.analyze_model,
            "frame_interval": self.frame_interval,
            "max_frames": self.max_frames,
            "pixeldrain_api_key": self.pixeldrain_api_key,
            "summarize": self.summarize,
            "summarize_backend": self.summarize_backend,
            "summarize_model": self.summarize_model,
            "ollama_url": self.ollama_url,
            "credentials_file": self.credentials_file,
            "gemini_api_key": self.gemini_api_key,
            "summarize_fallback_backend": self.summarize_fallback_backend,
            "summarize_fallback_model": self.summarize_fallback_model,
            "summarize_auto_threshold_chars": self.summarize_auto_threshold_chars,
            "summarize_max_retries": self.summarize_max_retries,
            "summarize_retry_delay": self.summarize_retry_delay,
            "max_video_duration_sec": self.max_video_duration_sec,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "PipelineConfig":
        return cls(
            source_type=d["source_type"],
            source_config=d.get("source_config", {}),
            download_dir=d.get("download_dir", "./downloads"),
            max_posts=int(d.get("max_posts", 50)),
            analyzer=d.get("analyzer", "none"),
            analyze_model=d.get("analyze_model"),
            frame_interval=int(d.get("frame_interval", 10)),
            max_frames=int(d.get("max_frames", 8)),
            pixeldrain_api_key=d.get("pixeldrain_api_key"),
            summarize=d.get("summarize", True),
            summarize_backend=d.get("summarize_backend", "none"),
            summarize_model=d.get("summarize_model", ""),
            ollama_url=d.get("ollama_url", "http://localhost:11434"),
            credentials_file=d.get("credentials_file", ""),
            gemini_api_key=d.get("gemini_api_key", ""),
            summarize_fallback_backend=d.get("summarize_fallback_backend", ""),
            summarize_fallback_model=d.get("summarize_fallback_model", ""),
            summarize_auto_threshold_chars=int(d.get("summarize_auto_threshold_chars", 60000)),
            summarize_max_retries=int(d.get("summarize_max_retries", 3)),
            summarize_retry_delay=float(d.get("summarize_retry_delay", 5.0)),
            max_video_duration_sec=int(d.get("max_video_duration_sec", 1200)),
        )

