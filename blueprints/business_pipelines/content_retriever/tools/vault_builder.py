#!/usr/bin/env python3
"""
vault_builder.py — Generic summary.json → PrismRag vault builder.

Driven by a YAML config file. Supports any video source (rednote, reddit,
youtube, etc.) as long as the summaries follow the standard schema:

  {
    "post_id":        str,
    "post_title":     str,
    "summary":        str,
    "key_points":     [str, ...],
    "topic":          str,
    "target_audience": str,
    "actionable":     bool
  }

Pipeline: convert → ingest → patch_urls (all automatic)

Usage
-----
  python3 vault_builder.py --config configs/rednote_vault.yaml
  python3 vault_builder.py --config configs/rednote_vault.yaml --convert-only
  python3 vault_builder.py --config configs/rednote_vault.yaml --ingest-only
"""
from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

_PRISMRAG_ROOT = Path("/home/kingy/Foundation/PrismRag")
_VENV_PYTHON   = _PRISMRAG_ROOT / ".venv" / "bin" / "python3"


# ── Config ─────────────────────────────────────────────────────────────────────

class VaultConfig:
    """Parsed vault builder config."""

    def __init__(self, data: dict) -> None:
        # Source: where to find summary.json files
        self.summaries_glob: str  = data["summaries_glob"]       # e.g. "/path/**/*.summary.json"

        # Output vault
        self.vault_root: Path     = Path(data["vault_root"])      # e.g. EdenGateway/rednote
        self.namespace: str       = data["namespace"]             # e.g. "rednote"

        # URL template — use {post_id} as placeholder
        # e.g. "https://www.xiaohongshu.com/explore/{post_id}"
        # leave blank to skip URL patching
        self.url_template: str    = data.get("url_template", "")

        # Optional topic normalization map: {raw_topic: canonical_topic}
        self.topic_map: dict[str, str] = data.get("topic_map", {})

        # Display name for graph HTML title
        self.display_name: str    = data.get("display_name", self.namespace)

    @classmethod
    def from_yaml(cls, path: str) -> "VaultConfig":
        import yaml
        raw = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
        return cls(raw)

    @property
    def sources_dir(self) -> Path:
        return self.vault_root / "sources"

    @property
    def data_dir(self) -> Path:
        return self.vault_root / "data" / self.namespace

    def normalize_topic(self, raw: str) -> str:
        return self.topic_map.get(raw.strip(), raw.strip())

    def build_url(self, post_id: str) -> str:
        if not self.url_template:
            return ""
        return self.url_template.format(post_id=post_id)


# ── Helpers ────────────────────────────────────────────────────────────────────

def _slugify(text: str) -> str:
    return re.sub(r"[^\w\u4e00-\u9fff]", "_", text)[:60].strip("_")


def _make_markdown(data: dict, cfg: VaultConfig) -> str:
    post_id    = data.get("post_id", "")
    post_title = data.get("post_title", post_id)
    summary    = data.get("summary", "").strip()
    key_points = data.get("key_points", [])
    raw_topic  = data.get("topic", "").strip()
    topic      = cfg.normalize_topic(raw_topic)
    audience   = data.get("target_audience", "").strip()
    actionable = data.get("actionable", False)
    url        = cfg.build_url(post_id)

    tags = [topic] if topic else []
    tags_yaml = "[" + ", ".join(f'"{t}"' for t in tags) + "]"

    safe_title    = post_title.replace('"', "'")
    safe_audience = audience.replace('"', "'")[:80]

    lines = [
        "---",
        # knowledge_id → PrismRag uses frontmatter title as node label
        f'knowledge_id: "{post_id}"',
        f'title: "{safe_title}"',
        f"tags: {tags_yaml}",
        f'topic: "{topic}"',
        f'target_audience: "{safe_audience}"',
        f"actionable: {str(actionable).lower()}",
        f'source: "{cfg.namespace}"',
    ]
    if url:
        lines.append(f'url: "{url}"')
    lines += ["---", "", f"# {post_title}", "", "## 摘要", "", summary, ""]

    if key_points:
        lines += ["## 知识要点", ""]
        for kp in key_points:
            lines.append(f"- {kp.strip()}")
        lines.append("")

    if topic:
        lines += ["## 分类", "", f"- 主题：[[{topic}]]", f"- 受众：{audience}", ""]

    return "\n".join(lines)


# ── Pipeline steps ─────────────────────────────────────────────────────────────

