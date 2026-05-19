"""
v5.5 extended smoke tests — additional dedup scenarios beyond S1-S3.

S4 — Create path:    similar_existing detected but Jei decides concept is different → create
S5 — Rollback:       after reuse, rollback_dedup() removes MENTIONS edge from graph
S6 — Mixed decision: doc with 2 claims: one reuse + one create in same apply
S7 — Threshold boundary: content with moderate similarity should NOT trigger similar_existing

All tests run in a single session (Jei remembers context across turns).
Setup/teardown creates and cleans up test documents and proposals.

Usage:
    python3 run_jei_v55_extended_etest.py
"""
import asyncio
import json
import sys
import os
import logging
from pathlib import Path

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

# ── Test documents ─────────────────────────────────────────────────────────────

# S4/S5/S7: similar to KNOW-000032 (multimodal embedding)
TEST_DOC_A_REL  = "实验/v55-ext-test-a.md"
TEST_DOC_A_PATH = vault_dir / TEST_DOC_A_REL
TEST_DOC_A_CONTENT = """\
---
title: 多模态 Embedding — 实现路径概述
created: 2026-05-18
tags: [test, embedding]
---

## PrismRag Embedding 的两个层次

PrismRag 采用两层 embedding 方案：

**第一层（文字优先）**：图片由 gemma4 转文字描述，音频用 Whisper 转录，
最终由 gemini-embedding-2-preview 统一向量化处理。

**第二层（跨模态）**：nomic-embed-vision 将文字与图片映射至 768 维统一向量空间，
支持以文搜图和以图搜文。
"""

# S6: doc with 2 clearly distinct sections
TEST_DOC_B_REL  = "实验/v55-ext-test-b.md"
TEST_DOC_B_PATH = vault_dir / TEST_DOC_B_REL
TEST_DOC_B_CONTENT = """\
---
title: 混合决策测试文档
created: 2026-05-18
tags: [test, mixed]
---

## 第一节：多模态 Embedding 两层架构（与 KNOW-000032 相似）

PrismRag 设计了两层 embedding 方案：文字优先层和真正跨模态层。
图片通过 gemma4 描述后向量化，nomic-embed-vision 实现 768 维统一空间。

## 第二节：ZenithLoom Agent 内存管理（全新概念）

ZenithLoom 中 Agent 的内存分为三层：短期对话上下文（SQLite checkpoint）、
中期 session 摘要（compact 压缩）、长期 MEMORY.md 持久存储。
三层之间通过 !compact 命令手动触发迁移，无自动压缩策略。
"""

# S7: low-similarity content (about Kubernetes, unrelated to any KNOW node)
TEST_DOC_C_REL  = "实验/v55-ext-test-c.md"
TEST_DOC_C_PATH = vault_dir / TEST_DOC_C_REL
TEST_DOC_C_CONTENT = """\
---
title: Kubernetes Pod 调度策略
created: 2026-05-18
tags: [test, kubernetes]
---

## Pod 亲和性与反亲和性

Kubernetes 的 Pod 调度通过 nodeSelector、nodeAffinity 和 podAffinity 控制。
preferredDuringSchedulingIgnoredDuringExecution 提供软性约束，
requiredDuringSchedulingIgnoredDuringExecution 提供硬性约束。
Taint 和 Toleration 用于排斥不兼容的 Pod 落到特定节点。
"""

ALL_TEST_DOCS = [
    (TEST_DOC_A_REL, TEST_DOC_A_PATH, TEST_DOC_A_CONTENT),
    (TEST_DOC_B_REL, TEST_DOC_B_PATH, TEST_DOC_B_CONTENT),
    (TEST_DOC_C_REL, TEST_DOC_C_PATH, TEST_DOC_C_CONTENT),
]

# ── Stream capture ─────────────────────────────────────────────────────────────

_stream_buf: list[str] = []

