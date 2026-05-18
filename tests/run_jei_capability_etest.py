"""
Jei capability test runner — validates graph traversal, cross-namespace search,
multi-hop reasoning, and vault CRUD operations via live Jei CLI session.

T1 — Cross-namespace: query spanning nimbus (docs) + code namespace
T2 — trace_path: path between two known nodes
T3 — list_communities: community overview and theme summary
T4 — Multi-hop reasoning: search + traverse + synthesize
T5 — read_note: direct file read via vault path
T6 — Federation search: hybrid query hitting both namespaces
T7 — pending_edges: list pending edge proposals

Usage:
    cd /path/to/VoidDraft/tests
    python3 run_jei_capability_etest.py

Note: Jei systemd service can stay running — this runner uses a separate fresh session.

Pass criteria:
    PASS  = all pass_keywords found in response
    WARN  = no fail_keywords, but some pass_keywords missing (prompt/docstring issue)
    FAIL  = fail_keywords triggered OR empty response (code-layer issue)
"""
import asyncio
import sys
import os
import logging
from pathlib import Path

framework_dir = Path(__file__).parent.parent.parent / "ZenithLoom"
os.chdir(framework_dir)
sys.path.insert(0, str(framework_dir))

logging.basicConfig(level=logging.WARNING)
logging.getLogger("httpx").setLevel(logging.ERROR)
logging.getLogger("httpcore").setLevel(logging.ERROR)

from framework.loader import EntityLoader

BLUEPRINT_DIR = Path(__file__).parent.parent / "role_agents/knowledge_curator"
DATA_DIR      = Path(__file__).parent.parent.parent / "EdenGateway/agents/jei"

TESTS = [
    # ── T1: Cross-namespace query (code graph) ────────────────────────────────
    {
        "tag": "T1",
        "label": "Cross-namespace — query code graph for implementation details",
        "question": "search_knowledge 函数在 PrismRag 代码里是怎么实现的？它调用了哪些主要子函数或模块？",
        "pass_keywords": ["hybrid_search", "bm25", "server.py"],
        "fail_keywords": ["无法找到", "搜索结果为空", "不存在", "没有相关"],
        "layer": "agent",
        "note": "Verifies Jei queries the code namespace and returns function-level details.",
    },

    # ── T2: trace_path between two nodes ─────────────────────────────────────
    {
        "tag": "T2",
        "label": "trace_path — find relationship path between two vault nodes",
        "question": "PrismRag v5.4 设计文档和 atomize 相关的文档之间，在知识图谱里有没有连接路径？请用 trace_path 找一下。",
        "pass_keywords": ["path", "→", "edge"],
        "fail_keywords": ["工具不可用", "tool not found", "没有路径", "无法连接"],
        "layer": "agent",
        "note": "Verifies Jei uses trace_path tool and returns a valid node path.",
    },

    # ── T3: list_communities overview ─────────────────────────────────────────
    {
        "tag": "T3",
        "label": "list_communities — graph community overview",
        "question": "知识图谱里现在有哪些主题社区（community）？每个社区的核心话题是什么？",
        "pass_keywords": ["community", "社区"],
        "fail_keywords": ["工具不可用", "tool not found", "没有社区", "无法列出"],
        "layer": "agent",
        "note": "Verifies list_communities tool works and Jei summarizes community themes.",
    },

    # ── T4: Multi-hop reasoning ───────────────────────────────────────────────
    {
        "tag": "T4",
        "label": "Multi-hop reasoning — search → traverse → synthesize",
        "question": "找出知识图谱里和 'embedding' 最相关的 3 个笔记节点，然后分析这 3 个节点之间有什么共同概念或关联。",
        "pass_keywords": ["embedding", "节点", "相关"],
        "fail_keywords": ["无法执行", "工具错误", "没有找到任何"],
        "layer": "agent",
        "note": "Verifies multi-step tool chaining: search → explain → synthesize.",
    },

    # ── T5: read_note direct file access ─────────────────────────────────────
    {
        "tag": "T5",
        "label": "read_note — direct vault file read by path",
        "question": "帮我读一下 '实验/atomize-test-doc.md' 这个文件，告诉我它的 frontmatter 里有哪些字段。",
        "pass_keywords": ["frontmatter", "title", "tags"],
        "fail_keywords": ["路径不存在", "not found", "No such file", "FileNotFoundError", "无法读取"],
        "layer": "agent",
        "note": "Verifies Jei uses read_note (not search_knowledge) for direct file paths.",
    },

    # ── T6: Federation — cross-namespace hybrid search ────────────────────────
    {
        "tag": "T6",
        "label": "Federation — hybrid search hitting both nimbus and code namespaces",
        "question": "hybrid_search 的实现原理是什么？请从设计文档和代码实现两个角度来回答。",
        "pass_keywords": ["bm25", "embedding", "rrf"],
        "fail_keywords": ["无法找到", "没有相关信息", "搜索结果为空"],
        "layer": "agent",
        "note": "Verifies federated search hits both nimbus (design docs) and code namespaces.",
    },

    # ── T7: pending_edges review ──────────────────────────────────────────────
    {
        "tag": "T7",
        "label": "pending_edges — list unconfirmed edge proposals",
        "question": "知识图谱里有没有待确认的关系（pending edges）？列出前 5 条，并说明每条关系的 source 和 target。",
        "pass_keywords": ["edge", "source", "target"],
        "fail_keywords": ["工具不可用", "tool not found", "执行失败"],
        "layer": "agent",
        "note": "Verifies pending_edges tool is accessible and returns structured results.",
    },
]


