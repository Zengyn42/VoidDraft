"""
FancamHarvester pipeline node functions — V1 implementation.

Node functions (in pipeline order):
    fetch(state)                → posts: list[PostMeta]
    download(state)             → downloads: list[DownloadRecord]
    split_merged(state)         → downloads (extended with segments)
    detect_slowmo_for_clips(st) → slowmo_info: dict
    align(state)                → alignments: list[AlignRecord]   (parallel competition)
    extract_hd(state)           → extracts: list[ExtractRecord]
    select_best_clip(state)     → final_clips: list[FinalClipRecord]
    parse_post_metadata(state)  → post_metas: list[PostMetadata]
    store_results(state, db)    → _db_stats: dict

Pulsify components reused:
    url_downloader   → download node
    slowmo_detect    → detect_slowmo_for_clips node
    audio/dinov2/pose align → align node (parallel)
    VideoClipper     → extract_hd / select_best_clip
"""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict, dataclass, field, replace as dc_replace
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

from config import FancamConfig  # noqa: E402

# ═══════════════════════════════════════════════════════════════════════════
# Tuning constants (DESIGN_V1.md §5)
# ═══════════════════════════════════════════════════════════════════════════

# §5.1 — Alignment: parallel competition, minimum to accept
ALIGN_MIN_ACCEPT       = 0.10   # below this → unmatched
AUDIO_CONF_REF         = 0.10   # reference baseline (logged, not gating)
DINOV2_CONF_REF        = 0.50
POSE_CONF_REF          = 0.55
# If best confidence after parallel competition is below this, retry at 2x speed
SLOWMO_RETRY_THRESHOLD = 0.65   # §5.2 参数调优记录
# Offset deduplication: two clips are "same region" if their offsets are within this gap
DEDUP_MIN_GAP_SEC      = 2.0    # §5.1 参数调优记录

# §5.2 — Slowmo detection
SPEED_FACTOR_CANDIDATES = [1.0, 2.0, 4.0]

# §5.3 — Zoom detection
ZOOM_THRESHOLD         = 1.5
AR_DIFF_THRESHOLD      = 0.15

# §5.4 — Upvote settled threshold (hours)
_SETTLED_HOURS         = 48.0

# §5.5 — LLM metadata
LLM_CONF_MIN           = 0.6

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
    score: int = 0


@dataclass
class DownloadRecord:
    post_id: str
    clip_id: str                # "{post_id}_{index}"
    clip_type: str              # "source" | "clip" | "merged" | "external_ref"
    url: str
    local_path: str             # absolute path
    file_size_bytes: int
    duration_sec: float
    width: int
    height: int
    fps: float
    download_method: str        # "yt-dlp" | "jdownloader" | "pixeldrain" | "split"
    pixeldrain_filename: str = ""  # original PD filename (for album diff)


@dataclass
class AlignRecord:
    clip_id: str
    source_clip_id: str        # which source download this aligns to
    offset_sec: float
    confidence: float
    method: str                # "audio" | "dinov2" | "pose" | "unmatched"
    error: str = ""
    # parallel competition: all three layer confidences
    audio_conf: float = 0.0
    dinov2_conf: float = 0.0
    pose_conf: float = 0.0


@dataclass
class ExtractRecord:
    """One HD clip extracted from the source video at the aligned offset."""
    clip_id: str               # original clip_id this was extracted for
    source_clip_id: str        # source DownloadRecord this was cut from
    local_path: str            # path to the extracted HD file
    start_in_source: float     # offset_sec — where in source this starts
    end_in_source: float       # start + duration
    duration_sec: float
    width: int
    height: int
    fps: float
    confidence: float          # alignment confidence
    method: str                # alignment method used


@dataclass
class FinalClipRecord:
    """Result of select_best_clip: which files go into final/."""
    clip_id: str
    post_id: str
    final_path: str            # main final file
    final_creative_path: str = ""  # creative edit variant path (if any)
    final_kept: str = ""       # "pixeldrain" | "source_hd" | "both"
    is_slowmo: bool = False
    speed_factor: float = 1.0
    is_zoom_in: bool = False
    zoom_factor: float = 1.0
    zoom_method: str = ""


@dataclass
class PostMetadata:
    """Performance metadata parsed from filename / post title / URL."""
    post_id: str
    performance_date: str      # "YYYY-MM-DD" or "" if unknown
    song_name: str             # e.g. "Right Hand Girl"
    performers: list[str]      # e.g. ["Tzuyu"] or ["TWICE"]
    group_name: str            # e.g. "TWICE"
    parse_method: str          # "filename" | "title" | "llm"


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

def _get_pd_file_id_for_path(path: Path, album_files_raw: list[dict]) -> str | None:
    """Match a downloaded file path to its Pixeldrain file ID by filename."""
    name = path.name
    for f in album_files_raw:
        if f.get("name", "") == name:
            return f.get("id")
    return None


def _load_config(state: dict) -> FancamConfig:
    raw = state.get("config", "{}")
    data = json.loads(raw) if raw else {}
    cfg = FancamConfig(**{k: v for k, v in data.items() if hasattr(FancamConfig, k)})
    cfg.ensure_dirs()
    return cfg


def _video_info(path: Path) -> dict:
    """Delegate to pulsify.utils.video_info."""
    try:
        from pulsify.utils.video_info import get_video_info
        return get_video_info(path)
    except ImportError:
        logger.warning("pulsify.utils.video_info not available — using ffprobe")
        return _ffprobe_info(path)


def _ffprobe_info(path: Path) -> dict:
    """Fallback video info via raw ffprobe."""
    try:
        result = subprocess.run(
            ["ffprobe", "-v", "quiet", "-print_format", "json",
             "-show_streams", "-show_format", str(path)],
            capture_output=True, text=True, timeout=30,
        )
        data = json.loads(result.stdout)
        for s in data.get("streams", []):
            if s.get("codec_type") == "video":
                fps_str = s.get("r_frame_rate", "0/1")
                num, den = fps_str.split("/")
                fps = float(num) / float(den) if float(den) else 0.0
                dur = float(data.get("format", {}).get("duration", 0))
                return {
                    "width": int(s.get("width", 0)),
                    "height": int(s.get("height", 0)),
                    "fps": fps,
                    "duration_sec": dur,
                }
    except Exception as e:
        logger.warning("ffprobe failed for %s: %s", path, e)
    return {"width": 0, "height": 0, "fps": 0.0, "duration_sec": 0.0}


def _extract_pixeldrain_links(text: str) -> list[str]:
    return re.findall(r"https?://pixeldrain\.com/[ul]/[A-Za-z0-9_-]+", text)


def _extract_source_links(text: str) -> list[str]:
    """Extract YouTube / TikTok / Bilibili links.

    Handles both bare URLs and Markdown link syntax [label](URL).
    Trailing punctuation (closing brackets, commas, dots) is stripped.
    """
    patterns = [
        r"https?://(?:www\.)?youtube\.com/watch\?[^\s\"'<>]+",
        r"https?://youtu\.be/[A-Za-z0-9_-]+",
        r"https?://(?:www\.)?tiktok\.com/@[^\s\"'<>]+",
        r"https?://(?:www\.)?bilibili\.com/video/[^\s\"'<>]+",
    ]
    links = []
    for pat in patterns:
        for m in re.findall(pat, text):
            clean = m.rstrip(").,]")   # strip Markdown-link trailing chars
            if clean:
                links.append(clean)
    return list(dict.fromkeys(links))  # deduplicate preserving order