def _stream_cb(text: str, is_thinking: bool = False) -> None:
    _stream_buf.append(text)
    print(text, end="", flush=True)

# ── Setup / Teardown ───────────────────────────────────────────────────────────

def _setup() -> dict:
    for rel, path, content in ALL_TEST_DOCS:
        path.write_text(content, encoding="utf-8")
        print(f"[setup] created {path}", flush=True)

    from prism_rag.config import PrismRagSettings
    settings   = PrismRagSettings()
    nimbus_src = next(s for s in settings.resolved_graphs if s.namespace == "nimbus")
    dedup_log  = nimbus_src.data_dir / "dedup_log.jsonl"

    return {
        "dedup_log_path":       dedup_log,
        "original_dedup":       dedup_log.read_text(encoding="utf-8") if dedup_log.exists() else None,
        "graph_path":           nimbus_src.data_dir / "graph.json",
        "proposal_pending_dir": settings.data_dir / "atomize-proposals" / "pending",
        "proposal_applied_dir": settings.data_dir / "atomize-proposals" / "applied",
        "scan_cache_dir":       settings.data_dir / "atomize-proposals" / "scan_cache",
    }


def _teardown(state: dict) -> None:
    # Remove test docs
    test_rels = {rel for rel, _, _ in ALL_TEST_DOCS}
    for _, path, _ in ALL_TEST_DOCS:
        if path.exists():
            path.unlink()
            print(f"[teardown] removed {path}", flush=True)

    # Remove proposals referencing any test doc
    for d in [state.get("proposal_pending_dir"), state.get("proposal_applied_dir")]:
        if not d or not d.exists():
            continue
        for f in d.glob("*.json"):
            try:
                data = json.loads(f.read_text(encoding="utf-8"))
                if any(rel in data.get("doc_path", "") for rel in test_rels):
                    f.unlink()
                    print(f"[teardown] removed proposal {f.name}", flush=True)
            except Exception:
                pass

    # Remove scan caches for test docs
    sc = state.get("scan_cache_dir")
    if sc and sc.exists():
        for f in sc.glob("*.json"):
            try:
                data = json.loads(f.read_text(encoding="utf-8"))
                if any(rel in data.get("doc_path", "") for rel in test_rels):
                    f.unlink()
                    print(f"[teardown] removed scan cache {f.name}", flush=True)
            except Exception:
                pass

    # Remove KNOW files created by test
    know_dir = vault_dir / "knowledge"
    if know_dir.exists():
        for f in know_dir.glob("KNOW-*.md"):
            try:
                text = f.read_text(encoding="utf-8")
                if any(rel in text for rel in test_rels):
                    f.unlink()
                    print(f"[teardown] removed {f.name}", flush=True)
            except Exception:
                pass

    # Restore dedup_log
    dedup_log = state["dedup_log_path"]
    orig = state["original_dedup"]
    if orig is None:
        dedup_log.unlink(missing_ok=True)
        print("[teardown] dedup_log.jsonl removed", flush=True)
    else:
        dedup_log.write_text(orig, encoding="utf-8")
        print("[teardown] dedup_log.jsonl restored", flush=True)


# ── Assessment ─────────────────────────────────────────────────────────────────

def _assess(response: str, stream: str, pass_kw: list, fail_kw: list) -> tuple[str, list]:
    combined = (response + "\n" + stream).lower()
    if not combined.strip():
        return "FAIL", ["[empty response]"]
    triggered = [kw for kw in fail_kw if kw.lower() in combined]
    if triggered:
        return "FAIL", [f"fail_kw={kw!r}" for kw in triggered]
    missing = [kw for kw in pass_kw if kw.lower() not in combined]
    if missing:
        return "WARN", [f"missing={kw!r}" for kw in missing]
    return "PASS", []


# ── Tests ──────────────────────────────────────────────────────────────────────

