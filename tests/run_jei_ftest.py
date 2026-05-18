"""
F-series test runner for Jei — fresh session per question.

Each question gets its own EntityLoader + controller so there is zero
context carryover between questions. This isolates failures and avoids
the accumulated-context timeout seen when running all questions in one
long multi-turn session.

Usage:
    cd /home/kingy/Foundation/ZenithLoom
    python3 run_jei_ftest.py
"""
import asyncio
import sys
import os
import logging
import time
from pathlib import Path

framework_dir = Path("/home/kingy/Foundation/ZenithLoom")
os.chdir(framework_dir)
sys.path.insert(0, str(framework_dir))

logging.basicConfig(level=logging.WARNING)
logging.getLogger("httpx").setLevel(logging.ERROR)
logging.getLogger("httpcore").setLevel(logging.ERROR)

from framework.loader import EntityLoader


BLUEPRINT_DIR = Path("/home/kingy/Foundation/VoidDraft/role_agents/knowledge_curator")
DATA_DIR = Path("/home/kingy/Foundation/EdenGateway/agents/jei")

QUESTIONS = [
    # ── Code namespace search ─────────────────────────────────────────────
    ("F1", "code search — ClaudeSDKNode callers",
     "在 code 命名空间里搜索 ClaudeSDKNode，找到它的直接调用者（CALLS 边的来源节点），列出文件路径和函数名"),

    ("F2", "code search — LlmNode definition",
     "在 code 命名空间里，LlmNode 这个类定义在哪个文件？它有哪些子类？"),

    ("F3", "code search — SubgraphRefNode",
     "SubgraphRefNode 在 ZenithLoom 代码里是做什么用的？它的 __call__ 方法的逻辑是什么？"),

    # ── Graph stats (fresh session) ──────────────────────────────────────
    ("F4", "graph stats — namespace summary",
     "用 list_namespaces 工具列出所有命名空间，告诉我每个命名空间有多少节点和边"),

    ("F5", "graph stats — code communities",
     "code 命名空间有多少个 Leiden 社区？最大的社区包含哪些文件或模块？"),

    ("F6", "graph stats — nimbus top nodes",
     "nimbus 命名空间里 degree 最高的 10 个节点是什么？"),

    # ── Cross-namespace queries ───────────────────────────────────────────
    ("F7", "cross-ns — vault notes mentioning HeartbeatNode",
     "NimbusVault 里有哪些笔记提到了 HeartbeatNode？"),

    ("F8", "cross-ns — trace path from vault doc to code",
     "从 nimbus 命名空间的 PrismRag v5.0 设计文档，通过图的边，追踪到 code 命名空间里相关的代码节点"),

    # ── Semantic search ───────────────────────────────────────────────────
    ("F9", "semantic — knowledge graph traversal",
     "搜索跟 'BFS 图遍历' 或 '广度优先搜索' 相关的内容，重点找 PrismRag 代码里的实现"),

    ("F10", "semantic — session management",
     "搜索 ZenithLoom 代码里跟 'session 管理' 或 'checkpoint' 相关的节点和文件"),

    # ── Read / write ──────────────────────────────────────────────────────
    ("F11", "read note — frontmatter verification",
     "读一下 NimbusVault 里 knowledge/Obsidian多模态RAG系统架构设计.md 这个笔记，告诉我 frontmatter 里的 tags 和 created 字段"),

    ("F12", "write — new test note",
     "在 NimbusVault 的 实验/ 目录下新建笔记 f12测试.md，frontmatter: title: F12测试, date: 2026-05-07, tags: [测试]，正文只写一行：这是 F12 自动测试。"),

    ("F13", "read back — verify written note",
     "读一下 NimbusVault 里 实验/f12测试.md 这个笔记，把完整内容给我看"),

    ("F14", "write — patch existing note",
     "在 实验/f12测试.md 里追加一个段落：## 验证结果\n\n写入和读取功能正常。"),

    ("F15", "delete — cleanup test note",
     "把 实验/f12测试.md 这个测试笔记删掉"),

    # ── Multi-hop reasoning ───────────────────────────────────────────────
    ("F16", "multi-hop — OllamaNode callers chain",
     "在 code 命名空间里找到 OllamaNode，然后通过 CALLS 边往上追两跳，找出谁调用了调用 OllamaNode 的函数"),

    ("F17", "multi-hop — community membership",
     "在 code 命名空间里，framework/nodes/llm/claude.py 这个文件属于哪个 Leiden 社区？这个社区里还有哪些文件？"),

    # ── Knowledge quality ─────────────────────────────────────────────────
    ("F18", "quality — current PrismRag ingest pipeline",
     "根据 NimbusVault 里的设计文档，PrismRag 的 ingest pipeline 当前有哪几个 Pass？每个 Pass 做什么？"),

    ("F19", "quality — similarity edge design",
     "PrismRag 的 semantically_similar_to 边是在哪个 Pass 生成的？用的什么算法？threshold 是多少？"),

    ("F20", "quality — write_note CAS protocol",
     "PrismRag 的 write_note 工具用了什么写入协议？如果并发写同一个文件会发生什么？"),
]


