"""
H-series test runner for Jei — drives the graph via controller.run() directly.

Usage:
    cd /home/kingy/Foundation/ZenithLoom
    python3 run_jei_htest.py
"""
import asyncio
import sys
import os
import logging
from pathlib import Path

# ZenithLoom must be the cwd
framework_dir = Path("/home/kingy/Foundation/ZenithLoom")
os.chdir(framework_dir)
sys.path.insert(0, str(framework_dir))

# Reduce noise
logging.basicConfig(level=logging.WARNING)
logging.getLogger("httpx").setLevel(logging.ERROR)
logging.getLogger("httpcore").setLevel(logging.ERROR)

from framework.loader import EntityLoader


BLUEPRINT_DIR = Path("/home/kingy/Foundation/VoidDraft/role_agents/knowledge_curator")
DATA_DIR = Path("/home/kingy/Foundation/EdenGateway/agents/jei")

QUESTIONS = [
    ("H1", "read_note date field regression",
     "帮我读一下 设计细节/Obsidian Subgraph MCP Architecture Design.md 这个笔记的完整内容，包括 frontmatter"),

    ("H2", "namespace discovery without hardcoding",
     "你连接的知识图谱里有哪些命名空间？每个命名空间里索引的是什么内容？"),

    ("H3a", "multi-turn turn 1 — Leiden communities (anchor question)",
     "PrismRag 里的 Leiden 社区检测算法是如何工作的？有多少个社区？"),

    ("H3b", "multi-turn turn 2 — ClaudeSDKNode callers",
     "ZenithLoom 代码库里，ClaudeSDKNode 有哪些直接调用者？"),

    ("H3c", "multi-turn turn 3 — Six-Space Theory (KEY BUG3 TEST)",
     "帮我在 Vault 里找找有没有笔记是关于 '六空间理论' 或 'Six-Space Theory' 的"),

    ("H4", "highest-degree node graph reasoning",
     "在 nimbus（vault）命名空间里，找出 degree 最高的 5 个节点，然后告诉我其中 degree 最高的那个节点的所有出边和入边"),

    ("H5a", "CAS write new note",
     "帮我在 NimbusVault 里新建一个笔记 实验/六空间测试.md。内容：---\ntitle: 六空间理论测试\ndate: 2026-05-03\ntags: [实验, 六空间]\n---\n\n# 六空间理论\n\n这是一个测试笔记，用于验证 PrismRag 的写入功能。"),

    ("H5b", "CAS patch note",
     "现在给这个笔记加一个 ## 参考资料 段落，内容写：'- Wang Yanzhang, Six-Space Theory'"),
]

# ── stream capture ──────────────────────────────────────────────────────────

_stream_buf: list[str] = []

def _stream_cb(text: str, is_thinking: bool = False) -> None:
    _stream_buf.append(text)
    # print live so we can see progress
    print(text, end="", flush=True)


async def run_tests():
    loader = EntityLoader(BLUEPRINT_DIR, data_dir=DATA_DIR)

    # Start MCP servers (PrismRag) before building graph
    await loader.start_mcp_servers()

    controller = await loader.get_controller()

    # Attach stream callback to every LLM node
    try:
        graph = controller._graph
        # Walk nodes looking for set_stream_callback
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
            # If we captured stream, the controller.run() may return same text
            # Use whichever is longer
            final = response if len(response) >= len(streamed) else streamed
            if not final.strip():
                final = response or streamed

            # Print any remaining if stream didn't print it all
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