# Regex to extract YouTube video IDs embedded in Pixeldrain filenames, e.g.:
#   "qwer - hina [youtube@uaVz6uzupBY]-1.mp4" → "uaVz6uzupBY"
_YT_IN_FILENAME_RE = re.compile(r"\[youtube@([A-Za-z0-9_-]{11})\]", re.IGNORECASE)


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
                score=int(post.extra.get("score", 0)),
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

    try:
        from pulsify.fetcher.url_downloader import download_urls
    except ImportError:
        errors.append("download: pulsify.fetcher.url_downloader not available")
        return {"downloads": "[]", "errors": json.dumps(errors)}

    try:
        from downloaders.pixeldrain import PixeldrainDownloader
        pd = PixeldrainDownloader(api_key=cfg.pixeldrain_api_key or None)
    except ImportError:
        pd = None
        errors.append("download: pixeldrain downloader not available")

    from clip_analyzer import (
        parse_clip_filename, PixeldrainFile, identify_source_in_album,
    )

    for post_data in posts:
        post_id = post_data["post_id"]

        # --- Download ALL source videos (pre-checked for duration) ---
        src_urls = post_data.get("source_urls", [])
        if src_urls:
            dest_dir = cfg.source_dir / post_id
            try:
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
            except Exception as e:
                errors.append(f"source download {post_id}: {e}")

        # --- Download pixeldrain clips (with source/merged classification) ---
        if pd is None:
            continue

        for pd_url in post_data.get("pixeldrain_urls", []):
            dest_dir = cfg.clips_dir / post_id
            dest_dir.mkdir(parents=True, exist_ok=True)

            # Pre-fetch album metadata to classify files before downloading.
            list_match = re.search(r"pixeldrain\.com/l/([A-Za-z0-9_-]+)", pd_url)
            album_files_raw: list[dict] = []
            source_file_id: str | None = None
            album_youtube_ids: list[str] = []

            if list_match:
                list_id = list_match.group(1)
                try:
                    album_files_raw = pd.list_album(list_id)
                except Exception as e:
                    errors.append(f"pixeldrain list_album {list_id}: {e}")
                    continue

                pd_files = [
                    PixeldrainFile(
                        file_id=f["id"],
                        name=f.get("name", f["id"]),
                        size_bytes=f.get("size", 0),
                    )
                    for f in album_files_raw
                ]

                # Priority 1: Pixeldrain native source candidate
                source_result = identify_source_in_album(pd_files)
                if source_result.file:
                    source_file_id = source_result.file.file_id
                    logger.info(
                        "Pixeldrain source candidate: %s (%s)",
                        source_result.file.name, source_result.reason,
                    )

                # Priority 2: [youtube@ID] embedded in filenames
                for pf in pd_files:
                    m = _YT_IN_FILENAME_RE.search(pf.name)
                    if m:
                        yt_id = m.group(1)
                        if yt_id not in album_youtube_ids:
                            album_youtube_ids.append(yt_id)

            # If Pixeldrain has no native source, try [youtube@ID] fallback
            if not source_file_id and album_youtube_ids:
                existing_yt_urls = set(post_data.get("source_urls", []))
                yt_fallback_urls = [
                    f"https://www.youtube.com/watch?v={yt_id}"
                    for yt_id in album_youtube_ids
                    if f"https://www.youtube.com/watch?v={yt_id}" not in existing_yt_urls
                ]
                if yt_fallback_urls:
                    logger.info(
                        "post=%s: downloading %d [youtube@ID] source(s) from album filenames",
                        post_id, len(yt_fallback_urls),
                    )
                    yt_dest = cfg.source_dir / post_id
                    try:
                        yt_results = download_urls(
                            urls=yt_fallback_urls,
                            dest_dir=yt_dest,
                            max_duration_sec=300.0,
                        )
                        for idx, r in enumerate(yt_results):
                            cid = f"{post_id}_ytsrc{idx}"
                            if r.skipped:
                                errors.append(f"yt-src {cid} skipped: {r.skip_reason}")
                            elif not r.success:
                                errors.append(f"yt-src {cid} failed: {r.error}")
                            else:
                                records.append(DownloadRecord(
                                    post_id=post_id,
                                    clip_id=cid,
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
                    except Exception as e:
                        errors.append(f"yt-src download {post_id}: {e}")

            try:
                downloaded = pd.download(pd_url, dest_dir)
            except Exception as e:
                errors.append(f"pixeldrain {pd_url}: {e}")
                continue

            for path in downloaded:
                info = _video_info(path)
                clip_info = parse_clip_filename(path.name)
                file_id_in_album = _get_pd_file_id_for_path(path, album_files_raw)

                # Classify clip_type
                if source_file_id and file_id_in_album == source_file_id:
                    c_type = "source"
                    # Priority check: Pixeldrain source wins if higher resolution
                    pd_pixels = info.get("width", 0) * info.get("height", 0)
                    for i, r in enumerate(records):
                        if r.post_id != post_id or r.clip_type != "source":
                            continue
                        ext_pixels = r.width * r.height
                        if pd_pixels >= ext_pixels:
                            logger.info(
                                "Pixeldrain source (%dx%d) >= external source (%dx%d) "
                                "— demoting external source '%s' to external_ref",
                                info.get("width", 0), info.get("height", 0),
                                r.width, r.height, r.clip_id,
                            )
                            records[i] = dc_replace(r, clip_type="external_ref")
                        else:
                            logger.info(
                                "External source (%dx%d) > Pixeldrain source (%dx%d) "
                                "— keeping external, storing Pixeldrain as clip",
                                r.width, r.height,
                                info.get("width", 0), info.get("height", 0),
                            )
                            c_type = "clip"
                elif clip_info.is_merged:
                    c_type = "merged"
                else:
                    c_type = "clip"

                records.append(DownloadRecord(
                    post_id=post_id,
                    clip_id=f"{post_id}_{path.stem}",
                    clip_type=c_type,
                    url=(f"https://pixeldrain.com/u/{file_id_in_album}"
                         if file_id_in_album else pd_url),
                    local_path=str(path),
                    file_size_bytes=path.stat().st_size,
                    duration_sec=info.get("duration_sec", 0.0),
                    width=info.get("width", 0),
                    height=info.get("height", 0),
                    fps=info.get("fps", 0.0),
                    download_method="pixeldrain",
                    pixeldrain_filename=path.name,
                ))

    return {
        "downloads": json.dumps([asdict(r) for r in records]),
        "errors": json.dumps(errors),
    }


# ---------------------------------------------------------------------------
# Node: split_merged
# ---------------------------------------------------------------------------

def split_merged(state: dict) -> dict:
    """
    For each 'merged' clip: detect scene cuts, split into segments, and
    add the segments back into the downloads list as 'clip' type.

    If individual clips already exist for the same post, skip splitting.
    If no cuts detected, the merged file is treated as a single clip.
    """
    cfg = _load_config(state)
    downloads: list[dict] = json.loads(state.get("downloads", "[]"))
    errors: list[str] = json.loads(state.get("errors", "[]"))

    try:
        from pulsify.align.scene_cut import detect_cuts, verify_cuts, flag_duplicate_matches
        _scene_cut_available = True
    except ImportError:
        _scene_cut_available = False
        logger.warning("pulsify.align.scene_cut not available — merged clips won't be split")

    new_records: list[dict] = []

    for rec in downloads:
        if rec["clip_type"] != "merged":
            continue

        merged_path = Path(rec["local_path"])
        if not merged_path.exists():
            errors.append(f"split_merged: file missing {merged_path}")
            continue

        post_id = rec["post_id"]

        # Collect individual clips for this post
        individual_clips = [
            Path(r["local_path"])
            for r in downloads
            if r["post_id"] == post_id
            and r["clip_type"] == "clip"
            and Path(r["local_path"]).exists()
        ]

        # ── Optimization: if individual clips already exist, skip splitting ──
        if individual_clips:
            logger.info(
                "split_merged: post=%s already has %d individual clip(s) — "
                "skipping split of %s",
                post_id, len(individual_clips), merged_path.name,
            )
            continue

        if not _scene_cut_available:
            # Treat whole merged file as one clip
            rec_copy = dict(rec)
            rec_copy["clip_type"] = "clip"
            rec_copy["clip_id"] = f"{rec['clip_id']}_whole"
            new_records.append(rec_copy)
            logger.info(
                "split_merged: scene_cut unavailable — treating %s as single clip",
                merged_path.name,
            )
            continue

        # Detect cuts
        try:
            cut_timestamps = detect_cuts(merged_path, threshold=25.0, min_gap_sec=1.0)
        except Exception as e:
            errors.append(f"split_merged detect_cuts {merged_path.name}: {e}")
            # Treat as single clip on detection failure
            rec_copy = dict(rec)
            rec_copy["clip_type"] = "clip"
            rec_copy["clip_id"] = f"{rec['clip_id']}_whole"
            new_records.append(rec_copy)
            continue

        if not cut_timestamps:
            # No cuts → treat as single clip (DESIGN_V1.md §3.3)
            logger.info("split_merged: no cuts found in %s — treating as single clip", merged_path.name)
            rec_copy = dict(rec)
            rec_copy["clip_type"] = "clip"
            rec_copy["clip_id"] = f"{rec['clip_id']}_whole"
            new_records.append(rec_copy)
            continue

        logger.info(
            "split_merged: %s → %d cut(s) at %s",
            merged_path.name,
            len(cut_timestamps),
            [f"{t:.2f}s" for t in cut_timestamps],
        )

        # Split the merged file at cut points
        seg_dir = Path(rec["local_path"]).parent / f"_segments_{merged_path.stem}"
        seg_dir.mkdir(parents=True, exist_ok=True)

        try:
            from pulsify.align.scene_cut import split_at_cuts
            seg_paths = split_at_cuts(merged_path, cut_timestamps, seg_dir)
        except Exception as e:
            errors.append(f"split_merged split_at_cuts {merged_path.name}: {e}")
            continue

        for i, seg_path in enumerate(seg_paths):
            info = _video_info(seg_path)
            new_records.append({
                "post_id": post_id,
                "clip_id": f"{rec['clip_id']}_seg{i}",
                "clip_type": "clip",
                "url": rec["url"],
                "local_path": str(seg_path),
                "file_size_bytes": seg_path.stat().st_size,
                "duration_sec": info.get("duration_sec", 0.0),
                "width": info.get("width", 0),
                "height": info.get("height", 0),
                "fps": info.get("fps", 0.0),
                "download_method": "split",
                "pixeldrain_filename": rec.get("pixeldrain_filename", ""),
            })
            logger.info(
                "split_merged: segment %d → %s (%.2fs)",
                i, seg_path.name, info.get("duration_sec", 0.0),
            )

    # Append new segment records to downloads
    if new_records:
        downloads.extend(new_records)
        logger.info("split_merged: added %d segment clip(s) total", len(new_records))

    return {
        "downloads": json.dumps(downloads),
        "errors": json.dumps(errors),
    }


# ---------------------------------------------------------------------------
# Node: detect_slowmo_for_clips
# ---------------------------------------------------------------------------

def detect_slowmo_for_clips(state: dict) -> dict:
    """
    Run slowmo detection on all clip-type downloads.

    Outputs state["slowmo_info"] = {clip_id: {is_slowmo, speed_factor, detected_by}}
    """
    downloads: list[dict] = json.loads(state.get("downloads", "[]"))
    errors: list[str] = json.loads(state.get("errors", "[]"))
    slowmo_info: dict[str, dict] = {}

    try:
        from pulsify.align.slowmo_detect import detect_slowmo
        _slowmo_available = True
    except ImportError:
        _slowmo_available = False
        logger.warning("pulsify.align.slowmo_detect not available — skipping slowmo detection")

    for rec in downloads:
        if rec["clip_type"] != "clip":
            continue

        clip_id = rec["clip_id"]
        clip_path = Path(rec["local_path"])

        if not clip_path.exists():
            slowmo_info[clip_id] = {"is_slowmo": False, "speed_factor": 1.0, "detected_by": "none"}
            continue

        if not _slowmo_available:
            slowmo_info[clip_id] = {"is_slowmo": False, "speed_factor": 1.0, "detected_by": "unavailable"}
            continue

        try:
            result = detect_slowmo(clip_path)
            slowmo_info[clip_id] = {
                "is_slowmo": result.is_slowmo,
                "speed_factor": result.speed_factor,
                "detected_by": result.detected_by,
            }
            if result.is_slowmo:
                logger.info(
                    "detect_slowmo: %s → slowmo (factor=%.1f, by=%s)",
                    clip_id, result.speed_factor, result.detected_by,
                )
        except Exception as e:
            logger.warning("detect_slowmo failed for %s: %s", clip_id, e)
            slowmo_info[clip_id] = {"is_slowmo": False, "speed_factor": 1.0, "detected_by": "error"}
            errors.append(f"detect_slowmo {clip_id}: {e}")

    return {
        "slowmo_info": json.dumps(slowmo_info),
        "errors": json.dumps(errors),
    }


# ---------------------------------------------------------------------------
# Node: align (parallel competition — DESIGN_V1.md §3.5)
# ---------------------------------------------------------------------------

def _run_audio_align(source_path: Path, clip_path: Path, speed_factor: float) -> tuple[float, float]:
    """Run audio chroma alignment. Returns (offset_sec, confidence)."""
    try:
        from align.audio_fingerprint import find_offset as audio_find_offset
        if speed_factor != 1.0:
            result = audio_find_offset(
                source_path, clip_path,
                clip_speed_factor=speed_factor,
                min_confidence=0.0,  # don't raise, just return low conf
            )
        else:
            result = audio_find_offset(
                source_path, clip_path,
                min_confidence=0.0,
            )
        return (result.offset_sec, result.confidence)
    except Exception as e:
        logger.debug("audio align failed: %s", e)
        return (0.0, 0.0)


def _run_dinov2_align(source_path: Path, clip_path: Path) -> tuple[float, float]:
    """Run DINOv2 diagonal alignment. Returns (offset_sec, confidence)."""
    try:
        from pulsify.align.dinov2_align import dinov2_find_offset
        offset, conf = dinov2_find_offset(source_path, clip_path)
        return (offset, conf)
    except ImportError:
        logger.debug("DINOv2 align not available")
        return (0.0, 0.0)
    except Exception as e:
        logger.debug("DINOv2 align failed: %s", e)
        return (0.0, 0.0)


def _make_speed_clip(clip_path: Path, factor: float = 2.0) -> Path | None:
    """
    Return a temp copy of clip_path sped up by `factor` using ffmpeg setpts.
    Caller is responsible for deleting the returned file.
    Returns None if ffmpeg is unavailable or the operation fails.
    """
    import tempfile, shutil as _shutil
    ffmpeg = _shutil.which("ffmpeg")
    if not ffmpeg:
        return None
    tmp = Path(tempfile.mktemp(suffix=f"_fast{factor:.0f}x.mp4"))
    pts = f"{1.0 / factor:.6f}*PTS"
    result = subprocess.run(
        [ffmpeg, "-y", "-i", str(clip_path), "-vf", f"setpts={pts}", "-an", str(tmp)],
        capture_output=True,
    )
    if result.returncode != 0 or not tmp.exists():
        return None
    return tmp


def _source_duration(source_path: Path) -> float:
    """Return duration of source video in seconds via ffprobe/ffmpeg."""
    import shutil as _sh
    ff = _sh.which("ffprobe") or _sh.which("ffmpeg")
    if not ff:
        return 0.0
    cmd = [ff, "-v", "error", "-show_entries", "format=duration",
           "-of", "default=noprint_wrappers=1:nokey=1", str(source_path)]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
        return float(r.stdout.strip())
    except Exception:
        return 0.0


def _run_dinov2_with_exclusions(
    source_path: Path,
    clip_path: Path,
    excluded_ranges: list[tuple[float, float]],
    source_duration: float,
) -> tuple[float, float]:
    """
    Run DINOv2 alignment while skipping forbidden time windows in the source.

    Strategy:
      1. Build list of included segments from source (complement of excluded_ranges).
      2. Use ffmpeg concat to stitch those segments into a temp file.
      3. Build a stitched_time → original_time mapping.
      4. Run dinov2_find_offset on the stitched source.
      5. Map the returned offset back to original source time.
    """
    import shutil as _sh, tempfile as _tmp
    ffmpeg = _sh.which("ffmpeg")
    if not ffmpeg or source_duration <= 0:
        return (0.0, 0.0)

    # Build included segments (gaps between excluded ranges)
    excluded = sorted(excluded_ranges)
    segs: list[tuple[float, float]] = []
    cursor = 0.0
    for ex_start, ex_end in excluded:
        if cursor < ex_start:
            segs.append((cursor, ex_start))
        cursor = max(cursor, ex_end)
    if cursor < source_duration:
        segs.append((cursor, source_duration))

    if not segs:
        return (0.0, 0.0)

    # Build time mapping: list of (stitched_start, original_start, duration)
    time_map: list[tuple[float, float, float]] = []
    stitched_cursor = 0.0
    for orig_start, orig_end in segs:
        dur = orig_end - orig_start
        time_map.append((stitched_cursor, orig_start, dur))
        stitched_cursor += dur

    tmp_dir = Path(_tmp.mkdtemp())
    try:
        # Extract each segment as a temp file
        seg_files: list[Path] = []
        for i, (orig_start, orig_end) in enumerate(segs):
            seg_f = tmp_dir / f"seg_{i}.mp4"
            subprocess.run(
                [ffmpeg, "-y", "-ss", str(orig_start), "-to", str(orig_end),
                 "-i", str(source_path), "-c", "copy", str(seg_f)],
                capture_output=True,
            )
            if seg_f.exists():
                seg_files.append(seg_f)

        if not seg_files:
            return (0.0, 0.0)

        # Concat into stitched source
        concat_list = tmp_dir / "concat.txt"
        concat_list.write_text(
            "\n".join(f"file '{f.absolute()}'" for f in seg_files)
        )
        stitched = tmp_dir / "stitched.mp4"
        subprocess.run(
            [ffmpeg, "-y", "-f", "concat", "-safe", "0",
             "-i", str(concat_list), "-c", "copy", str(stitched)],
            capture_output=True,
        )
        if not stitched.exists():
            return (0.0, 0.0)

        # Run DINOv2 on stitched source
        stitched_offset, conf = _run_dinov2_align(stitched, clip_path)

        # Map stitched offset → original offset
        orig_offset = stitched_offset
        for stitch_start, orig_start, dur in time_map:
            if stitch_start <= stitched_offset < stitch_start + dur:
                orig_offset = orig_start + (stitched_offset - stitch_start)
                break

        return (orig_offset, conf)

    except Exception as e:
        logger.debug("dinov2_with_exclusions failed: %s", e)
        return (0.0, 0.0)
    finally:
        import shutil as _sh2
        _sh2.rmtree(tmp_dir, ignore_errors=True)


def _deduplicate_alignments(
    clip_recs: list[dict],
    post_alignments: list[AlignRecord],
    source_path: Path,
) -> list[AlignRecord]:
    """
    Greedy dedup: highest-confidence clip claims its source window first.
    Subsequent clips whose best offset falls within an already-claimed window
    are re-aligned on the remaining (unclaimed) source regions.
    """
    if len(post_alignments) <= 1:
        return post_alignments

    src_dur = _source_duration(source_path)
    dur_by_clip = {r["clip_id"]: r["duration_sec"] for r in clip_recs}

    # Sort by confidence desc (unmatched last)
    ordered = sorted(
        post_alignments,
        key=lambda a: (a.method != "unmatched", a.confidence),
        reverse=True,
    )

    claimed: list[tuple[float, float]] = []   # (start, end) in source time
    result: list[AlignRecord] = []

    for ar in ordered:
        if ar.method == "unmatched":
            result.append(ar)
            continue

        clip_dur = dur_by_clip.get(ar.clip_id, 5.0)
        # Source window = clip_dur / speed_factor
        # "dinov2_2x" means clip was sped up 2x to align → original clip is 2x slower
        # → it covers clip_dur/2 of source footage
        speed_factor = 2.0 if ar.method == "dinov2_2x" else 1.0
        source_coverage = clip_dur / speed_factor
        win_start = ar.offset_sec
        win_end   = ar.offset_sec + source_coverage

        # Check overlap with any claimed window
        overlap = any(
            not (win_end <= cs or win_start >= ce)
            for cs, ce in claimed
        )

        if not overlap:
            claimed.append((win_start, win_end))
            result.append(ar)
        else:
            # Re-align excluding all claimed windows
            logger.info(
                "dedup: %s offset=%.2fs (source_win=%.2f-%.2fs) conflicts claimed — re-aligning",
                ar.clip_id, ar.offset_sec, win_start, win_end,
            )
            new_offset, new_conf = _run_dinov2_with_exclusions(
                source_path=source_path,
                clip_path=Path(next(
                    r["local_path"] for r in clip_recs if r["clip_id"] == ar.clip_id
                )),
                excluded_ranges=claimed,
                source_duration=src_dur,
            )
            if new_conf >= ALIGN_MIN_ACCEPT:
                new_win = (new_offset, new_offset + source_coverage)
                claimed.append(new_win)
                logger.info(
                    "dedup: %s → new offset=%.2fs conf=%.3f",
                    ar.clip_id, new_offset, new_conf,
                )
                result.append(AlignRecord(
                    clip_id=ar.clip_id,
                    source_clip_id=ar.source_clip_id,
                    offset_sec=new_offset,
                    confidence=new_conf,
                    method="dinov2_dedup",
                    error="",
                    audio_conf=ar.audio_conf,
                    dinov2_conf=new_conf,
                    pose_conf=ar.pose_conf,
                ))
            else:
                result.append(AlignRecord(
                    clip_id=ar.clip_id,
                    source_clip_id=ar.source_clip_id,
                    offset_sec=0.0,
                    confidence=0.0,
                    method="unmatched",
                    error="dedup: no non-overlapping region found",
                    audio_conf=ar.audio_conf,
                    dinov2_conf=ar.dinov2_conf,
                    pose_conf=ar.pose_conf,
                ))

    return result


def _run_pose_align(source_path: Path, clip_path: Path) -> tuple[float, float]:
    """Run YOLO pose motion alignment. Returns (offset_sec, confidence)."""
    try:
        from pulsify.align.motion_align import find_offset as motion_find_offset
        result = motion_find_offset(
            source_video=source_path,
            clip_video=clip_path,
            device="cpu",
        )
        return (result.offset_sec, result.confidence)
    except ImportError:
        logger.debug("Pose align not available")
        return (0.0, 0.0)
    except Exception as e:
        logger.debug("Pose align failed: %s", e)
        return (0.0, 0.0)


def align(state: dict) -> dict:
    """
    Align each fancam clip to its source video using parallel competition.

    All three methods run concurrently:
      1. Audio chroma alignment (with slowmo speed compensation)
      2. DINOv2 diagonal match
      3. YOLO pose motion

    The result with the highest confidence wins.
    All three confidences are recorded for tuning analysis.
    """
    downloads: list[dict] = json.loads(state.get("downloads", "[]"))
    slowmo_info: dict = json.loads(state.get("slowmo_info", "{}"))
    errors: list[str] = json.loads(state.get("errors", "[]"))
    alignments: list[AlignRecord] = []

    # Build lookup: post_id → source records and clip records
    sources_by_post: dict[str, list[dict]] = {}
    clips_by_post: dict[str, list[dict]] = {}

    for rec in downloads:
        pid = rec["post_id"]
        if rec["clip_type"] == "source":
            sources_by_post.setdefault(pid, []).append(rec)
        elif rec["clip_type"] == "clip":
            clips_by_post.setdefault(pid, []).append(rec)

    for post_id, clip_recs in clips_by_post.items():
        source_recs = sources_by_post.get(post_id, [])
        if not source_recs:
            for cr in clip_recs:
                alignments.append(AlignRecord(
                    clip_id=cr["clip_id"],
                    source_clip_id="",
                    offset_sec=0.0, confidence=0.0,
                    method="unmatched", error="no source downloaded",
                ))
            continue

        # Pick best source by pixel count (not file size)
        best_source = max(source_recs, key=lambda r: r["width"] * r["height"])
        post_alignments: list[AlignRecord] = []  # collect per-post, dedup after

        for cr in clip_recs:
            clip_path   = Path(cr["local_path"])
            source_path = Path(best_source["local_path"])

            if not clip_path.exists() or not source_path.exists():
                post_alignments.append(AlignRecord(
                    clip_id=cr["clip_id"],
                    source_clip_id=best_source["clip_id"],
                    offset_sec=0.0, confidence=0.0,
                    method="unmatched", error="file missing",
                ))
                continue

            # Get speed factor for this clip
            clip_slowmo = slowmo_info.get(cr["clip_id"], {})
            speed_factor = clip_slowmo.get("speed_factor", 1.0)

            # ── Parallel competition: run all three methods concurrently ──
            audio_result = (0.0, 0.0)
            dinov2_result = (0.0, 0.0)
            pose_result = (0.0, 0.0)

            with ThreadPoolExecutor(max_workers=3) as executor:
                future_audio = executor.submit(
                    _run_audio_align, source_path, clip_path, speed_factor,
                )
                future_dinov2 = executor.submit(
                    _run_dinov2_align, source_path, clip_path,
                )
                future_pose = executor.submit(
                    _run_pose_align, source_path, clip_path,
                )

                try:
                    audio_result = future_audio.result(timeout=120)
                except Exception as e:
                    logger.debug("audio future error: %s", e)

                try:
                    dinov2_result = future_dinov2.result(timeout=120)
                except Exception as e:
                    logger.debug("dinov2 future error: %s", e)

                try:
                    pose_result = future_pose.result(timeout=120)
                except Exception as e:
                    logger.debug("pose future error: %s", e)

            # ── Select winner: highest confidence ──
            candidates = [
                ("audio",  audio_result[0],  audio_result[1]),
                ("dinov2", dinov2_result[0], dinov2_result[1]),
                ("pose",   pose_result[0],   pose_result[1]),
            ]
            # Sort by confidence descending
            candidates.sort(key=lambda x: x[2], reverse=True)
            best_method, best_offset, best_conf = candidates[0]

            # ── Slowmo retry: if best conf is low, try 2x speed DINOv2 ──────
            # Don't rely on slowmo_detect (FPS/BPM detection unreliable for
            # clips without audio or with frame-duplication slowmo).
            # If the 1x alignment is weak, speed up the clip 2x with ffmpeg
            # and re-run DINOv2. Keep whichever is better.
            if ALIGN_MIN_ACCEPT <= best_conf < SLOWMO_RETRY_THRESHOLD:
                fast_clip = _make_speed_clip(clip_path, factor=2.0)
                if fast_clip:
                    try:
                        r2x_offset, r2x_conf = _run_dinov2_align(source_path, fast_clip)
                        logger.info(
                            "slowmo-retry 2x: %s  conf %.3f → %.3f",
                            cr["clip_id"], best_conf, r2x_conf,
                        )
                        if r2x_conf > best_conf:
                            best_method = "dinov2_2x"
                            best_offset = r2x_offset
                            best_conf   = r2x_conf
                            dinov2_result = (r2x_offset, r2x_conf)
                    except Exception as e:
                        logger.debug("slowmo-retry failed: %s", e)
                    finally:
                        try:
                            fast_clip.unlink(missing_ok=True)
                        except Exception:
                            pass

            if best_conf < ALIGN_MIN_ACCEPT:
                best_method = "unmatched"
                best_offset = 0.0
                error_msg = (
                    f"all methods below threshold: "
                    f"audio={audio_result[1]:.3f} "
                    f"dinov2={dinov2_result[1]:.3f} "
                    f"pose={pose_result[1]:.3f}"
                )
            else:
                error_msg = ""

            post_alignments.append(AlignRecord(
                clip_id=cr["clip_id"],
                source_clip_id=best_source["clip_id"],
                offset_sec=best_offset,
                confidence=best_conf,
                method=best_method,
                error=error_msg,
                audio_conf=audio_result[1],
                dinov2_conf=dinov2_result[1],
                pose_conf=pose_result[1],
            ))

            logger.info(
                "Aligned %s: method=%s offset=%.2fs conf=%.3f "
                "[audio=%.3f dinov2=%.3f pose=%.3f]",
                cr["clip_id"], best_method, best_offset, best_conf,
                audio_result[1], dinov2_result[1], pose_result[1],
            )

        # ── Dedup: resolve clips that mapped to the same source window ───────
        deduped = _deduplicate_alignments(clip_recs, post_alignments, source_path)
        alignments.extend(deduped)

    return {
        "alignments": json.dumps([asdict(a) for a in alignments]),
        "errors": json.dumps(errors),
    }


# ---------------------------------------------------------------------------
# Node: extract_hd   (cut HD segment from source at aligned offset)
# ---------------------------------------------------------------------------

def extract_hd(state: dict) -> dict:
    """
    For every successfully aligned clip, cut the corresponding segment from
    the source video and save it as an HD clip.
    """
    cfg = _load_config(state)
    downloads: list[dict] = json.loads(state.get("downloads", "[]"))
    alignments: list[dict] = json.loads(state.get("alignments", "[]"))
    errors: list[str] = json.loads(state.get("errors", "[]"))
    extracts: list[ExtractRecord] = []

    downloads_by_id = {d["clip_id"]: d for d in downloads}
    align_by_clip = {a["clip_id"]: a for a in alignments}

    hd_dir = Path(cfg.workspace) / "hd_clips"
    hd_dir.mkdir(parents=True, exist_ok=True)

    for clip_id, align_rec in align_by_clip.items():
        if align_rec["method"] == "unmatched":
            continue

        clip_dl = downloads_by_id.get(clip_id)
        src_dl  = downloads_by_id.get(align_rec["source_clip_id"])
        if not clip_dl or not src_dl:
            continue

        src_path  = Path(src_dl["local_path"])
        offset    = align_rec["offset_sec"]
        duration  = clip_dl["duration_sec"]

        if not src_path.exists():
            errors.append(f"extract_hd: source missing {src_path}")
            continue

        out_path = hd_dir / f"{clip_id}_hd.mp4"

        # Use FFmpeg stream copy for speed (no re-encoding)
        try:
            result = subprocess.run(
                [
                    "ffmpeg", "-y",
                    "-ss", str(max(0, offset)),
                    "-i", str(src_path),
                    "-t", str(duration),
                    "-c", "copy",
                    "-avoid_negative_ts", "make_zero",
                    str(out_path),
                ],
                capture_output=True, text=True, timeout=120,
            )
            if result.returncode != 0:
                errors.append(f"extract_hd ffmpeg {clip_id}: {result.stderr[:200]}")
                continue
        except Exception as e:
            errors.append(f"extract_hd {clip_id}: {e}")
            continue

        if not out_path.exists() or out_path.stat().st_size < 1024:
            errors.append(f"extract_hd {clip_id}: output missing or too small")
            continue

        info = _video_info(out_path)
        extracts.append(ExtractRecord(
            clip_id=clip_id,
            source_clip_id=align_rec["source_clip_id"],
            local_path=str(out_path),
            start_in_source=offset,
            end_in_source=offset + duration,
            duration_sec=info.get("duration_sec", duration),
            width=info.get("width", src_dl["width"]),
            height=info.get("height", src_dl["height"]),
            fps=info.get("fps", src_dl["fps"]),
            confidence=align_rec["confidence"],
            method=align_rec["method"],
        ))
        logger.info(
            "extract_hd: %s → %s  [%.2fs–%.2fs @ %dx%d]",
            clip_id, out_path.name,
            offset, offset + duration,
            info.get("width", 0), info.get("height", 0),
        )

    return {
        "extracts": json.dumps([asdict(e) for e in extracts]),
        "errors": json.dumps(errors),
    }


# ---------------------------------------------------------------------------
# Node: select_best_clip  (DESIGN_V1.md §3.7 — creative edit + quality)
# ---------------------------------------------------------------------------

def select_best_clip(state: dict) -> dict:
    """
    For each clip, decide which files go into final/:
      - Creative edit (slowmo/zoom) → keep both versions
      - No creative edit → keep higher quality version
      - Same pixels, different fps → keep both
    """
    cfg = _load_config(state)
    downloads: list[dict] = json.loads(state.get("downloads", "[]"))
    alignments: list[dict] = json.loads(state.get("alignments", "[]"))
    extracts: list[dict] = json.loads(state.get("extracts", "[]"))
    slowmo_info: dict = json.loads(state.get("slowmo_info", "{}"))
    errors: list[str] = json.loads(state.get("errors", "[]"))
    final_clips: list[FinalClipRecord] = []

    from clip_analyzer import zoom_detect

    downloads_by_id = {d["clip_id"]: d for d in downloads}
    align_by_clip = {a["clip_id"]: a for a in alignments}
    extract_by_clip = {e["clip_id"]: e for e in extracts}

    final_dir = Path(cfg.workspace) / "final"

    clips = [d for d in downloads if d["clip_type"] == "clip"]

    for cr in clips:
        clip_id = cr["clip_id"]
        post_id = cr["post_id"]
        clip_path = Path(cr["local_path"])

        if not clip_path.exists():
            continue

        post_final_dir = final_dir / post_id
        post_final_dir.mkdir(parents=True, exist_ok=True)

        # Get slowmo info
        clip_slowmo = slowmo_info.get(clip_id, {})
        is_slowmo = clip_slowmo.get("is_slowmo", False)
        speed_factor = clip_slowmo.get("speed_factor", 1.0)

        # Get alignment info
        align_rec = align_by_clip.get(clip_id)
        hd_extract = extract_by_clip.get(clip_id)

        # Get source download for zoom detection
        source_dl = None
        if align_rec and align_rec.get("source_clip_id"):
            source_dl = downloads_by_id.get(align_rec["source_clip_id"])

        source_path = Path(source_dl["local_path"]) if source_dl else None
        align_offset = align_rec["offset_sec"] if align_rec else 0.0

        # ── Zoom detection ──
        is_zoom_in = False
        zoom_factor = 1.0
        zoom_method = "no_source"

        if source_path and source_path.exists() and align_rec and align_rec["method"] != "unmatched":
            try:
                zr = zoom_detect(
                    clip_path, source_path,
                    align_offset_sec=align_offset,
                    zoom_threshold=ZOOM_THRESHOLD,
                )
                is_zoom_in = zr.is_zoom_in
                zoom_factor = zr.zoom_factor
                zoom_method = zr.method
            except Exception as e:
                logger.warning("zoom_detect failed for %s: %s", clip_id, e)
                errors.append(f"zoom_detect {clip_id}: {e}")

        # ── Creative edit judgment (§3.7) ──
        has_creative_edit = is_slowmo or is_zoom_in

        if has_creative_edit:
            # Keep BOTH: pixeldrain clip (creative) + source extract (original)
            if is_slowmo:
                creative_dst = post_final_dir / f"{clip_id}_slowmo.mp4"
                original_dst = post_final_dir / f"{clip_id}_original.mp4"
            else:  # zoom
                creative_dst = post_final_dir / f"{clip_id}_zoom.mp4"
                original_dst = post_final_dir / f"{clip_id}_fullframe.mp4"

            # Copy creative edit (pixeldrain clip)
            shutil.copy2(str(clip_path), str(creative_dst))

            # Copy original from source (HD extract if available)
            if hd_extract and Path(hd_extract["local_path"]).exists():
                shutil.copy2(hd_extract["local_path"], str(original_dst))
                final_kept = "both"
            else:
                # No HD extract available — only keep creative version
                original_dst = ""
                final_kept = "pixeldrain"

            final_clips.append(FinalClipRecord(
                clip_id=clip_id,
                post_id=post_id,
                final_path=str(creative_dst),
                final_creative_path=str(original_dst) if original_dst else "",
                final_kept=final_kept,
                is_slowmo=is_slowmo,
                speed_factor=speed_factor,
                is_zoom_in=is_zoom_in,
                zoom_factor=zoom_factor,
                zoom_method=zoom_method,
            ))
            logger.info(
                "select_best: %s → creative edit (%s), kept=%s",
                clip_id, "slowmo" if is_slowmo else "zoom", final_kept,
            )

        else:
            # No creative edit — compare quality: pixels then fps
            clip_pixels = cr["width"] * cr["height"]
            clip_fps = cr["fps"]

            hd_pixels = 0
            hd_fps = 0.0
            hd_path = None
            if hd_extract and Path(hd_extract["local_path"]).exists():
                hd_pixels = hd_extract["width"] * hd_extract["height"]
                hd_fps = hd_extract["fps"]
                hd_path = Path(hd_extract["local_path"])

            if hd_path and hd_pixels > clip_pixels:
                # Source HD is better → use it
                dst = post_final_dir / f"{clip_id}.mp4"
                shutil.copy2(str(hd_path), str(dst))
                final_clips.append(FinalClipRecord(
                    clip_id=clip_id,
                    post_id=post_id,
                    final_path=str(dst),
                    final_kept="source_hd",
                    zoom_method=zoom_method,
                ))
                logger.info(
                    "select_best: %s → source_hd (%dx%d > %dx%d)",
                    clip_id, hd_extract["width"], hd_extract["height"],
                    cr["width"], cr["height"],
                )

            elif hd_path and hd_pixels == clip_pixels and abs(hd_fps - clip_fps) > 1.0:
                # Same pixels, different fps → keep BOTH
                dst_clip = post_final_dir / f"{clip_id}_pd.mp4"
                dst_hd = post_final_dir / f"{clip_id}_hd.mp4"
                shutil.copy2(str(clip_path), str(dst_clip))
                shutil.copy2(str(hd_path), str(dst_hd))
                final_clips.append(FinalClipRecord(
                    clip_id=clip_id,
                    post_id=post_id,
                    final_path=str(dst_clip),
                    final_creative_path=str(dst_hd),
                    final_kept="both",
                    zoom_method=zoom_method,
                ))
                logger.info(
                    "select_best: %s → both (same pixels, fps: %.1f vs %.1f)",
                    clip_id, clip_fps, hd_fps,
                )

            else:
                # Pixeldrain clip is better or equal → use it
                dst = post_final_dir / f"{clip_id}.mp4"
                shutil.copy2(str(clip_path), str(dst))
                final_clips.append(FinalClipRecord(
                    clip_id=clip_id,
                    post_id=post_id,
                    final_path=str(dst),
                    final_kept="pixeldrain",
                    zoom_method=zoom_method,
                ))
                logger.info("select_best: %s → pixeldrain", clip_id)

    return {
        "final_clips": json.dumps([asdict(f) for f in final_clips]),
        "errors": json.dumps(errors),
    }


# ---------------------------------------------------------------------------
# Node: parse_post_metadata   (DESIGN_V1.md §3.8 — rule-based, layer 1)
# ---------------------------------------------------------------------------

# Date patterns: YYMMDD or YYYYMMDD at start of filename / title
_DATE_YMD6_RE  = re.compile(r"\b(\d{2})(\d{2})(\d{2})\b")   # 260517 → 2026-05-17
_DATE_YMD8_RE  = re.compile(r"\b(20\d{2})(\d{2})(\d{2})\b") # 20260517

# Common k-pop girl group names (extend as needed)
_KNOWN_GROUPS = [
    "TWICE", "BLACKPINK", "aespa", "NewJeans", "IVE", "BABYMONSTER",
    "LE SSERAFIM", "MAMAMOO", "Red Velvet", "ITZY", "Stray Kids",
    "ENHYPEN", "(G)I-DLE", "NMIXX", "KISS OF LIFE", "tripleS",
    "OH MY GIRL", "SISTAR", "T-ARA", "APINK", "EXID", "AOA",
    "fromis_9", "Kep1er", "VIVIZ", "Brave Girls", "WJSN",
    "LOONA", "Dreamcatcher", "EVERGLOW", "STAYC", "LIGHTSUM",
    "Purple Kiss", "Billlie", "CLASS:y", "FIFTY FIFTY", "ILLIT",
    "QWER", "H1-KEY",
]
_GROUP_RE = re.compile(
    r"\b(" + "|".join(re.escape(g) for g in _KNOWN_GROUPS) + r")\b",
    re.IGNORECASE,
)

# Known members / solo artists — (name, group) pairs
_KNOWN_MEMBERS: list[tuple[str, str]] = [
    # TWICE
    ("Nayeon", "TWICE"), ("Jeongyeon", "TWICE"), ("Momo", "TWICE"),
    ("Sana", "TWICE"), ("Jihyo", "TWICE"), ("Mina", "TWICE"),
    ("Dahyun", "TWICE"), ("Chaeyoung", "TWICE"), ("Tzuyu", "TWICE"),
    # BLACKPINK
    ("Jisoo", "BLACKPINK"), ("Jennie", "BLACKPINK"),
    ("Rosé", "BLACKPINK"), ("Lisa", "BLACKPINK"),
    # aespa
    ("Karina", "aespa"), ("Giselle", "aespa"),
    ("Winter", "aespa"), ("Ningning", "aespa"),
    # NewJeans
    ("Minji", "NewJeans"), ("Hanni", "NewJeans"), ("Danielle", "NewJeans"),
    ("Haerin", "NewJeans"), ("Hyein", "NewJeans"),
    # IVE
    ("Yujin", "IVE"), ("Gaeul", "IVE"), ("Rei", "IVE"),
    ("Wonyoung", "IVE"), ("Liz", "IVE"), ("Leeseo", "IVE"),
    # BABYMONSTER
    ("Ruka", "BABYMONSTER"), ("Pharita", "BABYMONSTER"),
    ("Asa", "BABYMONSTER"), ("Rami", "BABYMONSTER"),
    ("Rora", "BABYMONSTER"), ("Chiquita", "BABYMONSTER"),
    ("Ahyeon", "BABYMONSTER"),
    # LE SSERAFIM
    ("Sakura", "LE SSERAFIM"), ("Chaewon", "LE SSERAFIM"),
    ("Yunjin", "LE SSERAFIM"), ("Kazuha", "LE SSERAFIM"),
    ("Eunchae", "LE SSERAFIM"),
    # ITZY
    ("Yeji", "ITZY"), ("Lia", "ITZY"), ("Ryujin", "ITZY"),
    ("Chaeryeong", "ITZY"), ("Yuna", "ITZY"),
    # (G)I-DLE
    ("Miyeon", "(G)I-DLE"), ("Minnie", "(G)I-DLE"),
    ("Soyeon", "(G)I-DLE"), ("Yuqi", "(G)I-DLE"), ("Shuhua", "(G)I-DLE"),
    # NMIXX
    ("Lily", "NMIXX"), ("Haewon", "NMIXX"), ("Sullyoon", "NMIXX"),
    ("Jinni", "NMIXX"), ("Bae", "NMIXX"), ("Jiwoo", "NMIXX"), ("Kyujin", "NMIXX"),
    # STAYC
    ("Sumin", "STAYC"), ("Sieun", "STAYC"), ("Isa", "STAYC"),
    ("Seeun", "STAYC"), ("Yoon", "STAYC"), ("J", "STAYC"),
    # QWER
    ("Hina", "QWER"), ("Siyeon", "QWER"), ("Magenta", "QWER"), ("Chodan", "QWER"),
]
_MEMBER_RE = re.compile(
    r"\b(" + "|".join(re.escape(m[0]) for m in _KNOWN_MEMBERS) + r")\b",
    re.IGNORECASE,
)
_MEMBER_TO_GROUP = {m[0].lower(): m[1] for m in _KNOWN_MEMBERS}


def _parse_date(text: str) -> str:
    """Extract performance date from filename or title. Returns 'YYYY-MM-DD' or ''."""
    # Try YYYYMMDD first (more specific)
    m = _DATE_YMD8_RE.search(text)
    if m:
        return f"{m.group(1)}-{m.group(2)}-{m.group(3)}"
    # Try YYMMDD (assume 20xx)
    m = _DATE_YMD6_RE.search(text)
    if m:
        yy, mm, dd = m.group(1), m.group(2), m.group(3)
        # Validate month/day range to avoid false matches
        if 1 <= int(mm) <= 12 and 1 <= int(dd) <= 31:
            return f"20{yy}-{mm}-{dd}"
    return ""


def _parse_song_name(title: str, performers: list[str], group: str) -> str:
    """
    Heuristically extract song name from post title.
    Strategy: remove known group/member names, date patterns, and common
    bracket noise; what remains is likely the song name.
    """
    text = title
    # Remove group + member names
    text = _GROUP_RE.sub("", text)
    text = _MEMBER_RE.sub("", text)
    # Remove date patterns
    text = _DATE_YMD8_RE.sub("", text)
    text = _DATE_YMD6_RE.sub("", text)
    # Remove common bracket noise like [직캠] [Fancam] (4K) etc.
    text = re.sub(r"[\[\(][^\]\)]{1,30}[\]\)]", "", text)
    # Remove common suffixes
    text = re.sub(
        r"\b(fancam|fan\s*cam|직캠|4k|hd|60fps|stage|focus|직캠|mc)\b",
        "", text, flags=re.IGNORECASE,
    )
    # Collapse whitespace and strip punctuation edges
    text = re.sub(r"[_\-|]+", " ", text)
    text = re.sub(r"\s+", " ", text).strip(" :-|/\\")
    return text if len(text) > 1 else ""


def parse_post_metadata(state: dict) -> dict:
    """
    Parse performance date, song name, and performers from post title and
    downloaded filenames. Results stored as PostMetadata list.

    Falls back gracefully — empty strings for fields that can't be parsed.
    LLM identify step can override/enrich these later.
    """
    posts_raw: list[dict] = json.loads(state.get("posts", "[]"))
    downloads: list[dict] = json.loads(state.get("downloads", "[]"))
    errors: list[str] = json.loads(state.get("errors", "[]"))
    post_metas: list[PostMetadata] = []

    # Build set of filenames per post for richer parsing
    filenames_by_post: dict[str, list[str]] = {}
    for dl in downloads:
        filenames_by_post.setdefault(dl["post_id"], []).append(
            Path(dl["local_path"]).name
        )

    for post in posts_raw:
        post_id = post.get("post_id", post.get("id", ""))
        title   = post.get("title", "")
        filenames = filenames_by_post.get(post_id, [])

        # Search title + all filenames for metadata
        search_texts = [title] + filenames

        # Date: try title first, then filenames
        perf_date = ""
        for text in search_texts:
            perf_date = _parse_date(text)
            if perf_date:
                break

        # Performers + group
        performers: list[str] = []
        group_name = ""

        all_text = " ".join(search_texts)

        # Find group name
        gm = _GROUP_RE.search(all_text)
        if gm:
            found = gm.group(1).lower()
            for g in _KNOWN_GROUPS:
                if g.lower() == found:
                    group_name = g
                    break

        # Find individual members
        for mm in _MEMBER_RE.finditer(all_text):
            name_found = mm.group(1)
            for name, grp in _KNOWN_MEMBERS:
                if name.lower() == name_found.lower():
                    if name not in performers:
                        performers.append(name)
                    if not group_name:
                        group_name = grp
                    break

        # Song name
        song = _parse_song_name(title, performers, group_name)

        post_metas.append(PostMetadata(
            post_id=post_id,
            performance_date=perf_date,
            song_name=song,
            performers=performers,
            group_name=group_name,
            parse_method="filename" if perf_date and (performers or group_name) else "title",
        ))

        logger.info(
            "parse_post_metadata: post=%s  date=%s  group=%s  performers=%s  song=%r",
            post_id, perf_date, group_name, performers, song,
        )

    return {
        "post_metas": json.dumps([asdict(m) for m in post_metas]),
        "errors": json.dumps(errors),
    }


# ---------------------------------------------------------------------------
# Node: store_results  (DESIGN_V1.md §3.8 — write to SQLite)
# ---------------------------------------------------------------------------

def store_results(state: dict, *, db_path: str = "") -> dict:
    """
    Write all pipeline results into SQLite:
      - posts table (with metadata + initial upvote)
      - clips table (with alignment + creative edit + final paths)
      - upvote_log (initial score)
      - metadata_training_log (rule parse results for V2 improvement)

    Returns _db_stats with counts for crawl_log.
    """
    from storage.database import (
        get_connection, init_db, upsert_post, upsert_clip,
        log_upvote, settle_post, log_metadata_training,
    )

    if not db_path:
        cfg = _load_config(state)
        db_path = str(Path(cfg.workspace) / "fancam.db")

    init_db(db_path)

    posts_raw: list[dict] = json.loads(state.get("posts", "[]"))
    downloads: list[dict] = json.loads(state.get("downloads", "[]"))
    alignments: list[dict] = json.loads(state.get("alignments", "[]"))
    final_clips: list[dict] = json.loads(state.get("final_clips", "[]"))
    slowmo_info: dict = json.loads(state.get("slowmo_info", "{}"))
    post_metas: list[dict] = json.loads(state.get("post_metas", "[]"))
    errors: list[str] = json.loads(state.get("errors", "[]"))

    # Build lookups
    align_by_clip = {a["clip_id"]: a for a in alignments}
    final_by_clip = {f["clip_id"]: f for f in final_clips}
    meta_by_post = {m["post_id"]: m for m in post_metas}

    now = time.time()
    clips_new = 0

    with get_connection(db_path) as conn:
        # ── Write posts ──
        for post_data in posts_raw:
            post_id = post_data["post_id"]
            meta = meta_by_post.get(post_id, {})

            upsert_post(
                conn,
                post_id=post_id,
                subreddit=post_data.get("subreddit", ""),
                title=post_data.get("title", ""),
                created_utc=post_data.get("created_utc", 0.0),
                reddit_url=post_data.get("url", ""),
                score=post_data.get("score", 0),
                group_name=meta.get("group_name", ""),
                performer=", ".join(meta.get("performers", [])),
                song=meta.get("song_name", ""),
                perf_date=meta.get("performance_date", ""),
                crawled_at=now,
            )

            # ── Upvote tracking (§3.8) ──
            created_utc = post_data.get("created_utc", 0.0)
            score = post_data.get("score", 0)
            post_age_h = (now - created_utc) / 3600.0 if created_utc > 0 else 999

            if post_age_h < _SETTLED_HOURS:
                log_upvote(conn, post_id=post_id, score=score,
                           post_age_hours=post_age_h)
            else:
                settle_post(conn, post_id, score)

            # ── Metadata training log ──
            filenames = [
                Path(d["local_path"]).name
                for d in downloads
                if d["post_id"] == post_id and d.get("pixeldrain_filename")
            ]
            log_metadata_training(
                conn,
                post_id=post_id,
                raw_title=post_data.get("title", ""),
                filename=filenames[0] if filenames else "",
                rule_result=meta if meta else None,
            )

        # ── Write clips ──
        for dl in downloads:
            if dl["clip_type"] not in ("clip", "source"):
                continue

            clip_id = dl["clip_id"]
            align_rec = align_by_clip.get(clip_id, {})
            final_rec = final_by_clip.get(clip_id, {})
            clip_slowmo = slowmo_info.get(clip_id, {})

            upsert_clip(
                conn,
                clip_id=clip_id,
                post_id=dl["post_id"],
                pixeldrain_filename=dl.get("pixeldrain_filename", ""),
                clip_type=dl["clip_type"],
                local_path=dl["local_path"],
                width=dl["width"],
                height=dl["height"],
                fps=dl["fps"],
                duration_sec=dl["duration_sec"],
                is_slowmo=clip_slowmo.get("is_slowmo", False),
                speed_factor=clip_slowmo.get("speed_factor", 1.0),
                is_zoom_in=final_rec.get("is_zoom_in", False),
                zoom_factor=final_rec.get("zoom_factor", 1.0),
                zoom_method=final_rec.get("zoom_method", ""),
                align_method=align_rec.get("method", ""),
                align_offset_sec=align_rec.get("offset_sec"),
                align_confidence=align_rec.get("confidence"),
                align_audio_conf=align_rec.get("audio_conf"),
                align_dinov2_conf=align_rec.get("dinov2_conf"),
                align_pose_conf=align_rec.get("pose_conf"),
                source_clip_id=align_rec.get("source_clip_id", ""),
                final_path=final_rec.get("final_path", ""),
                final_creative_path=final_rec.get("final_creative_path", ""),
                final_kept=final_rec.get("final_kept", ""),
            )
            clips_new += 1

    return {
        "_db_stats": json.dumps({"clips_new": clips_new}),
        "errors": json.dumps(errors),
    }
