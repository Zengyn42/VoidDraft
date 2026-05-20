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
    clip_type: str              # "source" | "clip" | "merged" | "external_ref"
    url: str
    local_path: str             # absolute path
    file_size_bytes: int
    duration_sec: float
    width: int
    height: int
    fps: float
    download_method: str        # "yt-dlp" | "jdownloader" | "pixeldrain" | "split"
    pixeldrain_filename: str = ""  # original Pixeldrain filename (for album diff)


@dataclass
class AlignRecord:
    clip_id: str
    source_clip_id: str        # which source download this aligns to
    offset_sec: float
    confidence: float
    method: str                # "audio" | "dinov2" | "pose" | "unmatched"
    # Per-layer raw confidence values (recorded for tuning regardless of winner)
    audio_conf: float = 0.0
    dinov2_conf: float = 0.0
    pose_conf: float = 0.0
    error: str = ""


@dataclass
class SlowmoInfo:
    clip_id: str
    is_slowmo: bool
    speed_factor: float        # 1.0 = normal, 2.0 = 2× slower
    detected_by: str           # "fps_metadata"|"audio_bpm"|"flow_magnitude"|"none"


@dataclass
class FinalClipRecord:
    clip_id: str
    post_id: str
    final_path: str            # main output file in final/
    creative_path: str = ""    # creative-edit version (_slowmo / _zoom), if any
    source_hd_path: str = ""   # source-extracted version (_original / _fullframe)
    final_kept: str = ""       # "pixeldrain" | "source_hd" | "both"
    is_slowmo: bool = False
    speed_factor: float = 1.0
    is_zoom_in: bool = False
    zoom_factor: float = 1.0
    zoom_method: str = ""


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
    from pulsify.utils.video_info import get_video_info
    return get_video_info(path)


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

        # --- Download pixeldrain clips (with source/merged classification) ---
        import re as _re
        from clip_analyzer import (
            parse_clip_filename, PixeldrainFile, identify_source_in_album,
            fill_probe_info,
        )

        source_platform = post_data.get("platform", "")

        for pd_url in post_data.get("pixeldrain_urls", []):
            dest_dir = cfg.clips_dir / post_id
            dest_dir.mkdir(parents=True, exist_ok=True)

            # Pre-fetch album metadata to classify files before downloading.
            # ALWAYS run even when we already have an external source — Pixeldrain
            # native source takes priority if it has higher resolution.
            list_match = _re.search(r"pixeldrain\.com/l/([A-Za-z0-9_-]+)", pd_url)
            album_files_raw: list[dict] = []
            source_file_id: str | None = None
            album_youtube_ids: list[str] = []  # IDs from filenames like [youtube@ID]

            if list_match:
                list_id = list_match.group(1)
                album_files_raw = pd.list_album(list_id)
                pd_files = [
                    PixeldrainFile(
                        file_id=f["id"],
                        name=f.get("name", f["id"]),
                        size_bytes=f.get("size", 0),
                    )
                    for f in album_files_raw
                ]

                # Priority 1: Pixeldrain native source candidate
                source_result = identify_source_in_album(pd_files, source_platform)
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

            # If Pixeldrain has no native source, try [youtube@ID] fallback.
            # These are added to source_urls only when the post didn't already
            # provide them (dedup handled by _extract_source_links).
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
                    # Priority check: Pixeldrain source wins if higher resolution.
                    # Demote any previously recorded external sources that are lower-res.
                    pd_pixels = info.get("width", 0) * info.get("height", 0)
                    for i, r in enumerate(records):
                        if r.post_id != post_id or r.clip_type != "source":
                            continue
                        ext_pixels = r.width * r.height
                        if pd_pixels >= ext_pixels:
                            logger.info(
                                "Pixeldrain source (%dx%d) ≥ external source (%dx%d) "
                                "— demoting external source '%s' to clip",
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
                    duration_sec=info["duration_sec"],
                    width=info["width"],
                    height=info["height"],
                    fps=info["fps"],
                    download_method="pixeldrain",
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

    Individual clips already present (same post) are used for cut verification
    to filter false positives.

    The original merged record stays in downloads (clip_type="merged") so it
    can still be used as reference. New segment records get clip_type="clip".
    """
    cfg = _load_config(state)
    downloads: list[dict] = json.loads(state.get("downloads", "[]"))
    errors: list[str] = json.loads(state.get("errors", "[]"))

    from pulsify.align.scene_cut import detect_cuts, verify_cuts, flag_duplicate_matches

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
        # The Pixeldrain album already contains the individual segments.
        # Splitting the merged file would produce duplicates and wasted I/O.
        if individual_clips:
            logger.info(
                "split_merged: post=%s already has %d individual clip(s) — "
                "skipping split of %s",
                post_id, len(individual_clips), merged_path.name,
            )
            continue

        # Detect cuts
        try:
            cut_timestamps = detect_cuts(merged_path, threshold=25.0, min_gap_sec=1.0)
        except Exception as e:
            errors.append(f"split_merged detect_cuts {merged_path.name}: {e}")
            continue

        if not cut_timestamps:
            logger.info("split_merged: no cuts found in %s — treating as single clip", merged_path.name)
            continue

        logger.info(
            "split_merged: %s → %d cut(s) at %s",
            merged_path.name,
            len(cut_timestamps),
            [f"{t:.2f}s" for t in cut_timestamps],
        )

        # Verify cuts against individual clips (filter false positives)
        verified_cuts = cut_timestamps
        if individual_clips:
            try:
                verifications = verify_cuts(merged_path, individual_clips, cut_timestamps)
                verifications = flag_duplicate_matches(verifications)
                # Keep only verified cuts
                verified_cuts = [
                    v.cut_timestamp for v in verifications if v.verified
                ]
                false_cuts = [v for v in verifications if not v.verified]
                if false_cuts:
                    logger.info(
                        "split_merged: removed %d false cut(s): %s",
                        len(false_cuts),
                        [f"{v.cut_timestamp:.2f}s" for v in false_cuts],
                    )
            except Exception as e:
                logger.warning("split_merged: verify_cuts failed (%s) — using raw cuts", e)

        # Split the merged file at verified cut points
        seg_dir = Path(rec["local_path"]).parent / f"_segments_{merged_path.stem}"
        seg_dir.mkdir(parents=True, exist_ok=True)

        try:
            from pulsify.align.scene_cut import split_at_cuts
            seg_paths = split_at_cuts(merged_path, verified_cuts, seg_dir)
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
                "duration_sec": info["duration_sec"],
                "width": info["width"],
                "height": info["height"],
                "fps": info["fps"],
                "download_method": "split",
            })
            logger.info(
                "split_merged: segment %d → %s (%.2fs)",
                i, seg_path.name, info["duration_sec"],
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
# Node: align
# ---------------------------------------------------------------------------

# Minimum pose confidence to trust motion_find_offset result
_POSE_CONF_THRESHOLD  = 0.55
# Minimum audio confidence to trust audio cross-correlation result
_AUDIO_CONF_THRESHOLD = 0.10

def align(state: dict) -> dict:
    """
    Align each fancam clip to its source video.

    Strategy (per clip, in order):
      1. Audio cross-correlation  — ms-level accuracy, camera-angle independent
      2. DINOv2 diagonal match    — semantic frame embeddings, robust to different
                                    camera angles, no audio needed  (fallback)
      3. Pose motion alignment    — Pulsify motion_find_offset  (last resort)
    """
    downloads: list[dict] = json.loads(state.get("downloads", "[]"))
    errors: list[str] = json.loads(state.get("errors", "[]"))
    alignments: list[AlignRecord] = []

    from pulsify.align import audio_find_offset, dinov2_find_offset
    try:
        from pulsify.align import motion_find_offset, MotionAlignError
        _pose_available = True
    except ImportError:
        _pose_available = False

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

        best_source = max(source_recs, key=lambda r: r["file_size_bytes"])

        for cr in clip_recs:
            clip_path   = Path(cr["local_path"])
            source_path = Path(best_source["local_path"])

            if not clip_path.exists() or not source_path.exists():
                alignments.append(AlignRecord(
                    clip_id=cr["clip_id"],
                    source_clip_id=best_source["clip_id"],
                    offset_sec=0.0, confidence=0.0,
                    method="unmatched", error="file missing",
                ))
                continue

            # ── Strategy 1: audio cross-correlation ──────────────────────
            audio_offset, audio_conf = audio_find_offset(source_path, clip_path)

            if audio_offset >= 0 and audio_conf >= _AUDIO_CONF_THRESHOLD:
                alignments.append(AlignRecord(
                    clip_id=cr["clip_id"],
                    source_clip_id=best_source["clip_id"],
                    offset_sec=audio_offset,
                    confidence=min(1.0, audio_conf * 5),  # scale to [0,1] range
                    method="audio",
                ))
                logger.info(
                    "Aligned %s via audio: offset=%.3fs  raw_conf=%.3f",
                    cr["clip_id"], audio_offset, audio_conf,
                )
                continue

            # ── Strategy 2: DINOv2 diagonal alignment (no-audio fallback) ──
            dino_offset, dino_conf = dinov2_find_offset(source_path, clip_path)
            _DINO_CONF_THRESHOLD = 0.50

            if dino_offset >= 0 and dino_conf >= _DINO_CONF_THRESHOLD:
                alignments.append(AlignRecord(
                    clip_id=cr["clip_id"],
                    source_clip_id=best_source["clip_id"],
                    offset_sec=dino_offset,
                    confidence=dino_conf,
                    method="dinov2",
                ))
                logger.info(
                    "Aligned %s via DINOv2: offset=%.3fs  conf=%.3f",
                    cr["clip_id"], dino_offset, dino_conf,
                )
                continue

            # ── Strategy 3: pose motion alignment (last resort) ──────────
            if not _pose_available:
                alignments.append(AlignRecord(
                    clip_id=cr["clip_id"],
                    source_clip_id=best_source["clip_id"],
                    offset_sec=0.0, confidence=0.0,
                    method="unmatched",
                    error=f"audio conf={audio_conf:.3f}, dino conf={dino_conf:.3f}, pose unavailable",
                ))
                continue

            try:
                result = motion_find_offset(
                    source_video=source_path,
                    clip_video=clip_path,
                    device="cpu",
                )
                alignments.append(AlignRecord(
                    clip_id=cr["clip_id"],
                    source_clip_id=best_source["clip_id"],
                    offset_sec=result.offset_sec,
                    confidence=result.confidence,
                    method=result.method,
                ))
                logger.info(
                    "Aligned %s via pose: offset=%.2fs conf=%.2f",
                    cr["clip_id"], result.offset_sec, result.confidence,
                )
            except MotionAlignError as e:
                logger.warning("Pose alignment failed for %s: %s", cr["clip_id"], e)
                alignments.append(AlignRecord(
                    clip_id=cr["clip_id"],
                    source_clip_id=best_source["clip_id"],
                    offset_sec=0.0, confidence=0.0,
                    method="unmatched", error=str(e),
                ))

    return {
        "alignments": json.dumps([asdict(a) for a in alignments]),
        "errors": json.dumps(errors),
    }


# ---------------------------------------------------------------------------
# Node: extract_hd   (step 4 + 5)
# ---------------------------------------------------------------------------

def extract_hd(state: dict) -> dict:
    """
    For every successfully aligned clip, cut the corresponding segment from
    the source video (which is usually higher resolution) and save it as a
    backup HD clip.

    Each ExtractRecord stores:
      - start_in_source / end_in_source  (timestamps in the source file)
      - local_path of the extracted file
      - alignment confidence + method
    """
    cfg = _load_config(state)
    downloads: list[dict] = json.loads(state.get("downloads", "[]"))
    alignments: list[dict] = json.loads(state.get("alignments", "[]"))
    errors: list[str] = json.loads(state.get("errors", "[]"))
    extracts: list[ExtractRecord] = []

    try:
        from pulsify.video_clipper import VideoClipper
        clipper = VideoClipper()
    except ImportError as e:
        errors.append(f"extract_hd: VideoClipper unavailable: {e}")
        return {"extracts": "[]", "errors": json.dumps(errors)}

    downloads_by_id = {d["clip_id"]: d for d in downloads}
    align_by_clip = {a["clip_id"]: a for a in alignments}

    hd_dir = Path(cfg.workspace) / "hd_clips"
    hd_dir.mkdir(parents=True, exist_ok=True)

    for clip_id, align_rec in align_by_clip.items():
        if align_rec["method"] == "unmatched":
            continue
        if align_rec["confidence"] < 0.40:
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
        try:
            success = clipper.extract_clip(
                input_file=str(src_path),
                start_time=offset,
                duration=duration,
                output_file=str(out_path),
            )
        except Exception as e:
            errors.append(f"extract_hd {clip_id}: {e}")
            continue

        if not success or not out_path.exists():
            errors.append(f"extract_hd {clip_id}: extraction returned no file")
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
# Node: parse_post_metadata   (step 6)
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
            # Normalize case to known group name
            found = gm.group(1).lower()
            for g in _KNOWN_GROUPS:
                if g.lower() == found:
                    group_name = g
                    break

        # Find individual members
        for mm in _MEMBER_RE.finditer(all_text):
            name_found = mm.group(1)
            # Normalize to known member name
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
