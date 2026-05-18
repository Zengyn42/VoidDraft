#!/usr/bin/env python3
"""
三个子图真实 LLM 调用测试：debate_brainstorm / debate_design / apex_coder

每个子图用简单 prompt 跑一轮完整流程，验证 permission_mode 和 schema 重构后一切正常。
"""

import sys, os\nfrom pathlib import Path\n_ZL = Path(__file__).parent.parent.parent / "ZenithLoom"\nos.chdir(_ZL)\nsys.path.insert(0, str(_ZL))\n
import asyncio
import logging
import sys
from pathlib import Path

from dotenv import load_dotenv
load_dotenv()

logging.basicConfig(
    level=logging.DEBUG,
    format="%(asctime)s [%(name)s] %(levelname)s %(message)s",
    stream=sys.stderr,
)

from framework.loader import EntityLoader
from framework.debug import set_debug, push_graph_scope, pop_graph_scope
from langchain_core.messages import HumanMessage


async def run_subgraph(name: str, agent_dir: str, prompt: str, init_extra: dict = None):
    """运行一个子图并打印结果。"""
    print(f"\n{'='*70}")
    print(f"🚀 {name}")
    print(f"{'='*70}\n", flush=True)

    loader = EntityLoader(Path(agent_dir))
    graph = await loader.build_graph(checkpointer=None)

    init_state = {
        "messages": [HumanMessage(content=prompt)],
        "routing_target": "",
        "routing_context": prompt,
        "node_sessions": {},
        "workspace": "",
        "project_root": "",
        "rollback_reason": "",
        "retry_count": 0,
    }
    if init_extra:
        init_state.update(init_extra)

    push_graph_scope(name)
    try:
        last_state = {}
        async for event in graph.astream(init_state, stream_mode="updates"):
            for node_id, update in event.items():
                if node_id in ("__start__", "__end__"):
                    continue
                last_state.update(update)

                msgs = update.get("messages", [])
                for msg in msgs:
                    content = getattr(msg, "content", "")
                    if not content:
                        continue
                    lines = content.split("\n")
                    print(f"\n┌─ [{node_id}]", flush=True)
                    for line in lines[:30]:
                        print(f"│ {line}", flush=True)
                    if len(lines) > 30:
                        print(f"│ ... ({len(content)} chars total)", flush=True)
                    print(f"└─", flush=True)

        print(f"\n✅ {name} 完成", flush=True)
        return True

    except Exception as e:
        print(f"\n❌ {name} 失败: {e}", flush=True)
        return False
    finally:
        pop_graph_scope()


async def main():
    set_debug(True)
    print("🔧 三子图真实 LLM E2E 测试\n", flush=True)

    results = {}

    # 1. debate_design (claude_first) — plan 模式
    results["debate_design"] = await run_subgraph(
        name="debate_design (claude_first)",
        agent_dir=str(Path(__file__).parent.parent / "functional_graphs/debate_claude_first"),
        prompt="设计一个 Python CLI 计算器，支持加减乘除和括号表达式。用什么架构最合适？",
    )

    # 2. debate_brainstorm (gemini_first) — plan 模式
    results["debate_brainstorm"] = await run_subgraph(
        name="debate_brainstorm (gemini_first)",
        agent_dir=str(Path(__file__).parent.parent / "functional_graphs/debate_gemini_first"),
        prompt="如何设计一个本地优先的 AI Agent 记忆系统？要支持跨 session 持久化和语义检索。",
    )

    # 3. apex_coder — bypassPermissions 模式
    results["apex_coder"] = await run_subgraph(
        name="apex_coder",
        agent_dir=str(Path(__file__).parent.parent / "functional_graphs/apex_coder"),
        prompt="在 /tmp/e2e_test/ 下创建一个 hello.py，内容是 print('hello from apex_coder')。然后用 python3 运行它，确认输出正确。",
    )

    # 汇总
    print(f"\n{'='*70}")
    print("📋 汇总")
    print(f"{'='*70}")
    for name, ok in results.items():
        print(f"  {'✅' if ok else '❌'} {name}")

    # 验证 apex_coder 是否真的写了文件
    hello_path = Path("/tmp/e2e_test/hello.py")
    if hello_path.exists():
        print(f"\n📁 /tmp/e2e_test/hello.py 存在 ✅")
        print(f"   内容: {hello_path.read_text().strip()}")
    else:
        print(f"\n📁 /tmp/e2e_test/hello.py 不存在 ❌")

    # 验证 debate 子图没有写文件
    from pathlib import Path as P
    if not P("/tmp/e2e_test/calculator.py").exists():
        print(f"📁 debate 未产生代码文件 ✅ (plan 模式生效)")
    else:
        print(f"📁 debate 产生了代码文件 ❌ (plan 模式失效!)")


if __name__ == "__main__":
    asyncio.run(main())