TESTS = [
    # ── S4: Create path ────────────────────────────────────────────────────────
    {
        "tag": "S4",
        "label": "Create path — similar detected, Jei decides to create new node",
        "question": (
            f"请对 '{TEST_DOC_A_REL}' 做 atomize_scan，然后把核心内容作为一个 claim 提交 atomize_propose。"
            "如果返回 needs_review，请分析 similar_existing 里的节点：假设你判断这个新内容虽然相似，"
            "但侧重点不同（它比 KNOW-000032 更简短、缺少具体模型名称），"
            "决定 action='create'（新建），然后用新 proposal_id 调用 atomize_apply。"
            "告诉我 applied_count 和 reused_count 各是多少。"
        ),
        "pass_keywords": ["applied_count", "1", "reused_count", "0"],
        "fail_keywords": ["error", "StaleDocError"],
        "note": "Verifies create path: even when similar_existing fires, Jei can choose action='create'.",
    },
    # ── S5: Rollback ───────────────────────────────────────────────────────────
    {
        "tag": "S5",
        "label": "Rollback — reuse then rollback_dedup removes MENTIONS edge",
        "question": (
            f"现在对 '{TEST_DOC_A_REL}' 重新做 atomize_scan + atomize_propose（用新 ID），"
            "这次在 claim 里设置 action='reuse'、reuse_id='KNOW-000032'，然后 atomize_apply。"
            "apply 成功后，请立刻调用 list_dedup_log 找到刚才的 decision_id，"
            "然后用 rollback_dedup(decision_id) 撤销这次复用。"
            "告诉我 rollback 的结果里说了什么（有没有提到 removed 或 edge）。"
        ),
        "pass_keywords": ["removed", "rollback"],
        "fail_keywords": ["error", "not found", "nothing to roll back"],
        "note": "Verifies rollback_dedup removes MENTIONS edge and marks snapshot rolled_back.",
    },
    # ── S6: Mixed decision (2 claims, 1 reuse + 1 create) ────────────────────
    {
        "tag": "S6",
        "label": "Mixed decision — 2 claims: one reuse + one create in same apply",
        "question": (
            f"请对 '{TEST_DOC_B_REL}' 做 atomize_scan。"
            "这篇文档有两个主要概念：\n"
            "  1. 多模态 Embedding 两层架构（和 KNOW-000032 相似，应该 reuse）\n"
            "  2. ZenithLoom Agent 内存管理（全新概念，应该 create）\n"
            "请分别为这两个概念 alloc_knowledge_id，构造两个 claims，\n"
            "第一个 claim 设置 action='reuse'、reuse_id='KNOW-000032'，\n"
            "第二个 claim 保持 action='create'（或不设置），\n"
            "提交 atomize_propose，然后 atomize_apply。\n"
            "告诉我 reused_count 和 applied_count 分别是多少。"
        ),
        "pass_keywords": ["reused_count", "1", "applied_count", "1"],
        "fail_keywords": ["error", "StaleDocError"],
        "note": "Verifies mixed reuse+create in a single proposal apply call.",
    },
    # ── S7: Threshold boundary (unrelated content) ────────────────────────────
    {
        "tag": "S7",
        "label": "Threshold boundary — unrelated content produces no similar_existing",
        "question": (
            f"请对 '{TEST_DOC_C_REL}' 做 atomize_scan，"
            "然后把这篇关于 Kubernetes Pod 调度的内容作为一个 claim 提交 atomize_propose。"
            "告诉我这次返回的 claim_status 是什么？有没有 similar_existing 字段？"
            "如果没有 needs_review，说明阈值过滤正常工作。"
        ),
        # Jei should confirm status=pending and absence of similar_existing.
        # Don't put 'needs_review'/'similar_existing' in fail_keywords — Jei mentions them
        # while explaining they were NOT present, causing false positives.
        "pass_keywords": ["pending", "阈值"],
        "fail_keywords": ["error", "StaleDocError"],
        "note": "Verifies threshold: unrelated content (Kubernetes) produces no similar_existing.",
    },
]


