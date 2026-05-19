"""
FancamHarvester standalone CLI runner.

Usage:
    python run.py --subreddit kpopfancams --max-posts 10 --workspace /tmp/fancam

    # Compile highlights after harvesting:
    python run.py --compile --group twice --idol tzuyu --song tt --date 20261015
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from dataclasses import asdict
from pathlib import Path

# Resolve sys.path so pipeline imports work
_PIPELINE_DIR = Path(__file__).parent
_VOIDDRAFT = _PIPELINE_DIR.parent.parent
_LIB = _VOIDDRAFT / "lib"
_CONTENT_RETRIEVER = _VOIDDRAFT / "pipelines" / "content_retriever"
for p in [str(_LIB), str(_VOIDDRAFT), str(_PIPELINE_DIR), str(_CONTENT_RETRIEVER)]:
    if p not in sys.path:
        sys.path.insert(0, p)

from config import FancamConfig
from state import FancamState
import validators

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)


def run_pipeline(cfg: FancamConfig) -> dict:
    """Run the full harvest pipeline (minus LLM identify node)."""
    state: dict = {
        "config": json.dumps(asdict(cfg)),
        "posts": "[]",
        "downloads": "[]",
        "alignments": "[]",
        "analyses": "[]",
        "identities": "[]",
        "stored": "[]",
        "errors": "[]",
    }

    logger.info("=== Step 1: fetch ===")
    state.update(validators.fetch(state))

    posts = json.loads(state["posts"])
    logger.info(f"Fetched {len(posts)} posts")
    if not posts:
        logger.warning("No posts fetched — exiting")
        return state

    logger.info("=== Step 2: download ===")
    state.update(validators.download(state))

    downloads = json.loads(state["downloads"])
    logger.info(f"Downloaded {len(downloads)} files")

    logger.info("=== Step 3: align ===")
    state.update(validators.align(state))

    logger.info("=== Step 4: analyze ===")
    state.update(validators.analyze(state))

    logger.info(
        "=== Step 5: identify (LLM) — skipped in CLI mode ===\n"
        "    Run via ZenithLoom graph to get LLM identification."
    )

    errors = json.loads(state.get("errors", "[]"))
    if errors:
        logger.warning(f"Pipeline completed with {len(errors)} error(s):")
        for e in errors[:10]:
            logger.warning(f"  {e}")

    return state


def run_compile(args: argparse.Namespace, cfg: FancamConfig) -> None:
    """Run highlight compilation."""
    from compile import HighlightQuery, compile_highlight

    query = HighlightQuery(
        group=args.group,
        idol=args.idol,
        song=args.song,
        date=args.date,
        max_duration=float(args.max_duration),
        output_name=f"{args.group or 'all'}_{args.idol or 'all'}_{args.song or 'all'}",
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


def main() -> None:
    parser = argparse.ArgumentParser(description="FancamHarvester pipeline")
    parser.add_argument("--subreddit", default="kpopfancams")
    parser.add_argument("--max-posts", type=int, default=20)
    parser.add_argument("--workspace", default="/tmp/fancam_harvester")
    parser.add_argument("--sort", default="new")
    parser.add_argument("--jd-email", default="")
    parser.add_argument("--jd-password", default="")
    parser.add_argument("--pixeldrain-key", default="")
    parser.add_argument("--youtube-api-key", default="")
    parser.add_argument("--align-strategy", default="auto",
                        choices=["auto", "audio", "visual"])

    # Compile mode
    parser.add_argument("--compile", action="store_true",
                        help="Compile highlight reels instead of harvesting")
    parser.add_argument("--group", default=None)
    parser.add_argument("--idol", default=None)
    parser.add_argument("--song", default=None)
    parser.add_argument("--date", default=None)
    parser.add_argument("--max-duration", default=180.0)

    args = parser.parse_args()

    cfg = FancamConfig(
        subreddit=args.subreddit,
        max_posts=args.max_posts,
        workspace=args.workspace,
        sort=args.sort,
        jd_email=args.jd_email,
        jd_password=args.jd_password,
        use_jdownloader=bool(args.jd_email),
        pixeldrain_api_key=args.pixeldrain_key,
        youtube_api_key=args.youtube_api_key,
        align_strategy=args.align_strategy,
    )

    if args.compile:
        run_compile(args, cfg)
    else:
        final_state = run_pipeline(cfg)
        print("\n--- Summary ---")
        print(f"Posts:     {len(json.loads(final_state['posts']))}")
        print(f"Downloads: {len(json.loads(final_state['downloads']))}")
        print(f"Aligned:   {len(json.loads(final_state['alignments']))}")
        print(f"Analyses:  {len(json.loads(final_state['analyses']))}")
        print(f"Errors:    {len(json.loads(final_state['errors']))}")


if __name__ == "__main__":
    main()
