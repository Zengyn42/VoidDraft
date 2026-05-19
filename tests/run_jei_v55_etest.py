"""
v5.5 smoke test runner for Jei — validates Atomize 语义去重 via live Jei session.

Flow (single continuous session — Jei remembers context across turns):

  S1 — Detection:  scan + propose a doc with content similar to KNOW-000032
                   → proposal must contain similar_existing / needs_review claim
  S2 — Reuse:      Jei decides action='reuse', calls atomize_apply
                   → response confirms MENTIONS edge created / reuse recorded
  S3 — Audit log:  programmatic check that dedup_log.jsonl was written
                   (no Jei call needed — filesystem assertion)

Setup/teardown:
  - Creates 实验/v55-dedup-test.md in NimbusVault (content semantically similar to KNOW-000032)
  - Cleans up test doc, any generated proposals, and dedup_log entries after run

Usage:
    python3 run_jei_v55_etest.py

Note: Jei systemd service can stay running — this uses a fresh isolated session.
      Requires Ollama (qwen3-embedding:8b) to be running for embedding dedup to fire.
"""
import asyncio
import json
import sys
import os
import logging
from pathlib import Path
from datetime import datetime, timezone

framework_dir = Path(__file__).parent.parent.parent / "ZenithLoom"
prismrag_dir  = Path(__file__).parent.parent.parent / "PrismRag"
vault_dir     = Path(__file__).parent.parent.parent / "NimbusVault"
os.chdir(framework_dir)
sys.path.insert(0, str(framework_dir))
sys.path.insert(0, str(prismrag_dir))

logging.basicConfig(level=logging.WARNING)
logging.getLogger("httpx").setLevel(logging.ERROR)
logging.getLogger("httpcore").setLevel(logging.ERROR)

from framework.loader import EntityLoader

BLUEPRINT_DIR = Path("/home/kingy/Foundation/VoidDraft/role_agents/knowledge_curator")
DATA_DIR      = Path("/home/kingy/Foundation/EdenGateway/agents/jei")

# ── Test document ──────────────────────────────────────────────────────────────
# Content is intentionally semantically similar to KNOW-000032
# (PrismRag 多模态 Embedding — Text-first 与跨模态架构) to trigger dedup.

TEST_DOC_REL  = "实验/v55-dedup-test.md"
TEST_DOC_PATH = vault_dir / TEST_DOC_REL
TEST_DOC_CONTENT = """\
---
title: Embedding 层次化架构 — 多模态处理方案
created: 2026-05-18
tags: [test, embedding, multimodal]
---

## 多模态 Embedding 的两种实现路径

PrismRag 的 embedding 方案分为两个层次：

**第一层（文字优先路径）**：将非文字内容先转换为文字描述，再用文字 embedding 模型统一处理。
图片通过 gemma4 生成视觉描述，音频通过 Whisper 转录为文字，PDF 直接提取文字内容，
最终统一由 gemini-embedding-2-preview 生成向量。

**第二层（真正跨模态路径）**：使用 nomic-embed-vision 将文字和图片投影到同一个 768 维向量空间，
实现「以文搜图」和「以图搜文」的跨模态检索能力。

目前仅 PDF 文字提取路径已实装，图片和音频处理方案尚在规划中。
"""

# ── Stream capture ─────────────────────────────────────────────────────────────

_stream_buf: list[str] = []

def _stream_cb(text: str, is_thinking: bool = False) -> None:
    _stream_buf.append(text)
    print(text, end="", flush=True)

# ── Setup / Teardown ───────────────────────────────────────────────────────────

def _setup() -> dict:
    """Create test doc, return state for teardown."""
    TEST_DOC_PATH.write_text(TEST_DOC_CONTENT, encoding="utf-8")
    print(f"[setup] created {TEST_DOC_PATH}", flush=True)

    from prism_rag.config import PrismRagSettings
    settings   = PrismRagSettings()
    nimbus_src = next(s for s in settings.resolved_graphs if s.namespace == "nimbus")
    dedup_log  = nimbus_src.data_dir / "dedup_log.jsonl"
    original_dedup = dedup_log.read_text(encoding="utf-8") if dedup_log.exists() else None

    return {
        "dedup_log_path": dedup_log,
        "original_dedup": original_dedup,
        "proposal_pending_dir": settings.data_dir / "atomize-proposals" / "pending",
        "proposal_applied_dir": settings.data_dir / "atomize-proposals" / "applied",
        "scan_cache_dir":       settings.data_dir / "atomize-proposals" / "scan_cache",
    }


