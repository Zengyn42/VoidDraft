#!/usr/bin/env python3
"""
Content Retriever Pipeline — standalone CLI runner.

Usage:
  python3 run.py --config configs/reddit_example.yaml
  python3 run.py --config configs/rednote_example.yaml --analyze ollama
  python3 run.py --config configs/reddit_example.yaml --max-posts 10
  python3 run.py --config configs/reddit_example.yaml --report-only

This module can also be invoked as a LangGraph subgraph via SubgraphRefNode
by passing a pre-built ContentRetrieverState to the graph.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

# Ensure the pipeline root is on sys.path when run directly
_HERE = Path(__file__).resolve().parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

from validators import PipelineConfig, fetch, download, analyze, report


def _run_pipeline(cfg: PipelineConfig) -> None:
    """Execute the full pipeline by calling each DETERMINISTIC node function in order."""
    state: dict = {
        "config": json.dumps(cfg.to_dict()),
        "posts": "[]",
        "downloads": "[]",
        "analysis": "[]",
        "report": "",
        "errors": "[]",
    }

    print(f"[run] Starting content_retriever pipeline")
    print(f"[run] Source: {cfg.source_type}, max_posts={cfg.max_posts}")
    print(f"[run] Download dir: {cfg.download_dir}")
    print(f"[run] Analyzer: {cfg.analyzer}")
    print()

    # Node 1: fetch
    state.update(fetch(state))
    posts = json.loads(state.get("posts", "[]"))
    print(f"[run] fetch -> {len(posts)} post(s)")

    if not posts:
        print("[run] No new posts to process. Exiting.")
        _print_errors(state)
        return

    # Node 2: download
    state.update(download(state))
    downloads = json.loads(state.get("downloads", "[]"))
    print(f"[run] download -> {len(downloads)} post(s) downloaded")

    # Node 3: analyze (skipped if analyzer == "none")
    state.update(analyze(state))
    analysis = json.loads(state.get("analysis", "[]"))
    if analysis:
        print(f"[run] analyze -> {len(analysis)} post(s) analyzed")
    else:
        print(f"[run] analyze -> skipped")

    # Node 4: report
    state.update(report(state))
    report_text = state.get("report", "")
    if report_text:
        print(f"[run] report -> generated ({len(report_text)} chars)")

    _print_errors(state)
    print("\n[run] Pipeline complete.")


def _print_errors(state: dict) -> None:
    errors = json.loads(state.get("errors", "[]"))
    if errors:
        print(f"\n[run] {len(errors)} error(s):")
        for e in errors:
            print(f"  - {e}")


def _report_only(cfg: PipelineConfig) -> None:
    """Generate a report from already-downloaded files without fetching or downloading."""
    state: dict = {
        "config": json.dumps(cfg.to_dict()),
        "posts": "[]",
        "downloads": "[]",
        "analysis": "[]",
        "report": "",
        "errors": "[]",
    }

    # Build downloads_data from existing directory structure
    download_dir = Path(cfg.download_dir)
    if not download_dir.exists():
        print(f"[run] Download dir not found: {download_dir}")
        return

    _IMAGE_SUFFIXES = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp", ".tiff"}
    _VIDEO_SUFFIXES = {".mp4", ".mkv", ".avi", ".mov", ".webm", ".flv", ".m4v"}
    skip_names = {"text.txt", "analysis.json", "pipeline_state.json"}

    downloads_data = []
    analysis_data = []

    for post_dir in sorted(download_dir.iterdir()):
        if not post_dir.is_dir() or post_dir.name.startswith("."):
            continue
        parts = post_dir.name.split("_", 1)
        post_id = parts[0]
        post_title = parts[1].replace("_", " ") if len(parts) > 1 else post_id

        files = [
            str(f)
            for f in post_dir.iterdir()
            if f.is_file() and f.name not in skip_names
            and f.suffix.lower() in _IMAGE_SUFFIXES | _VIDEO_SUFFIXES
        ]

        downloads_data.append({
            "post_id": post_id,
            "post_title": post_title,
            "post_dir": str(post_dir),
            "files": files,
        })

        # Load existing analysis.json if present
        analysis_file = post_dir / "analysis.json"
        if analysis_file.exists():
            try:
                analysis_data.append(json.loads(analysis_file.read_text(encoding="utf-8")))
            except Exception:
                pass

    state["downloads"] = json.dumps(downloads_data, ensure_ascii=False)
    state["analysis"] = json.dumps(analysis_data, ensure_ascii=False)

    state.update(report(state))
    report_text = state.get("report", "")
    print(report_text)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Content Retriever Pipeline — scrape, download, and optionally analyze media.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument(
        "--config",
        required=True,
        metavar="PATH",
        help="Path to a YAML config file (e.g. configs/reddit_example.yaml)",
    )
    parser.add_argument(
        "--max-posts",
        type=int,
        default=None,
        metavar="N",
        help="Override the max_posts value from the config file",
    )
    parser.add_argument(
        "--analyze",
        metavar="BACKEND",
        default=None,
        help="Override analyzer backend: ollama / claude / gemini / none",
    )
    parser.add_argument(
        "--report-only",
        action="store_true",
        help="Skip fetching/downloading; only generate a report from already-downloaded files",
    )
    args = parser.parse_args()

    # Resolve config path relative to the script's directory if not absolute
    config_path = Path(args.config)
    if not config_path.is_absolute():
        config_path = _HERE / config_path

    cfg = PipelineConfig.from_yaml(str(config_path))

    # Apply CLI overrides
    if args.max_posts is not None:
        cfg.max_posts = args.max_posts
    if args.analyze is not None:
        cfg.analyzer = args.analyze

    if args.report_only:
        _report_only(cfg)
    else:
        _run_pipeline(cfg)


if __name__ == "__main__":
    main()
