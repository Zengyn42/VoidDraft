"""
E-series extended test runner for Jei — deeper v5.2 validation.

Focuses on:
- Code namespace direct search (diagnose H3b)
- Cross-namespace link traversal (nimbus ↔ code)
- Semantic similarity search
- List / filter operations
- Multi-hop graph traversal
- Graph statistics

Usage:
    cd /path/to/VoidDraft/tests
    python3 run_jei_etest.py
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
DATA_DIR = Path(__file__).parent.parent.parent / "EdenGateway/agents/jei"

QUESTIONS = [
    # ── Code namespace direct search ──────────────────────────────────────
    ("E1", "code namespace — explicit symbol search",
     "在 code 命名空间里搜索 'ClaudeSDKNode'，列出所有匹配的节点，包括它们的文件路径和类型"),

    ("E2", "code namespace — callers via graph traversal",
     "在 code 命名空间里，找到 ClaudeSDKNode 这个类，然后通过图的边找出所有调用它或者引用它的模块和函数"),

    # ── Cross-namespace link traversal ────────────────────────────────────
    ("E3", "cross-namespace — vault docs referencing SubgraphRefNode",
     "NimbusVault 里有哪些笔记提到了 SubgraphRefNode？把这些笔记列出来，并说明每篇笔记里是在什么上下文下提到它的"),

    ("E4", "cross-namespace — vault + code dual query",
     "找出 NimbusVault 里所有提到 LangGraph 的笔记，同时告诉我 ZenithLoom 代码里哪些模块或文件跟 LangGraph 有直接关系"),

    # ── Semantic similarity search ────────────────────────────────────────
    ("E5", "semantic search — async task scheduling",
     "帮我搜一搜跟 '异步任务调度' 或 '消息队列' 相关的内容，包括 vault 笔记和代码"),

    ("E6", "semantic search — embedding pipeline",
     "搜索跟 '向量嵌入' 或 'embedding pipeline' 相关的内容，重点找 PrismRag 的实现细节"),

    # ── List / filter operations ──────────────────────────────────────────
    ("E7", "list notes — by tag filter",
     "列出 vault 里所有包含 'Architecture' 或 '架构' 标签的笔记，按文件名排序"),

    ("E8", "list notes — by category",
     "vault 里 '设计细节' 这个 category 下面有哪些笔记？"),

    # ── Graph statistics ──────────────────────────────────────────────────
    ("E9", "graph stats — code namespace",
     "code 命名空间里一共有多少个节点？有多少个 Leiden 社区？告诉我最大的那个社区里包含哪些文件"),

    ("E10", "graph stats — nimbus communities",
     "nimbus 命名空间里有多少个 Leiden 社区？每个社区的主题是什么？"),

    # ── Multi-hop traversal ───────────────────────────────────────────────
    ("E11", "multi-hop — GeminiCodeAssistNode location + siblings",
     "GeminiCodeAssistNode 定义在 ZenithLoom 代码库的哪个文件里？这个文件里还定义了哪些其他的类或函数？"),

    ("E12", "multi-hop — OllamaNode inheritance chain",
     "在代码图里找到 OllamaNode，告诉我它的父类是什么，以及有哪些子类或别名继承自它"),

    # ── Write + cleanup ───────────────────────────────────────────────────
    ("E13", "write new note — test cleanup",
     "帮我把 实验/六空间测试.md 这个笔记删掉，它是之前 H5a 测试创建的"),
]

# ── stream capture ──────────────────────────────────────────────────────────

_stream_buf: list[str] = []

def _stream_cb(text: str, is_thinking: bool = False) -> None:
    _stream_buf.append(text)
    print(text, end="", flush=True)


async def run_tests():
    loader = EntityLoader(BLUEPRINT_DIR, data_dir=DATA_DIR)
    await loader.start_mcp_servers()
    controller = await loader.get_controller()

    try:
        graph = controller._graph
        if hasattr(graph, "nodes"):
            for node_id, node_fn in graph.nodes.items():
                if hasattr(node_fn, "set_stream_callback"):
                    node_fn.set_stream_callback(_stream_cb)
    except Exception as e:
        print(f"[warn] Could not attach stream callback: {e}")

    results = []

    for tag, label, question in QUESTIONS:
        global _stream_buf
        _stream_buf = []

        sep = "=" * 70
        print(f"\n{sep}")
        print(f"TEST {tag}: {label}")
        print(f"Q: {question[:120]}{'...' if len(question) > 120 else ''}")
        print(f"{sep}")
        print("RESPONSE:")

        try:
            response = await asyncio.wait_for(
                controller.run(question),
                timeout=300
            )
            streamed = "".join(_stream_buf)
            final = response if len(response) >= len(streamed) else streamed
            if not final.strip():
                final = response or streamed
            if not streamed and final:
                print(final)

            results.append({
                "tag": tag,
                "label": label,
                "response": final,
                "error": None,
            })
        except asyncio.TimeoutError:
            print("\n*** TIMEOUT after 300s ***")
            results.append({"tag": tag, "label": label, "response": "", "error": "TIMEOUT"})
        except Exception as e:
            import traceback
            print(f"\n*** ERROR: {e} ***")
            traceback.print_exc()
            results.append({"tag": tag, "label": label, "response": "", "error": str(e)})

        print(f"\n--- END {tag} ---\n")

    await loader.stop_mcp_servers()
    return results


def main():
    results = asyncio.run(run_tests())

    print("\n\n" + "=" * 70)
    print("FINAL RESULTS SUMMARY")
    print("=" * 70)
    for r in results:
        status = f"ERROR({r['error']})" if r["error"] else f"OK ({len(r['response'])} chars)"
        print(f"  {r['tag']:5s} {r['label'][:50]:52s} [{status}]")


if __name__ == "__main__":
    main()