def convert(cfg: VaultConfig, verbose: bool = True) -> int:
    """Convert all summary.json files → markdown vault documents."""
    import glob as _glob

    if cfg.sources_dir.exists():
        shutil.rmtree(cfg.sources_dir)
    cfg.sources_dir.mkdir(parents=True, exist_ok=True)

    files = sorted(Path(f) for f in _glob.glob(cfg.summaries_glob, recursive=True))
    written = 0

    for sf in files:
        try:
            data = json.loads(sf.read_text(encoding="utf-8"))
        except Exception as e:
            print(f"  [warn] {sf.name}: {e}")
            continue

        post_title = data.get("post_title", data.get("post_id", sf.stem))
        filename   = f"{_slugify(post_title)}.md"
        out_path   = cfg.sources_dir / filename

        # Handle duplicate titles
        counter = 1
        while out_path.exists():
            filename  = f"{_slugify(post_title)}_{counter}.md"
            out_path  = cfg.sources_dir / filename
            counter  += 1

        out_path.write_text(_make_markdown(data, cfg), encoding="utf-8")
        written += 1
        if verbose:
            print(f"  [convert] {filename}")

    print(f"\n[vault_builder] Converted {written} summaries → {cfg.sources_dir}")
    return written


def ingest(cfg: VaultConfig) -> None:
    """Run prism-rag ingest on the vault."""
    print(f"\n[vault_builder] Running prism-rag ingest…")
    print(f"  vault     : {cfg.vault_root}")
    print(f"  output    : {cfg.vault_root / 'data'}")
    print(f"  namespace : {cfg.namespace}")

    (cfg.vault_root / "data").mkdir(parents=True, exist_ok=True)

    cmd = [
        str(_VENV_PYTHON), "-m", "prism_rag.cli", "ingest",
        "--vault",     str(cfg.vault_root),
        "--output",    str(cfg.vault_root / "data"),
        "--namespace", cfg.namespace,
    ]
    result = subprocess.run(cmd, cwd=str(_PRISMRAG_ROOT))
    if result.returncode != 0:
        print(f"[vault_builder] ingest failed (exit {result.returncode})")
        sys.exit(result.returncode)


def patch_urls(cfg: VaultConfig) -> None:
    """Inject source URLs into graph node sig/metadata, regenerate HTML."""
    if not cfg.url_template:
        print("[vault_builder] No url_template configured — skipping URL patch.")
        return

    # post_id pattern: 24-char hex (rednote) or any non-empty id that's not a path
    _ID_RE = re.compile(r"^[0-9a-f]{16,}$|^[a-zA-Z0-9_-]{6,}$")

    graph_path = cfg.data_dir / "graph.json"
    html_path  = cfg.data_dir / "graph.html"

    if not graph_path.exists():
        print(f"[vault_builder] graph.json not found: {graph_path}")
        return

    sys.path.insert(0, str(_PRISMRAG_ROOT))
    from prism_rag.store.graph import KnowledgeGraph
    from prism_rag.report.visualize import generate_html

    kg = KnowledgeGraph.load(graph_path)
    patched = 0
    for node_id, attrs in kg.g.nodes(data=True):
        url = cfg.build_url(str(node_id))
        if url and _ID_RE.match(str(node_id)) and "sources/" not in str(node_id):
            meta = attrs.get("metadata") or {}
            meta["signature"] = url
            attrs["metadata"] = meta
            patched += 1

    print(f"[vault_builder] Patched {patched} nodes with source URLs")
    kg.save(graph_path)
    generate_html(kg, html_path, vault_name=cfg.display_name)
    print(f"[vault_builder] HTML regenerated: {html_path}")


# ── CLI ────────────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Generic summary.json → PrismRag vault builder",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("--config",       required=True, metavar="PATH",
                        help="Path to vault config YAML")
    parser.add_argument("--convert-only", action="store_true",
                        help="Only convert summaries to markdown, skip ingest")
    parser.add_argument("--ingest-only",  action="store_true",
                        help="Only run ingest + patch, skip conversion")
    parser.add_argument("--quiet",        action="store_true",
                        help="Suppress per-file output during convert")
    args = parser.parse_args()

    cfg = VaultConfig.from_yaml(args.config)

    if not args.ingest_only:
        convert(cfg, verbose=not args.quiet)

    if not args.convert_only:
        ingest(cfg)
        patch_urls(cfg)


if __name__ == "__main__":
    main()
