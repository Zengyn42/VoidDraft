#!/usr/bin/env python3
"""
rednote_build_vault.py — Convert rednote summary.json files to a PrismRag vault,
then run prism-rag ingest to build a proper knowledge graph with semantic edges.

Output vault: /home/kingy/Foundation/EdenGateway/rednote/
  sources/         ← markdown files (one per video post)
  data/            ← graph.json, graph.html, embeddings

Fixes v2:
  - Node labels use video title (via knowledge_id in frontmatter)
  - Removed meaningless "actionable" tag
  - Topic normalization: 167 fragmented topics → ~25 canonical categories
  - Filename uses title only (no post_id prefix)

Usage
-----
  python3 rednote_build_vault.py            # convert + ingest
  python3 rednote_build_vault.py --convert-only  # only write markdown files
  python3 rednote_build_vault.py --ingest-only   # only run prism-rag ingest
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

# ── Paths ──────────────────────────────────────────────────────────────────────
_DOWNLOADS_DIR  = Path("/home/kingy/Foundation/EdenGateway/rednote_downloads")
_VAULT_ROOT     = Path("/home/kingy/Foundation/EdenGateway/rednote")
_SOURCES_DIR    = _VAULT_ROOT / "sources"
_DATA_DIR       = _VAULT_ROOT / "data"
_PRISMRAG_ROOT  = Path("/home/kingy/Foundation/PrismRag")
_VENV_PYTHON    = _PRISMRAG_ROOT / ".venv" / "bin" / "python3"


# ── Topic normalization map ────────────────────────────────────────────────────
# Maps any variant → canonical topic (used as [[wikilink]] hub node)
_TOPIC_MAP: dict[str, str] = {
    # AI 工具
    "AI 创业": "AI工具", "AI 工具": "AI工具", "AI 工具/副业赚钱": "AI工具",
    "AI 工具/效率提升": "AI工具", "AI 工具/理财投资": "AI工具",
    "AI 工具学习": "AI工具", "AI 工具教程": "AI工具", "AI 应用技巧": "AI工具",
    "AI 技术学习": "AI工具", "AI 行业与职业规划": "AI工具", "AI/科技": "AI工具",
    "AI前沿": "AI工具", "AI学习": "AI工具", "AI工具": "AI工具",
    "AI工具/副业赚钱": "AI工具", "AI工具分享": "AI工具", "AI工具教程": "AI工具",
    "AI应用与职场成长": "AI工具", "AI技术": "AI工具", "人工智能": "AI工具",
    "人工智能/AI技巧": "AI工具", "人工智能/技术教程": "AI工具",
    "人工智能/软件工程": "AI工具", "科技/AI 工具": "AI工具",
    "职场效率/AI 工具": "AI工具", "学习工具": "AI工具", "效率工具": "AI工具",
    "科技工具": "AI工具", "科技效率": "AI工具",
    # 美妆发型
    "美妆": "美妆发型", "美妆发型": "美妆发型", "美妆变美": "美妆发型",
    "美妆护肤": "美妆发型", "美妆教程": "美妆发型", "美容护肤": "美妆发型",
    "医美护肤": "美妆发型", "个人形象管理": "美妆发型", "旅游变美": "美妆发型",
    "婚礼跟妆": "美妆发型",
    # 穿搭时尚
    "穿搭分享": "穿搭时尚", "穿搭推荐": "穿搭时尚", "穿搭时尚": "穿搭时尚",
    "穿搭购物": "穿搭时尚", "时尚穿搭": "穿搭时尚", "女装推荐": "穿搭时尚",
    "女装穿搭": "穿搭时尚", "服装推荐": "穿搭时尚", "服装穿搭": "穿搭时尚",
    # 健身运动
    "健身": "健身运动", "健身减肥": "健身运动", "健身塑形": "健身运动",
    "健身教学": "健身运动", "健身教程": "健身运动", "健身运动": "健身运动",
    "健身舞蹈": "健身运动", "运动健身": "健身运动", "减肥瘦身": "健身运动",
    "减脂健身": "健身运动", "体态矫正": "健身运动", "体态纠正": "健身运动",
    # 舞蹈教程
    "舞蹈健身": "舞蹈教程", "舞蹈娱乐": "舞蹈教程", "舞蹈教学": "舞蹈教程",
    "舞蹈教程": "舞蹈教程",
    # 母婴育儿
    "母婴": "母婴育儿", "母婴健康": "母婴育儿", "母婴喂养": "母婴育儿",
    "母婴好物": "母婴育儿", "母婴育儿": "母婴育儿", "早教育儿": "母婴育儿",
    "育儿亲子": "母婴育儿", "育儿健康": "母婴育儿", "育儿教育": "母婴育儿",
    "育儿母婴": "母婴育儿", "育儿省钱": "母婴育儿", "育儿知识": "母婴育儿",
    "育儿经验": "母婴育儿", "家庭教育": "母婴育儿",
    # 孕期备孕
    "孕期准备": "孕期备孕", "孕期护理": "孕期备孕", "孕期日常": "孕期备孕",
    "孕期科普": "孕期备孕", "孕期运动": "孕期备孕", "备孕指导": "孕期备孕",
    "备孕育儿": "孕期备孕",
    # 健康养生
    "健康养生": "健康养生", "健康美食": "健康养生", "健康饮食": "健康养生",
    "宠物健康养生": "健康养生",
    # 宠物
    "宠物健康": "宠物", "宠物养护": "宠物", "宠物医疗": "宠物",
    "宠物医疗护理": "宠物", "宠物情感": "宠物", "宠物护理": "宠物",
    # 情感关系
    "情感": "情感关系", "情感关系": "情感关系", "情感励志": "情感关系",
    "情感婚姻": "情感关系", "情感成长": "情感关系", "情感故事": "情感关系",
    "情感沟通": "情感关系", "情感综艺": "情感关系", "婚姻家庭": "情感关系",
    "家庭关系": "情感关系", "家庭生活": "情感关系", "人际交往": "情感关系",
    # 美食
    "美食": "美食", "美食 Vlog": "美食", "美食挑战": "美食",
    "美食探店": "美食", "美食教程": "美食", "美食日常": "美食",
    "美食生活": "美食",
    # 购物省钱
    "购物分享": "购物省钱", "购物分享/女装推荐": "购物省钱", "购物技巧": "购物省钱",
    "购物攻略": "购物省钱", "购物省钱": "购物省钱", "省钱攻略": "购物省钱",
    "网购技巧": "购物省钱", "网购攻略": "购物省钱", "网购省钱攻略": "购物省钱",
    "礼物推荐": "购物省钱",
    # 理财投资
    "理财投资": "理财投资", "财经投资": "理财投资", "创业副业": "理财投资",
    "电商创业": "理财投资",
    # 职场成长
    "职场干货": "职场成长", "职场成长": "职场成长", "职场沟通": "职场成长",
    "职场英语": "职场成长", "职场面试": "职场成长", "口才训练": "职场成长",
    "个人成长": "职场成长",
    # 家居生活
    "家居 DIY": "家居生活", "家居好物": "家居生活", "家居收纳": "家居生活",
    "家居生活": "家居生活", "家居装修": "家居生活",
    # 学习教育
    "教育学习": "学习教育", "教育科普": "学习教育", "知识科普": "学习教育",
    "英语学习": "学习教育", "语言学习": "学习教育", "编程学习": "学习教育",
    "编程开发": "学习教育", "科研工具": "学习教育",
    # 数码科技
    "数码科技": "数码科技", "科技医疗": "数码科技", "科技数码": "数码科技",
    "科技科普": "数码科技",
    # 娱乐
    "娱乐": "娱乐", "娱乐八卦": "娱乐", "娱乐搞笑": "娱乐",
    "影视剧情": "娱乐", "剧情解说": "娱乐", "生活趣事": "娱乐",
    "生活记录": "娱乐",
    # 摄影
    "摄影技巧": "摄影", "摄影教程": "摄影",
    # 生活技巧
    "生活妙招": "生活技巧", "生活技巧": "生活技巧",
    # 远程工作
    "远程工作": "远程工作",
    # 其他
    "其他": "其他", "无法分类": "其他", "无法识别": "其他",
}


def _normalize_topic(raw: str) -> str:
    """Return canonical topic, or raw stripped if not in map."""
    return _TOPIC_MAP.get(raw.strip(), raw.strip())


# ── Markdown generation ────────────────────────────────────────────────────────

def _slugify(text: str) -> str:
    import re
    return re.sub(r"[^\w\u4e00-\u9fff]", "_", text)[:60].strip("_")


def _make_markdown(data: dict) -> str:
    post_id     = data.get("post_id", "")
    post_title  = data.get("post_title", post_id)
    summary     = data.get("summary", "").strip()
    key_points  = data.get("key_points", [])
    raw_topic   = data.get("topic", "").strip()
    topic       = _normalize_topic(raw_topic)
    audience    = data.get("target_audience", "").strip()
    actionable  = data.get("actionable", False)

    # Tags: only semantic category tags (no actionable boolean)
    tags = [topic] if topic else []
    tags_yaml = "[" + ", ".join(f'"{t}"' for t in tags) + "]"

    safe_title    = post_title.replace('"', "'")
    safe_audience = audience.replace('"', "'")[:80]

    lines = [
        "---",
        # knowledge_id makes PrismRag use frontmatter title as node label
        f'knowledge_id: "{post_id}"',
        f'title: "{safe_title}"',
        f"tags: {tags_yaml}",
        f"topic: \"{topic}\"",
        f'target_audience: "{safe_audience}"',
        f"actionable: {str(actionable).lower()}",
        'source: "rednote"',
        "---",
        "",
        f"# {post_title}",
        "",
        "## 摘要",
        "",
        summary,
        "",
    ]

    if key_points:
        lines += ["## 知识要点", ""]
        for kp in key_points:
            lines.append(f"- {kp.strip()}")
        lines.append("")

    if topic:
        lines += [
            "## 分类",
            "",
            f"- 主题：[[{topic}]]",
            f"- 受众：{audience}",
            "",
        ]

    return "\n".join(lines)


# ── Convert summaries → markdown ───────────────────────────────────────────────

def convert(verbose: bool = True) -> int:
    # Clear old sources and rebuild
    import shutil
    if _SOURCES_DIR.exists():
        shutil.rmtree(_SOURCES_DIR)
    _SOURCES_DIR.mkdir(parents=True, exist_ok=True)

    summary_files = sorted(_DOWNLOADS_DIR.rglob("*.summary.json"))
    written = 0

    for sf in summary_files:
        try:
            data = json.loads(sf.read_text(encoding="utf-8"))
        except Exception as e:
            print(f"  [warn] {sf.name}: {e}")
            continue

        post_title = data.get("post_title", data.get("post_id", sf.stem))
        # Filename = title slug only (no post_id prefix)
        filename   = f"{_slugify(post_title)}.md"
        out_path   = _SOURCES_DIR / filename

        # Handle duplicate titles by appending a counter
        counter = 1
        while out_path.exists():
            filename = f"{_slugify(post_title)}_{counter}.md"
            out_path = _SOURCES_DIR / filename
            counter += 1

        md = _make_markdown(data)
        out_path.write_text(md, encoding="utf-8")
        written += 1
        if verbose:
            print(f"  [convert] {filename}")

    print(f"\n[build_vault] Converted {written} summaries → {_SOURCES_DIR}")
    return written


# ── Run prism-rag ingest ───────────────────────────────────────────────────────

def _rednote_url(post_id: str) -> str:
    return f"https://www.xiaohongshu.com/explore/{post_id}"


def patch_urls() -> None:
    """Post-process: inject rednote URLs into graph node sig field, regenerate HTML."""
    import re
    _POST_ID_RE = re.compile(r"^[0-9a-f]{24}$")

    graph_path = _DATA_DIR / "rednote" / "graph.json"
    html_path  = _DATA_DIR / "rednote" / "graph.html"

    if not graph_path.exists():
        print(f"[patch_urls] graph.json not found: {graph_path}")
        return

    sys.path.insert(0, str(_PRISMRAG_ROOT))
    from prism_rag.store.graph import KnowledgeGraph
    from prism_rag.report.visualize import generate_html

    kg = KnowledgeGraph.load(graph_path)
    patched = 0
    for node_id, attrs in kg.g.nodes(data=True):
        # Node id = post_id (24-char hex) for rednote knowledge nodes
        if _POST_ID_RE.match(str(node_id)):
            url = _rednote_url(node_id)
            meta = attrs.get("metadata") or {}
            meta["signature"] = url
            attrs["metadata"] = meta
            patched += 1

    print(f"[patch_urls] Patched {patched} nodes with rednote URLs")
    kg.save(graph_path)
    generate_html(kg, html_path, vault_name="rednote知识图谱")
    print(f"[patch_urls] HTML regenerated: {html_path}")


def ingest() -> None:
    print(f"\n[build_vault] Running prism-rag ingest…")
    print(f"  vault    : {_VAULT_ROOT}")
    print(f"  output   : {_DATA_DIR}")
    print(f"  namespace: rednote")

    _DATA_DIR.mkdir(parents=True, exist_ok=True)

    cmd = [
        str(_VENV_PYTHON), "-m", "prism_rag.cli", "ingest",
        "--vault",     str(_VAULT_ROOT),
        "--output",    str(_DATA_DIR),
        "--namespace", "rednote",
    ]

    result = subprocess.run(cmd, cwd=str(_PRISMRAG_ROOT))
    if result.returncode != 0:
        print(f"[build_vault] ingest failed (exit {result.returncode})")
        sys.exit(result.returncode)

    html = _DATA_DIR / "rednote" / "graph.html"
    if html.exists():
        print(f"\n[build_vault] Graph HTML: {html}")
    print("[build_vault] Done.")


# ── CLI ────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Build a PrismRag vault from rednote summaries and run ingest"
    )
    parser.add_argument("--convert-only", action="store_true", help="Only convert summaries to markdown")
    parser.add_argument("--ingest-only",  action="store_true", help="Only run prism-rag ingest (skip conversion)")
    parser.add_argument("--quiet",        action="store_true", help="Suppress per-file output")
    args = parser.parse_args()

    if not args.ingest_only:
        convert(verbose=not args.quiet)

    if not args.convert_only:
        ingest()

    if not args.convert_only:
        patch_urls()