# ── Stream capture ─────────────────────────────────────────────────────────────

_stream_buf: list[str] = []

def _stream_cb(text: str, is_thinking: bool = False) -> None:
    _stream_buf.append(text)
    print(text, end="", flush=True)


def _assess(response: str, stream: str, test: dict) -> tuple[str, list[str]]:
    combined = (response + "\n" + stream).lower()
    failed_pass = []
    triggered_fail = []

    for kw in test["pass_keywords"]:
        if kw.lower() not in combined:
            failed_pass.append(kw)

    for kw in test["fail_keywords"]:
        if kw.lower() in combined:
            triggered_fail.append(kw)

    if not response.strip():
        return "FAIL", ["[empty response]"]
    if triggered_fail:
        return "FAIL", [f"fail_kw={kw!r}" for kw in triggered_fail]
    if failed_pass:
        return "WARN", [f"missing={kw!r}" for kw in failed_pass]
    return "PASS", []


async def run_tests():
    loader = EntityLoader(BLUEPRINT_DIR, data_dir=DATA_DIR)
    await loader.start_mcp_servers()
    controller = await loader.get_controller()

    session_name = f"cap_test_{int(__import__('time').time())}"
    await controller.new_session(session_name)
    print(f"[smoke] fresh session: {session_name}", flush=True)

    try:
        graph = controller._graph
        if hasattr(graph, "nodes"):
            for node_id, node_fn in graph.nodes.items():
                if hasattr(node_fn, "set_stream_callback"):
                    node_fn.set_stream_callback(_stream_cb)
    except Exception as e:
        print(f"[warn] Could not attach stream callback: {e}")

    results = []

    for test in TESTS:
        global _stream_buf
        _stream_buf = []

        sep = "=" * 70
        tag   = test["tag"]
        label = test["label"]
        q     = test["question"]

        print(f"\n{sep}")
        print(f"TEST {tag}: {label}")
        print(f"Q: {q[:120]}{'...' if len(q) > 120 else ''}")
        print(f"NOTE: {test['note']}")
        print(f"{sep}")
        print("RESPONSE:")

        try:
            response = await asyncio.wait_for(
                controller.run(q),
                timeout=300,
            )
            stream = "".join(_stream_buf)
            final = response if len(response) >= len(stream) else stream
            if not final.strip():
                final = response or stream
            if not stream and final:
                print(final)
            response = final
        except asyncio.TimeoutError:
            print("\n*** TIMEOUT after 300s ***")
            response = ""
            stream   = ""
        except Exception as e:
            import traceback
            print(f"\n*** ERROR: {e} ***")
            traceback.print_exc()
            response = ""
            stream   = ""

        stream = "".join(_stream_buf)
        status, failures = _assess(response, stream, test)

        layer_label = f"[{test['layer'].upper()} LAYER]"
        print(f"\n--- END {tag} ---")
        verdict = (
            f"VERDICT: PASS {layer_label}" if status == "PASS"
            else f"VERDICT: WARN {layer_label}  → {failures}" if status == "WARN"
            else f"VERDICT: FAIL {layer_label}  → {failures}"
        )
        print(verdict)
        results.append((tag, status, failures, label))

    # ── Summary ──────────────────────────────────────────────────────────────
    sep = "=" * 70
    print(f"\n\n{sep}")
    print("CAPABILITY TEST SUMMARY")
    print(sep)
    pass_count = sum(1 for _, s, _, _ in results if s == "PASS")
    warn_count = sum(1 for _, s, _, _ in results if s == "WARN")
    fail_count = sum(1 for _, s, _, _ in results if s == "FAIL")

    for tag, status, failures, label in results:
        icon = "✅" if status == "PASS" else "⚠️" if status == "WARN" else "❌"
        short_label = label[:55] + "..." if len(label) > 55 else label
        reason_str = f"  → {failures}" if failures else ""
        print(f"  {icon} {tag:<6} {short_label:<58}{reason_str}")

    print(sep)
    if fail_count == 0 and warn_count == 0:
        print("RESULT: ALL PASS ✅")
    elif fail_count == 0:
        print(f"RESULT: ALL PASS ✅  ({warn_count} WARN — prompt-layer issues)")
    else:
        print(f"RESULT: {fail_count} FAIL, {warn_count} WARN")
        for tag, status, failures, _ in results:
            if status == "FAIL":
                print(f"  FAIL {tag}: {failures}")
            elif status == "WARN":
                guide = {
                    "T1": "Jei not querying code namespace; check cross-ns config",
                    "T2": "trace_path tool not called or path not found",
                    "T3": "list_communities not invoked or result empty",
                    "T4": "Multi-hop chain broken; check search→explain pipeline",
                    "T5": "read_note not used; Jei may have used search instead",
                    "T6": "Federation not triggering both namespaces",
                    "T7": "pending_edges tool error or no pending edges exist",
                }.get(tag, "check tool docstring / graph state")
                print(f"  WARN {tag}: {failures}  → {guide}")


def main():
    asyncio.run(run_tests())


if __name__ == "__main__":
    main()
