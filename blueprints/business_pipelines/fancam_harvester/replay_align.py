"""
replay_align.py — Re-run align → extract_hd → select_best_clip → store_results
on already-downloaded clips, without re-downloading.

Usage:
    python replay_align.py --post-id 1tj9md0
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
from dataclasses import asdict
from pathlib import Path

_PIPELINE_DIR      = Path(__file__).parent
_VOIDDRAFT         = _PIPELINE_DIR.parent.parent
_LIB               = _VOIDDRAFT / "lib"
_CONTENT_RETRIEVER = _VOIDDRAFT / "pipelines" / "content_retriever"

for p in [str(_PIPELINE_DIR), str(_LIB), str(_VOIDDRAFT), str(_CONTENT_RETRIEVER)]:
    if p not in sys.path:
        sys.path.insert(0, p)

from config import FancamConfig  # noqa: E402
import validators                # noqa: E402

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)

_DEFAULT_WORKSPACE = str(Path.home() / "Foundation" / "EdenGateway" / "RedditPulsify")


def _build_downloads_from_disk(post_id: str, cfg: FancamConfig) -> list[dict]:
    """Reconstruct downloads list from already-downloaded files on disk."""
    try:
        from pulsify.utils.video_info import get_video_info
    except ImportError:
        from validators import _video_info as get_video_info

    records = []

    # Source videos
    src_dir = Path(cfg.source_dir) / post_id
    if src_dir.exists():
        for i, f in enumerate(sorted(src_dir.glob("*.mp4"))):
            info = get_video_info(f)
            records.append({
                "post_id": post_id,
                "clip_id": f"{post_id}_src{i}",
                "clip_type": "source",
                "url": "",
                "local_path": str(f),
                "file_size_bytes": f.stat().st_size,
                "duration_sec": info.get("duration_sec", 0.0),
                "width": info.get("width", 0),
                "height": info.get("height", 0),
                "fps": info.get("fps", 0.0),
                "download_method": "disk",
                "pixeldrain_filename": "",
            })
            logger.info("  source: %s  dur=%.2fs  %dx%d",
                        f.name, info.get("duration_sec", 0), info.get("width", 0), info.get("height", 0))

    # Segment clips (from _segments_* subdirs)
    clip_dir = Path(cfg.clips_dir) / post_id
    if clip_dir.exists():
        seg_count = 0
        for seg_dir in sorted(clip_dir.iterdir()):
            if not seg_dir.is_dir() or not seg_dir.name.startswith("_segments_"):
                continue
            for j, f in enumerate(sorted(seg_dir.glob("*.mp4"))):
                info = get_video_info(f)
                clip_id = f"{post_id}_{seg_dir.name}_seg{j}"
                records.append({
                    "post_id": post_id,
                    "clip_id": clip_id,
                    "clip_type": "clip",
                    "url": "",
                    "local_path": str(f),
                    "file_size_bytes": f.stat().st_size,
                    "duration_sec": info.get("duration_sec", 0.0),
                    "width": info.get("width", 0),
                    "height": info.get("height", 0),
                    "fps": info.get("fps", 0.0),
                    "download_method": "disk",
                    "pixeldrain_filename": "",
                })
                seg_count += 1
                logger.info("  clip: %s  dur=%.2fs  %dx%d",
                            f.name, info.get("duration_sec", 0), info.get("width", 0), info.get("height", 0))

        # Also check top-level clip files (non-segmented)
        for f in sorted(clip_dir.glob("*.mp4")):
            info = get_video_info(f)
            records.append({
                "post_id": post_id,
                "clip_id": f"{post_id}_{f.stem}",
                "clip_type": "clip",
                "url": "",
                "local_path": str(f),
                "file_size_bytes": f.stat().st_size,
                "duration_sec": info.get("duration_sec", 0.0),
                "width": info.get("width", 0),
                "height": info.get("height", 0),
                "fps": info.get("fps", 0.0),
                "download_method": "disk",
                "pixeldrain_filename": f.name,
            })
            logger.info("  clip (top): %s  dur=%.2fs", f.name, info.get("duration_sec", 0))

    return records


def replay(post_id: str, workspace: str) -> None:
    cfg = FancamConfig(workspace=workspace)
    cfg.ensure_dirs()

    post_data = {
        "post_id": post_id,
        "title": f"replay:{post_id}",
        "url": "",
        "subreddit": "kpopfap",
        "source_urls": [],
        "pixeldrain_urls": [],
        "text": "",
        "created_utc": 0.0,
        "score": 0,
    }

    logger.info("=== replay_align: post=%s ===", post_id)
    logger.info("Scanning disk for existing downloads...")
    downloads = _build_downloads_from_disk(post_id, cfg)
    logger.info("Found %d records (%d sources, %d clips)",
                len(downloads),
                sum(1 for r in downloads if r["clip_type"] == "source"),
                sum(1 for r in downloads if r["clip_type"] == "clip"))

    state = {
        "config":      json.dumps(asdict(cfg)),
        "posts":       json.dumps([post_data]),
        "downloads":   json.dumps(downloads),
        "slowmo_info": "{}",
        "alignments":  "[]",
        "extracts":    "[]",
        "final_clips": "[]",
        "post_metas":  "[]",
        "errors":      "[]",
    }

    logger.info("→ align (1x vs 2x competition + dedup)")
    state.update(validators.align(state))

    # Print alignment results
    alignments = json.loads(state["alignments"])
    logger.info("Alignment results:")
    for a in sorted(alignments, key=lambda x: x["confidence"], reverse=True):
        logger.info(
            "  %s  method=%-14s  offset=%6.2fs  conf=%.3f  [audio=%.3f dinov2=%.3f pose=%.3f]",
            a["clip_id"], a["method"], a["offset_sec"], a["confidence"],
            a.get("audio_conf", 0), a.get("dinov2_conf", 0), a.get("pose_conf", 0),
        )

    logger.info("→ extract_hd")
    state.update(validators.extract_hd(state))

    logger.info("→ select_best_clip")
    state.update(validators.select_best_clip(state))

    logger.info("→ parse_post_metadata")
    state.update(validators.parse_post_metadata(state))

    db = str(Path(workspace) / "fancam.db")
    logger.info("→ store_results → %s", db)
    state.update(validators.store_results(state, db_path=db))

    errs = json.loads(state.get("errors", "[]"))
    if errs:
        logger.warning("%d error(s):", len(errs))
        for e in errs:
            logger.warning("  %s", e)

    # Print DB summary
    try:
        import sqlite3
        conn = sqlite3.connect(db)
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT clip_id, align_method, align_offset_sec, align_confidence "
            "FROM clips WHERE post_id=? ORDER BY align_confidence DESC",
            (post_id,)
        ).fetchall()
        logger.info("=== DB clips for post=%s ===", post_id)
        for r in rows:
            logger.info("  %s  method=%-14s  offset=%.2fs  conf=%.3f",
                        r["clip_id"], r["align_method"] or "-",
                        r["align_offset_sec"] or 0, r["align_confidence"] or 0)
        conn.close()
    except Exception as e:
        logger.warning("DB summary failed: %s", e)

    logger.info("=== done ===")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--post-id", required=True)
    parser.add_argument("--workspace", default=_DEFAULT_WORKSPACE)
    args = parser.parse_args()
    replay(args.post_id, args.workspace)
