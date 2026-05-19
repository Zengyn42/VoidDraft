"""
FancamHarvester pipeline node functions.

Each function maps to one DETERMINISTIC node in entity.json.
The `identify` node is CLAUDE_SDK (LLM), handled separately.

Node functions:
    fetch(state)     → posts: list[PostMeta]
    download(state)  → downloads: list[DownloadRecord]
    align(state)     → alignments: list[AlignRecord]
    analyze(state)   → analyses: list[AnalysisRecord]
    store(state)     → stored: list[StoredRecord]

Pulsify components reused:
    JDownloaderHelper, VideoDownloader → download node
    VideoClipper                       → quality re-extract
    BaseAnalyzer (YOLO Pose)           → analyze node
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# VoidDraft lib path (so relative imports from pipelines work)
# ---------------------------------------------------------------------------
_PIPELINE_DIR = Path(__file__).parent
_VOIDDRAFT = _PIPELINE_DIR.parent.parent
_LIB = _VOIDDRAFT / "lib"
if str(_LIB) not in sys.path:
    sys.path.insert(0, str(_LIB))

# content_retriever shared modules (reuse Reddit source + Pixeldrain downloader)
_CONTENT_RETRIEVER = _VOIDDRAFT / "pipelines" / "content_retriever"
if str(_CONTENT_RETRIEVER) not in sys.path:
    sys.path.insert(0, str(_CONTENT_RETRIEVER))

from config import FancamConfig  # noqa: E402  (relative to pipeline dir)

# ---------------------------------------------------------------------------
# Data records (all JSON-serialisable)
# ---------------------------------------------------------------------------

@dataclass
class PostMeta:
    post_id: str
    title: str
    url: str
    subreddit: str
    source_urls: list[str]      # YouTube/TikTok etc.
    pixeldrain_urls: list[str]
    text: str
    created_utc: float


@dataclass
class DownloadRecord:
    post_id: str
    clip_id: str                # "{post_id}_{index}"
    clip_type: str              # "source" | "clip"
    url: str
    local_path: str             # absolute path
    file_size_bytes: int
    duration_sec: float
    width: int
    height: int
    fps: float
    download_method: str        # "yt-dlp" | "jdownloader" | "pixeldrain"


@dataclass
class AlignRecord:
    clip_id: str
    source_clip_id: str        # which source download this aligns to
    offset_sec: float
    confidence: float
    method: str                # "audio" | "visual" | "unmatched"
    error: str = ""


@dataclass
class AnalysisRecord:
    clip_id: str
    quality_tag: str           # "4K60fps" etc.
    best_path: str             # path of best-quality version
    upgraded: bool             # True if re-extracted from source
    action_tags: list[str]     # pose-based action tags
    pose_confidence: float
    duration_sec: float


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _load_config(state: dict) -> FancamConfig:
    raw = state.get("config", "{}")
    data = json.loads(raw) if raw else {}
    cfg = FancamConfig(**{k: v for k, v in data.items() if hasattr(FancamConfig, k)})
    cfg.ensure_dirs()
    return cfg


def _video_info(path: Path) -> dict:
    """Delegate to pulsify.utils.video_info."""
    from pulsify.utils.video_info import get_video_info
    return get_video_info(path)


def _extract_pixeldrain_links(text: str) -> list[str]:
    import re
    return re.findall(r"https?://pixeldrain\.com/[ul]/[A-Za-z0-9_-]+", text)


def _extract_source_links(text: str) -> list[str]:
    """Extract YouTube / TikTok / Bilibili links."""
    import re
    patterns = [
        r"https?://(?:www\.)?youtube\.com/watch\?[^\s\"'<>]+",
        r"https?://youtu\.be/[A-Za-z0-9_-]+",
        r"https?://(?:www\.)?tiktok\.com/@[^\s\"'<>]+",
        r"https?://(?:www\.)?bilibili\.com/video/[^\s\"'<>]+",
    ]
    links = []
    for pat in patterns:
        links.extend(re.findall(pat, text))
    return list(dict.fromkeys(links))  # deduplicate preserving order


# ---------------------------------------------------------------------------
# Node: fetch
# ---------------------------------------------------------------------------

def fetch(state: dict) -> dict:
    """Fetch Reddit posts and extract source/pixeldrain links."""
    cfg = _load_config(state)
    errors: list[str] = json.loads(state.get("errors", "[]"))
    posts: list[PostMeta] = []

    try:
        from sources.reddit import RedditSource
        source = RedditSource(
            subreddit=cfg.subreddit,
            sort=cfg.sort,
            request_delay=cfg.request_delay,
        )
        for post in source.get_posts(max_posts=cfg.max_posts):
            combined_text = post.title + " " + post.text
            # Also search all comments if they were fetched
            extra_text = " ".join(
                str(v) for v in post.extra.values() if isinstance(v, str)
            )
            full_text = combined_text + " " + extra_text

            pm = PostMeta(
                post_id=post.id,
                title=post.title,
                url=post.url,
                subreddit=cfg.subreddit,
                source_urls=_extract_source_links(full_text),
                pixeldrain_urls=_extract_pixeldrain_links(full_text),
                text=post.text[:1000],
                created_utc=float(post.extra.get("created_utc", 0)),
            )
            posts.append(pm)
            logger.info(
                f"Post {pm.post_id}: {len(pm.source_urls)} source(s), "
                f"{len(pm.pixeldrain_urls)} pixeldrain(s)"
            )
    except Exception as e:
        logger.error(f"fetch node error: {e}")
        errors.append(f"fetch: {e}")

    return {
        "posts": json.dumps([asdict(p) for p in posts]),
        "errors": json.dumps(errors),
    }


# ---------------------------------------------------------------------------
# Node: download
# ---------------------------------------------------------------------------

def download(state: dict) -> dict:
    """Download source videos (yt-dlp/JD) and pixeldrain clips."""
    cfg = _load_config(state)
    posts: list[dict] = json.loads(state.get("posts", "[]"))
    errors: list[str] = json.loads(state.get("errors", "[]"))
    records: list[DownloadRecord] = []

    from pulsify.fetcher.url_downloader import download_urls, DownloadResult
    from downloaders.pixeldrain import PixeldrainDownloader

    pd = PixeldrainDownloader(api_key=cfg.pixeldrain_api_key or None)

    for post_data in posts:
        post_id = post_data["post_id"]

        # --- Download ALL source videos (pre-checked for duration) ---
        src_urls = post_data.get("source_urls", [])
        if src_urls:
            dest_dir = cfg.source_dir / post_id
            results = download_urls(
                urls=src_urls,
                dest_dir=dest_dir,
                max_duration_sec=300.0,  # skip videos > 5 min
            )
            for idx, r in enumerate(results):
                clip_id = f"{post_id}_src{idx}"
                if r.skipped:
                    errors.append(f"source {clip_id} skipped: {r.skip_reason}")
                elif not r.success:
                    errors.append(f"source {clip_id} failed: {r.error}")
                else:
                    records.append(DownloadRecord(
                        post_id=post_id,
                        clip_id=clip_id,
                        clip_type="source",
                        url=r.url,
                        local_path=str(r.local_path),
                        file_size_bytes=r.file_size_bytes,
                        duration_sec=r.duration_sec,
                        width=r.width,
                        height=r.height,
                        fps=r.fps,
                        download_method="yt-dlp",
                    ))

        # --- Download pixeldrain clips ---
        for idx, pd_url in enumerate(post_data.get("pixeldrain_urls", [])):
            clip_id = f"{post_id}_clip{idx}"
            dest_dir = cfg.clips_dir / post_id
            dest_dir.mkdir(parents=True, exist_ok=True)
            try:
                downloaded = pd.download(pd_url, dest_dir)
                for path in downloaded:
                    info = _video_info(path)
                    records.append(DownloadRecord(
                        post_id=post_id,
                        clip_id=f"{clip_id}_{path.stem}",
                        clip_type="clip",
                        url=pd_url,
                        local_path=str(path),
                        file_size_bytes=path.stat().st_size,
                        duration_sec=info["duration_sec"],
                        width=info["width"],
                        height=info["height"],
                        fps=info["fps"],
                        download_method="pixeldrain",
                    ))
            except Exception as e:
                errors.append(f"clip {clip_id}: {e}")

    return {
        "downloads": json.dumps([asdict(r) for r in records]),
        "errors": json.dumps(errors),
    }


# ---------------------------------------------------------------------------
# Node: align
# ---------------------------------------------------------------------------

def align(state: dict) -> dict:
    """Align each clip to its source video, finding the timestamp offset."""
    cfg = _load_config(state)
    downloads: list[dict] = json.loads(state.get("downloads", "[]"))
    errors: list[str] = json.loads(state.get("errors", "[]"))
    alignments: list[AlignRecord] = []

    from align import align_clip, AlignmentError  # relative to pipeline dir

    # Build lookup: post_id → source records
    sources_by_post: dict[str, list[dict]] = {}
    clips_by_post: dict[str, list[dict]] = {}
    for rec in downloads:
        pid = rec["post_id"]
        if rec["clip_type"] == "source":
            sources_by_post.setdefault(pid, []).append(rec)
        else:
            clips_by_post.setdefault(pid, []).append(rec)

    for post_id, clip_recs in clips_by_post.items():
        source_recs = sources_by_post.get(post_id, [])
        if not source_recs:
            # No source → mark all clips as unmatched
            for cr in clip_recs:
                alignments.append(AlignRecord(
                    clip_id=cr["clip_id"],
                    source_clip_id="",
                    offset_sec=0.0,
                    confidence=0.0,
                    method="unmatched",
                    error="no source downloaded",
                ))
            continue

        # Use the largest source (most likely the full video)
        best_source = max(source_recs, key=lambda r: r["file_size_bytes"])

        for cr in clip_recs:
            try:
                result = align_clip(
                    source_video=Path(best_source["local_path"]),
                    clip_video=Path(cr["local_path"]),
                    strategy=cfg.align_strategy,
                    audio_confidence_threshold=cfg.audio_confidence_threshold,
                    visual_ssim_threshold=cfg.visual_ssim_threshold,
                    visual_sample_frames=cfg.visual_sample_frames,
                )
                alignments.append(AlignRecord(
                    clip_id=cr["clip_id"],
                    source_clip_id=best_source["clip_id"],
                    offset_sec=result.offset_sec,
                    confidence=result.confidence,
                    method=result.method,
                ))
                logger.info(
                    f"Aligned {cr['clip_id']}: offset={result.offset_sec:.2f}s "
                    f"({result.method}, conf={result.confidence:.2f})"
                )
            except AlignmentError as e:
                logger.warning(f"Alignment failed for {cr['clip_id']}: {e}")
                alignments.append(AlignRecord(
                    clip_id=cr["clip_id"],
                    source_clip_id=best_source["clip_id"],
                    offset_sec=0.0,
                    confidence=0.0,
                    method="unmatched",
                    error=str(e),
                ))

    return {
        "alignments": json.dumps([asdict(a) for a in alignments]),
        "errors": json.dumps(errors),
    }


# ---------------------------------------------------------------------------
# Node: analyze
# ---------------------------------------------------------------------------

def analyze(state: dict) -> dict:
    """
    For each clip: quality compare vs source, re-extract if source is better,
    then run pose analysis for action tagging.

    Reuses from Pulsify:
        VideoClipper  — FFmpeg re-extraction
        YOLOAnalyzer  — pose-based action tags
    """
    cfg = _load_config(state)
    downloads: list[dict] = json.loads(state.get("downloads", "[]"))
    alignments: list[dict] = json.loads(state.get("alignments", "[]"))
    errors: list[str] = json.loads(state.get("errors", "[]"))
    analyses: list[AnalysisRecord] = []

    # Pulsify imports
    try:
        from pulsify.video_clipper import VideoClipper
        clipper = VideoClipper()
    except ImportError as e:
        errors.append(f"analyze: VideoClipper import failed: {e}")
        clipper = None

    # Try to import YOLO pose analyzer (optional; skip if not installed)
    try:
        from pulsify.analyzer.pose.yolo import YOLOAnalyzer
        pose_analyzer = YOLOAnalyzer(device="cpu")
    except Exception:
        pose_analyzer = None
        logger.info("YOLO pose analyzer not available — action tags will be empty")

    # Build lookup maps
    downloads_by_id = {d["clip_id"]: d for d in downloads}
    align_by_clip = {a["clip_id"]: a for a in alignments}

    # Sources lookup
    sources_by_post: dict[str, list[dict]] = {}
    for d in downloads:
        if d["clip_type"] == "source":
            sources_by_post.setdefault(d["post_id"], []).append(d)

    clips = [d for d in downloads if d["clip_type"] == "clip"]

    for cr in clips:
        clip_id = cr["clip_id"]
        clip_path = Path(cr["local_path"])
        align_rec = align_by_clip.get(clip_id)

        best_path = clip_path
        upgraded = False

        # --- Quality upgrade: re-extract from source if it's better ---
        if (
            align_rec
            and align_rec["method"] != "unmatched"
            and clipper is not None
        ):
            src_recs = sources_by_post.get(cr["post_id"], [])
            if src_recs:
                best_src = max(src_recs, key=lambda r: r["file_size_bytes"])
                src_path = Path(best_src["local_path"])

                src_quality = best_src["height"] * best_src["fps"]
                clip_quality = cr["height"] * cr["fps"]

                if src_quality >= clip_quality * cfg.quality_upgrade_factor:
                    # Re-extract from source
                    out_path = (
                        cfg.aligned_dir
                        / f"{clip_id}_upgraded.mp4"
                    )
                    try:
                        success = clipper.extract_clip(
                            input_file=str(src_path),
                            start_time=align_rec["offset_sec"],
                            duration=cr["duration_sec"],
                            output_file=str(out_path),
                        )
                        if success and out_path.exists():
                            best_path = out_path
                            upgraded = True
                            logger.info(
                                f"Upgraded {clip_id}: {cr['height']}p → {best_src['height']}p"
                            )
                    except Exception as e:
                        errors.append(f"analyze re-extract {clip_id}: {e}")

        # --- Pose / action analysis ---
        action_tags: list[str] = []
        pose_confidence = 0.0
        if pose_analyzer is not None:
            try:
                result = pose_analyzer.analyze(str(best_path))
                # Derive simple action tags from pose features
                for frame in (result.frames or []):
                    for tag, val in frame.features.items():
                        if isinstance(val, (int, float)) and float(val) > 0.5:
                            action_tags.append(tag)
                # Deduplicate
                action_tags = list(dict.fromkeys(action_tags))
                pose_confidence = result.score if result else 0.0
            except Exception as e:
                logger.warning(f"Pose analysis failed for {clip_id}: {e}")

        info = _video_info(best_path)
        from storage.organizer import _quality_tag
        quality_tag = _quality_tag(best_path)

        analyses.append(AnalysisRecord(
            clip_id=clip_id,
            quality_tag=quality_tag,
            best_path=str(best_path),
            upgraded=upgraded,
            action_tags=action_tags,
            pose_confidence=pose_confidence,
            duration_sec=info["duration_sec"] or cr["duration_sec"],
        ))

    return {
        "analyses": json.dumps([asdict(a) for a in analyses]),
        "errors": json.dumps(errors),
    }


# ---------------------------------------------------------------------------
# Node: store
# ---------------------------------------------------------------------------

def store(state: dict) -> dict:
    """
    Organise clips into library directory based on LLM identity output.

    The `identities` field is written by the CLAUDE_SDK identify node.
    This node parses the LLM text, then calls storage.organizer.store_clip().
    """
    cfg = _load_config(state)
    analyses: list[dict] = json.loads(state.get("analyses", "[]"))
    downloads: list[dict] = json.loads(state.get("downloads", "[]"))
    alignments: list[dict] = json.loads(state.get("alignments", "[]"))
    raw_identities: str = state.get("identities", "")
    errors: list[str] = json.loads(state.get("errors", "[]"))

    from identify.idol_parser import parse_llm_response, IdentityRecord
    from storage.organizer import store_clip

    # The LLM node may output a JSON array of per-clip results,
    # or a text block with multiple JSON objects.
    # We try to parse it as a JSON array first, then line-by-line.
    identity_map: dict[str, IdentityRecord] = {}
    try:
        items = json.loads(raw_identities)
        if isinstance(items, list):
            for item in items:
                clip_id = item.get("clip_id", "")
                rec = IdentityRecord(
                    clip_id=clip_id,
                    group=item.get("group"),
                    idol=item.get("idol"),
                    song=item.get("song"),
                    performance_date=item.get("performance_date"),
                    confidence=float(item.get("confidence", 0.0)),
                    notes=item.get("notes", ""),
                )
                identity_map[clip_id] = rec
    except Exception:
        # Fallback: parse individual LLM text blocks per clip_id
        for d in downloads:
            if d["clip_type"] == "clip":
                cid = d["clip_id"]
                rec = parse_llm_response(cid, raw_identities)
                identity_map[cid] = rec

    analyses_by_id = {a["clip_id"]: a for a in analyses}
    downloads_by_id = {d["clip_id"]: d for d in downloads}
    align_by_id = {a["clip_id"]: a for a in alignments}

    stored_records = []

    for clip_id, identity in identity_map.items():
        # Apply confidence threshold
        if identity.confidence < cfg.id_confidence_threshold:
            identity.group = None  # force unidentified

        analysis = analyses_by_id.get(clip_id)
        if not analysis:
            continue

        dl = downloads_by_id.get(clip_id, {})
        post_id = dl.get("post_id", "unknown")
        align_rec = align_by_id.get(clip_id)
        offset = align_rec["offset_sec"] if align_rec else None

        try:
            rec = store_clip(
                clip_path=Path(analysis["best_path"]),
                library_dir=cfg.library_dir,
                unidentified_dir=cfg.unidentified_dir,
                clip_id=clip_id,
                post_id=post_id,
                identity=identity,
                align_offset_sec=offset,
            )
            stored_records.append(rec)
        except Exception as e:
            errors.append(f"store {clip_id}: {e}")
            logger.error(f"Store failed for {clip_id}: {e}")

    return {
        "stored": json.dumps([
            {
                "clip_id": r.clip_id,
                "final_path": r.final_path,
                "category": r.category,
                "group": r.group,
                "idol": r.idol,
                "song": r.song,
                "performance_date": r.performance_date,
            }
            for r in stored_records
        ]),
        "errors": json.dumps(errors),
    }