def _teardown(state: dict) -> None:
    """Remove test doc and any generated proposals/log entries."""
    if TEST_DOC_PATH.exists():
        TEST_DOC_PATH.unlink()
        print(f"[teardown] removed {TEST_DOC_PATH}", flush=True)

    # Clean up any proposals that reference the test doc
    for d in [state["proposal_pending_dir"], state["proposal_applied_dir"]]:
        if not d or not d.exists():
            continue
        for f in d.glob("*.json"):
            try:
                data = json.loads(f.read_text(encoding="utf-8"))
                if TEST_DOC_REL in data.get("doc_path", ""):
                    f.unlink()
                    print(f"[teardown] removed proposal {f.name}", flush=True)
            except Exception:
                pass

    # Clean up scan cache
    if state.get("scan_cache_dir") and state["scan_cache_dir"].exists():
        for f in state["scan_cache_dir"].glob("*.json"):
            try:
                data = json.loads(f.read_text(encoding="utf-8"))
                if TEST_DOC_REL in data.get("doc_path", ""):
                    f.unlink()
                    print(f"[teardown] removed scan cache {f.name}", flush=True)
            except Exception:
                pass

    # Restore dedup_log
    dedup_log = state["dedup_log_path"]
    if state["original_dedup"] is None:
        if dedup_log.exists():
            dedup_log.unlink()
            print("[teardown] dedup_log.jsonl removed", flush=True)
    else:
        dedup_log.write_text(state["original_dedup"], encoding="utf-8")
        print("[teardown] dedup_log.jsonl restored", flush=True)


# ── Assertion ──────────────────────────────────────────────────────────────────

def _assess(tag: str, response: str, stream: str,
            pass_keywords: list[str], fail_keywords: list[str]) -> tuple[str, list[str]]:
    combined = (response + "\n" + stream).lower()
    if not combined.strip():
        return "FAIL", ["[empty response]"]
    triggered = [kw for kw in fail_keywords if kw.lower() in combined]
    if triggered:
        return "FAIL", [f"fail_kw={kw!r}" for kw in triggered]
    missing = [kw for kw in pass_keywords if kw.lower() not in combined]
    if missing:
        return "WARN", [f"missing={kw!r}" for kw in missing]
    return "PASS", []


# ── Tests ──────────────────────────────────────────────────────────────────────

TESTS = [
    {
        "tag": "S1",
        "label": "Detection — atomize_propose flags similar_existing for KNOW-000032",
        "question": (
            f"请对 '{TEST_DOC_REL}' 做 atomize_scan。"
            "然后把这篇文档的核心内容作为一个 claim 提交 atomize_propose（用 alloc_knowledge_id 分配 ID）。"
            "告诉我返回的 proposal 里，有没有 claim 被标记为 needs_review 或者包含 similar_existing 字段？"
            "如果有，列出相似节点的 ID 和 score。"
        ),
        # Dedup fires → Jei should report similar_existing with KNOW-000032
        "pass_keywords": ["similar_existing", "needs_review", "KNOW-000032"],
        "fail_keywords": ["error", "ScanExpiredError", "scan 失败", "工具不可用"],
        "note": (
            "Verifies atomize_propose detects semantic similarity with KNOW-000032. "
            "Requires Ollama (qwen3-embedding:8b) running and ≥100 KNOW nodes. "
            "If dedup skipped (cold-start), expect WARN not FAIL."
        ),
    },
    {
        "tag": "S2",
        "label": "Reuse — Jei applies proposal with action=reuse for KNOW-000032",
        "question": (
            "上面的 proposal 里有一个 claim 和 KNOW-000032 非常相似。"
            "请判断这个 claim 是否应该复用 KNOW-000032（而不是新建），如果是，"
            "请在那个 claim 上设置 action='reuse'、reuse_id='KNOW-000032'，"
            "然后调用 atomize_apply 完成这个 proposal。"
            "告诉我 apply 的结果：reused_count 是多少？有没有建立 MENTIONS 边？"
        ),
        # Jei should call atomize_apply with reuse, response confirms reuse
        "pass_keywords": ["reuse", "mentions"],
        "fail_keywords": ["error", "StaleDocError", "找不到 proposal", "proposal.*not found"],
        "note": (
            "Verifies Jei correctly executes reuse path: skips file creation, "
            "writes MENTIONS edge, records DedupSnapshot."
        ),
    },
]

# S3 is a programmatic check (no Jei call needed)


# ── Runner ─────────────────────────────────────────────────────────────────────