# ── Runner ─────────────────────────────────────────────────────────────────────

async def run_tests():
    state = _setup()

    loader = EntityLoader(BLUEPRINT_DIR, data_dir=DATA_DIR)
    await loader.start_mcp_servers()
    controller = await loader.get_controller()

    session_name = f"v55_ext_{int(__import__('time').time())}"
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
            status, failures = _assess(response, stream, test["pass_keywords"], test["fail_keywords"])

            print(f"\n--- END {tag} ---")
            layer = "[AGENT LAYER]"
            verdict = (
                f"VERDICT: PASS {layer}" if status == "PASS"
                else f"VERDICT: WARN {layer}  → {failures}" if status == "WARN"
                else f"VERDICT: FAIL {layer}  → {failures}"
            )
            print(verdict)
            results.append((tag, status, failures, label))

        # ── S5b: Programmatic rollback verification ────────────────────────────
        print(f"\n{sep}")
        print("TEST S5b: Programmatic — verify rollback_status='rolled_back' in dedup_log")
        print(sep)

        from prism_rag.ingest.dedup_log import list_snapshots
        snapshots = list_snapshots(state["dedup_log_path"])
        rolled = [s for s in snapshots if s.rollback_status == "rolled_back"]
        if rolled:
            snap = rolled[-1]
            print(f"  rolled_back entries: {len(rolled)}")
            print(f"  latest: claim='{snap.claim_title}' reused_id={snap.reused_id}")
            s5b_status, s5b_detail = "PASS", []
        else:
            s5b_status = "WARN"
            s5b_detail = ["no rolled_back entries found (S5 may have WARN'd)"]
            print(f"  {s5b_detail[0]}")

        print(f"--- END S5b ---")
        print(f"VERDICT: {'PASS [FILESYSTEM]' if s5b_status == 'PASS' else 'WARN [FILESYSTEM]  → ' + str(s5b_detail)}")
        results.append(("S5b", s5b_status, s5b_detail, "Rollback status in dedup_log"))

    finally:
        _teardown(state)

    # ── Summary ───────────────────────────────────────────────────────────────
    print(f"\n\n{sep}")
    print("V5.5 EXTENDED SMOKE TEST SUMMARY")
    print(sep)

    pass_count = sum(1 for _, s, _, _ in results if s == "PASS")
    warn_count = sum(1 for _, s, _, _ in results if s == "WARN")
    fail_count = sum(1 for _, s, _, _ in results if s == "FAIL")

    for tag, status, failures, label in results:
        icon = "✅" if status == "PASS" else "⚠️" if status == "WARN" else "❌"
        short = label[:55] + "..." if len(label) > 55 else label
        reason = f"  → {failures}" if failures else ""
        print(f"  {icon} {tag:<5} {short:<57}{reason}")

    print(sep)
    if fail_count == 0 and warn_count == 0:
        print("RESULT: ALL PASS ✅")
    elif fail_count == 0:
        print(f"RESULT: ALL PASS ✅  ({warn_count} WARN)")
    else:
        print(f"RESULT: {fail_count} FAIL, {warn_count} WARN")
        for tag, status, failures, _ in results:
            if status in ("FAIL", "WARN"):
                guide = {
                    "S4": "Create path: check action field preserved in proposal JSON",
                    "S5": "Rollback: check rollback_dedup MCP tool exposed and reachable",
                    "S5b": "Follows S5 — rolled_back status only set if S5 ran rollback_dedup",
                    "S6": "Mixed: check atomize_apply handles reuse+create in same proposal",
                    "S7": "Threshold: K8s content should score < 0.90 vs all existing KNOW nodes",
                }.get(tag, "")
                print(f"  {status} {tag}: {failures}  → {guide}")


def main():
    asyncio.run(run_tests())


if __name__ == "__main__":
    main()
