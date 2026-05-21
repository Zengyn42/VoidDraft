#!/usr/bin/env python3
"""
rednote_to_prismrag.py — Import rednote summary.json files into PrismRag.

Strategy
--------
Each key_point in a summary becomes ONE KNOW node (atomic knowledge unit).
The summary text is embedded as context in the node body.

Output per video post:
  - {Download/video.know.md}   — KNOW markdown alongside the summary file
  - PrismRag nimbus graph updated with new nodes

Node structure per key_point:
  - id          : KNOW-XXXXXX (allocated from PrismRag registry)
  - label       : key_point text
  - content     : key_point + "\n\n## 来源摘要\n" + summary
  - kind        : "note"
  - namespace   : "nimbus"
  - ontology_type: "concept"
  - frontmatter : {topic, target_audience, actionable, post_title, post_id,
                   source_collection: "rednote"}

Usage
-----
  python3 rednote_to_prismrag.py
  python3 rednote_to_prismrag.py --dry-run          # 不写入 graph，只生成 .know.md
  python3 rednote_to_prismrag.py --limit 5          # 只处理前 5 个帖子
  python3 rednote_to_prismrag.py --skip-graph       # 只生成 .know.md，不更新 graph
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

# ── Paths ──────────────────────────────────────────────────────────────────────
_DOWNLOADS_DIR  = Path("/home/kingy/Foundation/EdenGateway/rednote_downloads")
_PRISMRAG_ROOT  = Path("/home/kingy/Foundation/PrismRag")
_PRISMRAG_DATA  = _PRISMRAG_ROOT / "data"
_REGISTRY_PATH  = _PRISMRAG_DATA / "registry.json"
_GRAPH_PATH     = _PRISMRAG_DATA / "nimbus" / "graph.json"

# Add PrismRag to path for Registry and KnowledgeGraph imports
sys.path.insert(0, str(_PRISMRAG_ROOT))


# ── Helpers ────────────────────────────────────────────────────────────────────

def _sha256(text: str) -> str:
    return "sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest()


def _token_estimate(text: str) -> int:
    return max(0, len(text) // 4)


def _load_summaries(limit: int | None = None) -> list[dict]:
    """Collect all .summary.json files from rednote_downloads."""
    files = sorted(_DOWNLOADS_DIR.rglob("*.summary.json"))
    if limit:
        files = files[:limit]
    summaries = []
    for f in files:
        try:
            data = json.loads(f.read_text(encoding="utf-8"))
            data["_summary_file"] = str(f)
            summaries.append(data)
        except Exception as e:
            print(f"  [warn] Failed to read {f.name}: {e}")
    return summaries


def _make_know_content(key_point: str, summary_data: dict) -> str:
    """Build the KNOW node body text."""
    post_title = summary_data.get("post_title", "")
    summary    = summary_data.get("summary", "")
    topic      = summary_data.get("topic", "")
    audience   = summary_data.get("target_audience", "")
    actionable = summary_data.get("actionable", False)

    lines = [
        key_point,
        "",
        "## 视频摘要",
        "",
        summary,
        "",
        "## 元信息",
        "",
        f"- **来源视频**：{post_title}",
        f"- **主题分类**：{topic}",
        f"- **目标受众**：{audience}",
        f"- **可操作性**：{'是' if actionable else '否'}",
    ]
    return "\n".join(lines)


def _make_know_md(know_id: str, key_point: str, content: str, summary_data: dict) -> str:
    """Generate a PrismRag-compatible KNOW markdown file."""
    post_id    = summary_data.get("post_id", "")
    post_title = summary_data.get("post_title", "")
    topic      = summary_data.get("topic", "")
    audience   = summary_data.get("target_audience", "")
    actionable = summary_data.get("actionable", False)
    now        = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    fm_lines = [
        "---",
        f"id: {know_id}",
        f'title: "{key_point[:80].replace(chr(34), chr(39))}"',
        "kind: note",
        "ontology_type: concept",
        "namespace: nimbus",
        f"source_collection: rednote",
        f"post_id: {post_id}",
        f'post_title: "{post_title[:80].replace(chr(34), chr(39))}"',
        f"topic: {topic}",
        f'target_audience: "{audience[:60].replace(chr(34), chr(39))}"',
        f"actionable: {str(actionable).lower()}",
        f"created: {now}",
        "maturity: seed",
        "confidence: medium",
        "---",
        "",
    ]
    return "\n".join(fm_lines) + content


# ── Main logic ─────────────────────────────────────────────────────────────────

def run(dry_run: bool = False, limit: int | None = None, skip_graph: bool = False) -> None:
    from prism_rag.store.registry import Registry
    from prism_rag.store.graph import KnowledgeGraph, Node

    print(f"[rednote→prismrag] Loading summaries from {_DOWNLOADS_DIR}")
    summaries = _load_summaries(limit)
    print(f"[rednote→prismrag] Found {len(summaries)} summary files")

    # Count total key_points to allocate IDs in one batch
    all_pairs: list[tuple[dict, str]] = []  # (summary_data, key_point)
    for s in summaries:
        kps = s.get("key_points", [])
        if not kps:
            # Fall back to summary text as single node if no key_points
            kps = [s.get("summary", "").strip()]
        for kp in kps:
            kp = kp.strip()
            if kp:
                all_pairs.append((s, kp))

    print(f"[rednote→prismrag] Total key_points (KNOW nodes to create): {len(all_pairs)}")

    # Allocate KNOW IDs from PrismRag registry
    if not dry_run:
        reg = Registry(_REGISTRY_PATH)
        know_ids = reg.batch_alloc(len(all_pairs))
    else:
        # Fake IDs for dry run
        know_ids = [f"KNOW-DRY{i:04d}" for i in range(len(all_pairs))]

    print(f"[rednote→prismrag] Allocated IDs: {know_ids[0]} … {know_ids[-1]}")

    # Load graph (unless skip_graph or dry_run)
    if not dry_run and not skip_graph:
        print(f"[rednote→prismrag] Loading graph: {_GRAPH_PATH}")
        kg = KnowledgeGraph.load(_GRAPH_PATH)
        existing_ids = {attrs["id"] for _, attrs in kg.g.nodes(data=True) if "id" in attrs}
    else:
        kg = None
        existing_ids = set()

    # Process each key_point
    nodes_added = 0
    md_written  = 0
    skipped     = 0

    for (summary_data, key_point), know_id in zip(all_pairs, know_ids):
        # Skip if already in graph
        if know_id in existing_ids:
            skipped += 1
            continue

        content = _make_know_content(key_point, summary_data)
        md_text = _make_know_md(know_id, key_point, content, summary_data)

        # Write .know.md alongside summary file
        summary_file = Path(summary_data.get("_summary_file", ""))
        if summary_file.exists():
            # Derive a stable filename from know_id
            know_md_path = summary_file.parent / f"{know_id}.know.md"
            if not dry_run:
                know_md_path.write_text(md_text, encoding="utf-8")
            md_written += 1

        # Add node to graph
        if kg is not None:
            node = Node(
                id=know_id,
                label=key_point[:120],
                kind="note",
                source_file=str(summary_file),
                content=content,
                content_hash=_sha256(content),
                tokens=_token_estimate(content),
                namespace="nimbus",
                knowledge_id=know_id,
                ontology_type="concept",   # type: ignore[arg-type]
                maturity="seed",           # type: ignore[arg-type]
                confidence="medium",       # type: ignore[arg-type]
                actionability="reference", # type: ignore[arg-type]
                frontmatter={
                    "id": know_id,
                    "title": key_point[:80],
                    "topic": summary_data.get("topic", ""),
                    "post_id": summary_data.get("post_id", ""),
                    "post_title": summary_data.get("post_title", ""),
                    "target_audience": summary_data.get("target_audience", ""),
                    "actionable": summary_data.get("actionable", False),
                    "source_collection": "rednote",
                    "created": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
                },
            )
            kg.add_node(node)
            nodes_added += 1

    # Save graph
    if kg is not None and nodes_added > 0:
        print(f"[rednote→prismrag] Saving graph with {nodes_added} new nodes…")
        kg.save(_GRAPH_PATH)
        print(f"[rednote→prismrag] Graph saved: {_GRAPH_PATH}")

    print(f"\n[rednote→prismrag] ── Done ──────────────────────")
    print(f"  KNOW nodes added : {nodes_added}")
    print(f"  .know.md written : {md_written}")
    print(f"  Skipped (exist)  : {skipped}")
    if dry_run:
        print("  [dry-run] No files/graph written.")


# ── CLI ────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Import rednote summaries → PrismRag KNOW nodes")
    parser.add_argument("--dry-run",    action="store_true", help="Preview only, no writes")
    parser.add_argument("--limit",      type=int, default=None, metavar="N", help="Process only first N posts")
    parser.add_argument("--skip-graph", action="store_true", help="Write .know.md only, skip graph update")
    args = parser.parse_args()

    run(dry_run=args.dry_run, limit=args.limit, skip_graph=args.skip_graph)