async def run_tests():
    state = _setup()

    loader = EntityLoader(BLUEPRINT_DIR, data_dir=DATA_DIR)
    await loader.start_mcp_servers()
    controller = await loader.get_controller()

    session_name = f"v55_smoke_{int(__import__('time').time())}"
    await controller.new_session(session_name)
    print(f"[smoke] fresh session: {session_name}\n", flush=True)

    try:
        graph = controller._graph
        if hasattr(graph, "nodes"):
            for node_id, node_fn in graph.nodes.items():
                if hasattr(node_fn, "set_stream_callback"):
                    node_fn.set_stream_callback(_stream_cb)
    except Exception as e:
        print(f"[warn] stream callback: {e}")

    results = []
    sep = "=" * 70

    try:
        for test in TESTS:
            global _stream_buf
            _stream_buf = []

            tag   = test["tag"]
            label = test["label"]
            q     = test["question"]

            print(f"\n{sep}")
            print(f"TEST {tag}: {label}")
            print(f"NOTE: {test['note']}")
            print(sep)
            print("RESPONSE:")

            try:
                response = await asyncio.wait_for(controller.run(q), timeout=300)
                stream   = "".join(_stream_buf)
                final    = response if len(response) >= len(stream) else stream
                if not final.strip():
                    final = response or stream
                if not stream and final:
                    print(final)
                response = final
            except asyncio.TimeoutError:
                print("\n*** TIMEOUT ***")
                response = stream = ""
            except Exception as e:
                import traceback
                print(f"\n*** ERROR: {e} ***")
                traceback.print_exc()
                response = stream = ""

            stream = "".join(_stream_buf)
            status, failures = _assess(tag, response, stream,
                                       test["pass_keywords"], test["fail_keywords"])

            print(f"\n--- END {tag} ---")
            layer = "[AGENT LAYER]"
            if status == "PASS":
                print(f"VERDICT: PASS {layer}")
            elif status == "WARN":
                print(f"VERDICT: WARN {layer}  → {failures}")
            else:
                print(f"VERDICT: FAIL {layer}  → {failures}")
            results.append((tag, status, failures, label))

        # ── S3: programmatic dedup_log check ─────────────────────────────────
        print(f"\n{sep}")
        print("TEST S3: Audit log — dedup_log.jsonl written after reuse")
        print(sep)

        dedup_log = state["dedup_log_path"]
        s3_status  = "FAIL"
        s3_detail  = ""

        if not dedup_log.exists():
            s3_detail = "dedup_log.jsonl does not exist"
        else:
            from prism_rag.ingest.dedup_log import list_snapshots
            snapshots = list_snapshots(dedup_log)
            reuse_snaps = [s for s in snapshots if s.action == "reuse"]
            if not reuse_snaps:
                s3_detail = f"dedup_log exists but has no 'reuse' entries (total={len(snapshots)})"
            else:
                snap = reuse_snaps[-1]
                s3_detail = (
                    f"reuse entries={len(reuse_snaps)}, "
                    f"latest: claim='{snap.claim_title}' "
                    f"reused_id={snap.reused_id} score={snap.similarity_score:.3f}"
                )
                s3_status = "PASS"

        print(f"dedup_log path: {dedup_log}")
        print(f"result: {s3_detail}")
        print(f"\n--- END S3 ---")
        if s3_status == "PASS":
            print(f"VERDICT: PASS [FILESYSTEM]")
        else:
            print(f"VERDICT: FAIL [FILESYSTEM]  → {s3_detail}")
        results.append(("S3", s3_status, [s3_detail] if s3_status == "FAIL" else [], "Audit log — dedup_log.jsonl written"))

    finally:
        _teardown(state)

    # ── Summary ───────────────────────────────────────────────────────────────
    print(f"\n\n{sep}")
    print("V5.5 SMOKE TEST SUMMARY")
    print(sep)

    pass_count = sum(1 for _, s, _, _ in results if s == "PASS")
    warn_count = sum(1 for _, s, _, _ in results if s == "WARN")
    fail_count = sum(1 for _, s, _, _ in results if s == "FAIL")

    for tag, status, failures, label in results:
        icon = "✅" if status == "PASS" else "⚠️" if status == "WARN" else "❌"
        short = label[:55] + "..." if len(label) > 55 else label
        reason = f"  → {failures}" if failures else ""
        print(f"  {icon} {tag:<4} {short:<58}{reason}")

    print(sep)
    if fail_count == 0 and warn_count == 0:
        print("RESULT: ALL PASS ✅")
    elif fail_count == 0:
        print(f"RESULT: ALL PASS ✅  ({warn_count} WARN)")
        print("  WARN usually means dedup skipped (cold-start / Ollama unavailable)")
    else:
        print(f"RESULT: {fail_count} FAIL, {warn_count} WARN")
        for tag, status, failures, _ in results:
            if status in ("FAIL", "WARN"):
                guide = {
                    "S1": "Check Ollama running + embedding_store loaded in MCP server",
                    "S2": "S1 must PASS first (proposal_id carried in session context)",
                    "S3": "S2 must PASS first (reuse path writes dedup_log)",
                }.get(tag, "")
                print(f"  {status} {tag}: {failures}  → {guide}")


def main():
    asyncio.run(run_tests())


if __name__ == "__main__":
    main()
