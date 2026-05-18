#!/usr/bin/env python3
"""
单独测试 colony_coder_planner 子图。

验证：
  1. plan → design_debate → claude_swarm → task_decompose 共享 session
  2. task_decompose 输出合法 JSON（tasks + execution_order）
  3. decomposition_validator 正确路由
  4. Token guard 不误杀
"""

import sys, os\nfrom pathlib import Path\n_ZL = Path("/home/kingy/Foundation/ZenithLoom")\nos.chdir(_ZL)\nsys.path.insert(0, str(_ZL))\n
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

# 注册 state schema
import functional_graphs.colony_coder.state  # noqa: F401

from framework.loader import EntityLoader
from framework.debug import set_debug, push_graph_scope, pop_graph_scope
from langchain_core.messages import HumanMessage


async def main():
    set_debug(True)
    print("🔧 Planner 独立测试 — Debug ON\n", flush=True)

    loader = EntityLoader(Path("/home/kingy/Foundation/VoidDraft/functional_graphs/colony_coder_planner"))
    graph = await loader.build_graph(checkpointer=None)

    task = (
        "用 Python 写一个终端贪吃蛇游戏（Snake Game）。\n"
        "要求：\n"
        "1. 使用 curses 库实现终端 UI\n"
        "2. 蛇可以上下左右移动，吃到食物变长\n"
        "3. 撞墙或撞自己 Game Over，显示分数\n"
        "4. 单文件实现，保存到 /tmp/colony_game/snake.py\n"
        "5. 代码要有注释，可直接 python3 snake.py 运行"
    )

    init_state = {
        "messages": [HumanMessage(content=task)],
        "tasks": [],
        "execution_order": [],
        "refined_plan": "",
        "working_directory": "/tmp/colony_game",
        "completed_tasks": [],
        "final_files": [],
        "success": False,
        "abort_reason": "",
        "node_sessions": {},
        "ollama_sessions": {},
        "current_task_index": 0,
        "current_task_id": "",
        "retry_count": 0,
        "transient_retry_count": 0,
        "error_history": [],
        "cross_task_issues": [],
        "validation_output": None,
        "rescue_scope": "",
        "rescue_rationale": "",
        "affected_task_ids": [],
    }

    push_graph_scope("colony_coder_planner_test")
    try:
        print("🚀 Starting colony_coder_planner (Claude SDK × 4 nodes)\n")
        print("=" * 70, flush=True)

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
                    for line in lines[:80]:
                        print(f"│ {line}", flush=True)
                    if len(lines) > 80:
                        print(f"│ ... ({len(content)} chars total)", flush=True)
                    print(f"└─", flush=True)

                rt = update.get("routing_target")
                if rt:
                    print(f"  ⤳ routing_target = {rt!r}", flush=True)

                abort = update.get("abort_reason", "")
                if abort:
                    print(f"\n🛑 ABORT: {abort}", flush=True)

        print("\n" + "=" * 70, flush=True)

        # 检查结果
        tasks = last_state.get("tasks", [])
        execution_order = last_state.get("execution_order", [])
        refined_plan = last_state.get("refined_plan", "")
        abort = last_state.get("abort_reason", "")
        success = last_state.get("success", None)

        print(f"\n📋 Results:", flush=True)
        print(f"   tasks:           {len(tasks)} items", flush=True)
        print(f"   execution_order: {execution_order}", flush=True)
        print(f"   refined_plan:    {refined_plan[:200] if refined_plan else '(empty)'}", flush=True)
        print(f"   abort_reason:    {abort or '(none)'}", flush=True)
        print(f"   success:         {success}", flush=True)

        if tasks:
            print(f"\n📦 Task details:", flush=True)
            for t in tasks:
                print(f"   {t.get('id', '?')}: {t.get('description', '?')[:100]}", flush=True)
                print(f"      deps: {t.get('dependencies', [])}", flush=True)

        # 验证
        if tasks and execution_order:
            task_ids = {t["id"] for t in tasks if isinstance(t, dict) and "id" in t}
            all_valid = all(oid in task_ids for oid in execution_order)
            print(f"\n{'✅' if all_valid else '❌'} Validation: task IDs match execution_order = {all_valid}", flush=True)

            # Dump planner output for executor standalone testing
            import json
            snapshot = {
                "tasks": tasks,
                "execution_order": execution_order,
                "refined_plan": refined_plan,
                "working_directory": last_state.get("working_directory", "/tmp/colony_game"),
                "messages": [
                    {"type": getattr(m, "type", "ai"), "content": getattr(m, "content", "")}
                    for m in last_state.get("messages", [])
                ],
            }
            snapshot_path = Path("planner_output.json")
            snapshot_path.write_text(json.dumps(snapshot, ensure_ascii=False, indent=2), encoding="utf-8")
            print(f"\n💾 Planner 快照已保存: {snapshot_path}", flush=True)
            print(f"   可用 python3 run_executor_only.py 单独测试 executor", flush=True)

        elif abort:
            print(f"\n❌ Planner aborted: {abort}", flush=True)
        else:
            print(f"\n❌ No tasks or execution_order produced", flush=True)

    finally:
        pop_graph_scope()

    # Log files
    from datetime import datetime
    date = datetime.now().strftime("%Y-%m-%d")
    log_base = Path("logs") / date / "colony_coder_planner_test"

    print(f"\n📁 Debug logs: {log_base}/", flush=True)
    if log_base.exists():
        for p in sorted(log_base.rglob("*.md")):
            rel = p.relative_to(log_base)
            size = p.stat().st_size
            print(f"   {rel} ({size:,} bytes)", flush=True)
    else:
        print("   (no logs generated)", flush=True)


if __name__ == "__main__":
    asyncio.run(main())
