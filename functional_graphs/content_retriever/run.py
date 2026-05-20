#!/usr/bin/env python3
"""
Content Retriever Pipeline — standalone CLI runner (LangGraph edition).

Usage:
    python3 -m functional_graphs.content_retriever.run --config configs/rednote_example.yaml
    python3 -m functional_graphs.content_retriever.run --config configs/rednote_example.yaml --max-posts 5
    python3 -m functional_graphs.content_retriever.run --config configs/rednote_example.yaml --summarize-backend ollama --summarize-model qwen2.5:7b

Graph is defined in graph.py and follows the ZenithLoom awaken pattern:
    entry node : fetch
    exit  node : report
    (declared in entity.json for SubgraphRefNode compatibility)
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

# Ensure VoidDraft root is on sys.path when invoked directly
_HERE = Path(__file__).resolve().parent
_VOIDRAFT_ROOT = _HERE.parent.parent          # …/VoidDraft
if str(_VOIDRAFT_ROOT) not in sys.path:
    sys.path.insert(0, str(_VOIDRAFT_ROOT))

from functional_graphs.content_retriever.config import PipelineConfig
from functional_graphs.content_retriever.graph import build_graph


# --------------------------------------------------------------------------- #
# Runner
# --------------------------------------------------------------------------- #

def _run_pipeline(cfg: PipelineConfig, *, thread_id: str = "default") -> None:
    """Compile the LangGraph and invoke it with the given config."""

    # Initial state — only config is pre-populated; all list fields start empty
    # (LangGraph add reducers accumulate across node invocations)
    initial_state = {
        "config": json.dumps(cfg.to_dict()),
        "posts": [],
        "downloads": [],
        "transcripts": [],
        "summaries": [],
        "errors": [],
        "report": "",
    }

    print("[run] Compiling content_retriever graph…")
    graph = build_graph()          # no checkpointer for standalone run

    print(f"[run] Source: {cfg.source_type}, max_posts={cfg.max_posts}")
    print(f"[run] Download dir: {cfg.download_dir}")
    print(f"[run] Summarize backend: {cfg.summarize_backend or 'none'}")
    print()

    final_state = graph.invoke(initial_state)

    # ---------------------------------------------------------------- summary
    posts      = final_state.get("posts", [])
    downloads  = final_state.get("downloads", [])
    transcripts = final_state.get("transcripts", [])
    summaries  = final_state.get("summaries", [])
    errors     = final_state.get("errors", [])
    report     = final_state.get("report", "")

    print(f"\n[run] ── Pipeline complete ──────────────────────────")
    print(f"[run]   posts fetched  : {len(posts)}")
    print(f"[run]   posts downloaded: {len(downloads)}")
    print(f"[run]   transcripts    : {len(transcripts)}")
    print(f"[run]   summaries      : {len(summaries)}")
    print(f"[run]   report         : {len(report)} chars")

    if errors:
        print(f"\n[run] {len(errors)} error(s):")
        for e in errors:
            print(f"  - {e}")

    print("[run] Done.")


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Content Retriever Pipeline (LangGraph standalone runner)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument(
        "--config", required=True, metavar="PATH",
        help="Path to a YAML config file (e.g. configs/rednote_example.yaml)",
    )
    parser.add_argument(
        "--max-posts", type=int, default=None, metavar="N",
        help="Override max_posts from config",
    )
    parser.add_argument(
        "--summarize-backend", metavar="BACKEND", default=None,
        help="Override summarize_backend: ollama | claude | none",
    )
    parser.add_argument(
        "--summarize-model", metavar="MODEL", default=None,
        help="Override summarize_model (e.g. qwen2.5:7b for ollama)",
    )
    parser.add_argument(
        "--thread-id", default="default",
        help="LangGraph thread ID for checkpointing (future use)",
    )
    args = parser.parse_args()

    config_path = Path(args.config)
    if not config_path.is_absolute():
        config_path = _HERE / config_path

    cfg = PipelineConfig.from_yaml(str(config_path))

    # Apply CLI overrides
    if args.max_posts is not None:
        cfg.max_posts = args.max_posts
    if args.summarize_backend is not None:
        cfg.summarize_backend = args.summarize_backend
    if args.summarize_model is not None:
        cfg.summarize_model = args.summarize_model

    _run_pipeline(cfg, thread_id=args.thread_id)


if __name__ == "__main__":
    main()
