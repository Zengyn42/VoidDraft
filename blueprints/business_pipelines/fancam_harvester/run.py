"""
FancamHarvester — cron entry point.

Daily cron (UTC 06:00) classifies each fetched Reddit post as NEW or SEEN,
then runs the appropriate pipeline branch:

  NEW  → full pipeline: download → split_merged → detect_slowmo →
          align → extract_hd → select_best_clip → parse_post_metadata →
          store_results
  SEEN → update_seen_post: upvote tracking + Pixeldrain album diff

CLI usage:
    python run.py [--subreddit r/kpopfap] [--max-posts 250] [--workspace ...]
    python run.py --compile --group IVE --idol Wonyoung --song HEYA
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
import time
from dataclasses import asdict
from pathlib import Path

# ---------------------------------------------------------------------------
# sys.path setup
# ---------------------------------------------------------------------------
_PIPELINE_DIR       = Path(__file__).parent
_VOIDDRAFT          = _PIPELINE_DIR.parent.parent
_LIB                = _VOIDDRAFT / "lib"
_CONTENT_RETRIEVER  = _VOIDDRAFT / "pipelines" / "content_retriever"

for p in [str(_PIPELINE_DIR), str(_LIB), str(_VOIDDRAFT)]:
    if p not in sys.path:
        sys.path.insert(0, p)
if str(_CONTENT_RETRIEVER) not in sys.path:
    sys.path.append(str(_CONTENT_RETRIEVER))

from config import FancamConfig  # noqa: E402
import validators                # noqa: E402

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)

# Default workspace
_DEFAULT_WORKSPACE = str(
    Path.home() / "Foundation" / "EdenGateway" / "RedditPulsify"
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _db_path(cfg: FancamConfig) -> str:
    return str(Path(cfg.workspace) / "fancam.db")


def _state_skeleton(cfg: FancamConfig) -> dict:
    return {
        "config":      json.dumps(asdict(cfg)),
        "posts":       "[]",
        "downloads":   "[]",
        "slowmo_info": "{}",
        "alignments":  "[]",
        "extracts":    "[]",
        "final_clips": "[]",
        "post_metas":  "[]",
        "analyses":    "[]",
        "identities":  "[]",
        "stored":      "[]",
        "errors":      "[]",
    }


# ---------------------------------------------------------------------------
# Full pipeline (NEW posts)
# ---------------------------------------------------------------------------

def run_new_post_pipeline(cfg: FancamConfig, post_data: dict) -> dict:
    """
    Run the complete clip processing pipeline for a single NEW post.
    Returns the final state dict.
    """
    state = _state_skeleton(cfg)
    state["posts"] = json.dumps([post_data])

    logger.info("[NEW] post=%s  title=%r", post_data["post_id"], post_data.get("title", "")[:60])

    logger.info("  → download")
    state.update(validators.download(state))

    logger.info("  → split_merged")
    state.update(validators.split_merged(state))

    logger.info("  → detect_slowmo_for_clips")
    state.update(validators.detect_slowmo_for_clips(state))

    logger.info("  → align (parallel competition)")
    state.update(validators.align(state))

    logger.info("  → extract_hd")
    state.update(validators.extract_hd(state))

    logger.info("  → select_best_clip")
    state.update(validators.select_best_clip(state))

    logger.info("  → parse_post_metadata")
    state.update(validators.parse_post_metadata(state))

    logger.info("  → store_results")
    state.update(validators.store_results(state, db_path=_db_path(cfg)))

    errs = json.loads(state.get("errors", "[]"))
    if errs:
        logger.warning("  post=%s: %d error(s)", post_data["post_id"], len(errs))
        for e in errs[-5:]:
            logger.warning("    %s", e)

    return state


# ---------------------------------------------------------------------------
# SEEN post update
# ---------------------------------------------------------------------------

def update_seen_post(
    cfg: FancamConfig,
    post_data: dict,
    *,
    album_api_calls_counter: list[int],
) -> list[str]:
    """
    Update an already-processed post:
      ① Upvote tracking (48h settled rule)
      ② Pixeldrain album diff — download and process new files

    Returns list of error strings encountered.
    """
    from storage.database import (
        get_connection, log_upvote, settle_post,
        get_clip_filenames_for_post,
    )

    post_id     = post_data["post_id"]
    score       = int(post_data.get("score", 0))
    created_utc = float(post_data.get("created_utc", 0))
    now         = time.time()
    post_age_h  = (now - created_utc) / 3600.0
    db          = _db_path(cfg)
    errors: list[str] = []

    with get_connection(db) as conn:
        row = conn.execute(
            "SELECT settled FROM posts WHERE post_id=?", (post_id,)
        ).fetchone()

        if row is None:
            # Race condition — shouldn't happen, but handle gracefully
            errors.append(f"update_seen: {post_id} not in DB")
            return errors

        settled = bool(row["settled"])

        # ── ① Upvote tracking ─────────────────────────────────────────
        if not settled:
            if post_age_h < validators._SETTLED_HOURS:
                log_upvote(conn, post_id=post_id, score=score,
                           post_age_hours=post_age_h)
                logger.info(
                    "[SEEN] upvote_log: post=%s  score=%d  age=%.1fh",
                    post_id, score, post_age_h,
                )
            else:
                settle_post(conn, post_id, score)
                logger.info(
                    "[SEEN] settled: post=%s  final_score=%d", post_id, score,
                )

        # ── ② Album diff ──────────────────────────────────────────────
        known_filenames = get_clip_filenames_for_post(conn, post_id)

    pd_urls = post_data.get("pixeldrain_urls", [])
    if not pd_urls:
        return errors

    try:
        from downloaders.pixeldrain import PixeldrainDownloader
        import re as _re
        pd = PixeldrainDownloader(api_key=cfg.pixeldrain_api_key or None)
    except ImportError as e:
        errors.append(f"album_diff import: {e}")
        return errors

    for pd_url in pd_urls:
        list_match = _re.search(r"pixeldrain\.com/l/([A-Za-z0-9_-]+)", pd_url)
        if not list_match:
            continue

        list_id = list_match.group(1)
        try:
            album_files = pd.list_album(list_id)
            album_api_calls_counter[0] += 1
        except Exception as e:
            errors.append(f"album_diff list_album {list_id}: {e}")
            continue

        new_files = [
            f for f in album_files
            if f.get("name") and f["name"] not in known_filenames
        ]

        if not new_files:
            logger.debug("[SEEN] album diff: no new files for post=%s", post_id)
            continue

        logger.info(
            "[SEEN] album diff: post=%s  %d new file(s): %s",
            post_id, len(new_files),
            [f["name"] for f in new_files],
        )

        # Re-run clip pipeline for new files only
        # We inject a synthetic post_data with only the new PD URL
        mini_post = dict(post_data)
        mini_state = _state_skeleton(cfg)
        mini_state["posts"] = json.dumps([mini_post])

        try:
            mini_state.update(validators.download(mini_state))
            mini_state.update(validators.split_merged(mini_state))
            mini_state.update(validators.detect_slowmo_for_clips(mini_state))
            mini_state.update(validators.align(mini_state))
            mini_state.update(validators.extract_hd(mini_state))
            mini_state.update(validators.select_best_clip(mini_state))
            mini_state.update(validators.parse_post_metadata(mini_state))
            mini_state.update(validators.store_results(mini_state, db_path=db))
        except Exception as e:
            errors.append(f"album_diff clip_pipeline {post_id}: {e}")

        mini_errs = json.loads(mini_state.get("errors", "[]"))
        errors.extend(mini_errs)

    return errors


# ---------------------------------------------------------------------------
# Main cron runner
# ---------------------------------------------------------------------------

def run_cron(cfg: FancamConfig) -> None:
    """
    Main entry point for the daily cron job.

    1. Fetch posts from Reddit
    2. Classify each as NEW or SEEN by querying SQLite
    3. NEW  → full pipeline
       SEEN → upvote update + album diff
    4. Write crawl_log summary
    """
    from storage.database import init_db, get_connection, log_crawl, post_exists

    db = _db_path(cfg)
    init_db(db)

    run_start = time.time()

    # ── Step 1: fetch ─────────────────────────────────────────────────────
    state = _state_skeleton(cfg)
    logger.info("=== Cron start: fetching r/%s ===", cfg.subreddit)
    state.update(validators.fetch(state))

    posts_raw: list[dict] = json.loads(state["posts"])
    logger.info("Fetched %d posts", len(posts_raw))

    if not posts_raw:
        logger.warning("No posts fetched — exiting")
        return

    # ── Step 2: classify NEW vs SEEN ──────────────────────────────────────
    new_posts:  list[dict] = []
    seen_posts: list[dict] = []

    with get_connection(db) as conn:
        for p in posts_raw:
            if post_exists(conn, p["post_id"]):
                seen_posts.append(p)
            else:
                new_posts.append(p)

    logger.info("Classification: %d new, %d seen", len(new_posts), len(seen_posts))

    all_errors: list[str] = []
    total_clips_new    = 0
    album_api_calls    = [0]   # mutable counter passed by reference

    # ── Step 3a: NEW posts → full pipeline ────────────────────────────────
    for post_data in new_posts:
        try:
            result_state = run_new_post_pipeline(cfg, post_data)
            db_stats = json.loads(result_state.get("_db_stats", "{}"))
            total_clips_new += db_stats.get("clips_new", 0)
            all_errors.extend(json.loads(result_state.get("errors", "[]")))
        except Exception as e:
            all_errors.append(f"new_post_pipeline {post_data['post_id']}: {e}")
            logger.error("Pipeline failed for post %s: %s", post_data["post_id"], e)

    # ── Step 3b: SEEN posts → upvote + album diff ─────────────────────────
    for post_data in seen_posts:
        try:
            errs = update_seen_post(
                cfg, post_data,
                album_api_calls_counter=album_api_calls,
            )
            all_errors.extend(errs)
        except Exception as e:
            all_errors.append(f"update_seen {post_data['post_id']}: {e}")
            logger.error("update_seen failed for %s: %s", post_data["post_id"], e)

    # ── Step 4: crawl_log ─────────────────────────────────────────────────
    with get_connection(db) as conn:
        log_crawl(
            conn,
            posts_seen=len(posts_raw),
            posts_new=len(new_posts),
            posts_updated=len(seen_posts),
            clips_new=total_clips_new,
            album_api_calls=album_api_calls[0],
            errors=all_errors if all_errors else None,
        )

    elapsed = time.time() - run_start
    logger.info(
        "=== Cron done in %.1fs: new=%d seen=%d clips=%d api_calls=%d errors=%d ===",
        elapsed, len(new_posts), len(seen_posts),
        total_clips_new, album_api_calls[0], len(all_errors),
    )

    if all_errors:
        logger.warning("First 10 errors:")
        for e in all_errors[:10]:
            logger.warning("  %s", e)


# ---------------------------------------------------------------------------
# Compile mode
# ---------------------------------------------------------------------------

def run_compile(args: argparse.Namespace, cfg: FancamConfig) -> None:
    from compile.highlight_compiler import HighlightQuery, compile_highlight

    query = HighlightQuery(
        group=args.group,
        idol=args.idol,
        song=args.song,
        date=args.date,
        max_duration=float(args.max_duration),
        output_name=(
            f"{args.group or 'all'}_{args.idol or 'all'}_{args.song or 'all'}"
        ),
    )
    out = compile_highlight(
        library_dir=cfg.library_dir,
        output_dir=cfg.highlights_dir,
        query=query,
    )
    if out:
        print(f"Highlight compiled: {out}")
    else:
        print("Compilation failed — check logs")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(description="FancamHarvester")
    parser.add_argument("--subreddit",       default="kpopfap")
    parser.add_argument("--max-posts",       type=int, default=250)
    parser.add_argument("--workspace",       default=_DEFAULT_WORKSPACE)
    parser.add_argument("--sort",            default="new")
    parser.add_argument("--pixeldrain-key",  default="")
    parser.add_argument("--youtube-api-key", default="")
    parser.add_argument("--jd-email",        default="")
    parser.add_argument("--jd-password",     default="")

    # Compile mode
    parser.add_argument("--compile",      action="store_true")
    parser.add_argument("--group",        default=None)
    parser.add_argument("--idol",         default=None)
    parser.add_argument("--song",         default=None)
    parser.add_argument("--date",         default=None)
    parser.add_argument("--max-duration", default=180.0)

    args = parser.parse_args()

    cfg = FancamConfig(
        subreddit=args.subreddit,
        max_posts=args.max_posts,
        workspace=args.workspace,
        sort=args.sort,
        pixeldrain_api_key=args.pixeldrain_key,
        youtube_api_key=args.youtube_api_key,
        jd_email=args.jd_email,
        jd_password=args.jd_password,
        use_jdownloader=bool(args.jd_email),
    )

    if args.compile:
        run_compile(args, cfg)
    else:
        run_cron(cfg)


if __name__ == "__main__":
    main()