_stream_buf: list[str] = []

def _stream_cb(text: str, is_thinking: bool = False) -> None:
    _stream_buf.append(text)
    print(text, end="", flush=True)


async def run_one(tag: str, label: str, question: str) -> dict:
    """Run a single question in a fresh Jei session."""
    global _stream_buf
    _stream_buf = []

    loader = EntityLoader(BLUEPRINT_DIR, data_dir=DATA_DIR)
    await loader.start_mcp_servers()
    controller = await loader.get_controller()

    # Attach stream callback
    try:
        graph = controller._graph
        if hasattr(graph, "nodes"):
            for node_id, node_fn in graph.nodes.items():
                if hasattr(node_fn, "set_stream_callback"):
                    node_fn.set_stream_callback(_stream_cb)
    except Exception:
        pass

    sep = "=" * 70
    print(f"\n{sep}")
    print(f"TEST {tag}: {label}")
    print(f"Q: {question[:120]}{'...' if len(question) > 120 else ''}")
    print(f"{sep}")
    print("RESPONSE:")

    try:
        t0 = time.time()
        response = await asyncio.wait_for(controller.run(question), timeout=300)
        elapsed = time.time() - t0
        streamed = "".join(_stream_buf)
        final = response if len(response) >= len(streamed) else streamed
        if not final.strip():
            final = response or streamed
        if not streamed and final:
            print(final)
        result = {"tag": tag, "label": label, "response": final, "error": None, "elapsed": round(elapsed, 1)}
    except asyncio.TimeoutError:
        print("\n*** TIMEOUT after 300s ***")
        result = {"tag": tag, "label": label, "response": "", "error": "TIMEOUT", "elapsed": 300}
    except Exception as e:
        import traceback
        print(f"\n*** ERROR: {e} ***")
        traceback.print_exc()
        result = {"tag": tag, "label": label, "response": "", "error": str(e), "elapsed": 0}

    print(f"\n--- END {tag} ({result.get('elapsed', '?')}s) ---\n")
    await loader.stop_mcp_servers()
    return result


async def run_tests():
    results = []
    for tag, label, question in QUESTIONS:
        r = await run_one(tag, label, question)
        results.append(r)
    return results


def main():
    results = asyncio.run(run_tests())

    print("\n\n" + "=" * 70)
    print("FINAL RESULTS SUMMARY")
    print("=" * 70)
    for r in results:
        status = f"ERROR({r['error']})" if r["error"] else f"OK ({len(r['response'])} chars, {r['elapsed']}s)"
        print(f"  {r['tag']:5s} {r['label'][:48]:50s} [{status}]")


if __name__ == "__main__":
    main()
