#!/usr/bin/env python3
"""
单独测试 colony_coder_executor，跳过 planner。

用法：
  1. 先跑一次完整流程（或 run_planner_test.py），生成 planner_output.json
  2. python3 run_executor_only.py                     # 从 planner_output.json 加载
  3. python3 run_executor_only.py my_snapshot.json     # 指定快照文件

planner_output.json 格式：
  {
    "tasks": [...],
    "execution_order": [...],
    "refined_plan": "...",
    "working_directory": "/tmp/colony_game",
    "messages": [{"type": "human", "content": "..."}]
  }
"""

import asyncio
import json
import logging
import sys
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s %(message)s",
    stream=sys.stderr,
)

import blueprints.functional_graphs.colony_coder.state  # noqa: F401

from framework.loader import EntityLoader
from framework.debug import set_debug, push_graph_scope, pop_graph_scope
from langchain_core.messages import HumanMessage, AIMessage


def _load_snapshot(path: str) -> dict:
    """Load planner output snapshot from JSON."""
    with open(path, "r", encoding="utf-8") as f:
        raw = json.load(f)

    # Reconstruct LangChain messages from serialized form
    messages = []
    for m in raw.get("messages", []):
        if isinstance(m, dict):
            content = m.get("content", "")
            if m.get("type") == "human":
                messages.append(HumanMessage(content=content))
            else:
                messages.append(AIMessage(content=content))
        else:
            messages.append(m)

    state = {
        "messages": messages,
        "tasks": raw["tasks"],
        "execution_order": raw["execution_order"],
        "refined_plan": raw.get("refined_plan", ""),
        "working_directory": raw.get("working_directory", "/tmp/colony_game"),
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
        "execution_command": "",
        "execution_stdout": "",
        "execution_stderr": "",
        "execution_returncode": None,
        "test_files": [],
    }
    return state


async def main():
    set_debug(True)

    # 确定快照文件路径
    snapshot_path = sys.argv[1] if len(sys.argv) > 1 else "planner_output.json"
    if not Path(snapshot_path).exists():
        print(f"❌ 快照文件不存在: {snapshot_path}", flush=True)
        print(f"   先跑 run_planner_test.py 或完整流程生成它", flush=True)
        sys.exit(1)

    print(f"🔧 Executor 独立测试 — 从 {snapshot_path} 加载\n", flush=True)

    init_state = _load_snapshot(snapshot_path)
    print(f"📦 tasks: {len(init_state['tasks'])} items", flush=True)
    for t in init_state["tasks"]:
        print(f"   {t.get('id','?')}: {str(t.get('description',''))[:80]}", flush=True)
    print(f"📋 execution_order: {init_state['execution_order']}", flush=True)
    print(f"📁 working_directory: {init_state['working_directory']}\n", flush=True)

    # 确保 working_directory 存在
    Path(init_state["working_directory"]).mkdir(parents=True, exist_ok=True)

    loader = EntityLoader(Path("/home/kingy/Foundation/VoidDraft/functional_graphs/colony_coder_executor"))
    graph = await loader.build_graph(checkpointer=None)

    push_graph_scope("executor_standalone")
    try:
        print("🚀 Starting colony_coder_executor\n")
        print("=" * 70, flush=True)

        last_state = {}
        async for event in graph.astream(init_state, stream_mode="updates"):
            for node_id, update in event.items():
                if node_id in ("__start__", "__end__"):
                    continue
                if not update:
                    continue
                last_state.update(update)

                msgs = update.get("messages", [])
                for msg in msgs:
                    content = getattr(msg, "content", "")
                    if not content:
                        continue
                    lines = content.split("\n")
                    print(f"\n┌─ [{node_id}]", flush=True)
                    for line in lines[:40]:
                        print(f"│ {line}", flush=True)
                    if len(lines) > 40:
                        print(f"│ ... ({len(content)} chars total)", flush=True)
                    print(f"└─", flush=True)

                vo = update.get("validation_output")
                if vo:
                    print(f"  🔍 validation: {vo}", flush=True)

                rt = update.get("routing_target")
                if rt:
                    print(f"  ⤳ routing_target = {rt!r}", flush=True)

                success = update.get("success")
                if success is not None:
                    print(f"  {'✅' if success else '❌'} success={success}", flush=True)

                abort = update.get("abort_reason", "")
                if abort:
                    print(f"  🛑 ABORT: {abort}", flush=True)

        print("\n" + "=" * 70, flush=True)
        print(f"\n📋 Executor Results:", flush=True)
        print(f"   success:      {last_state.get('success')}", flush=True)
        print(f"   abort_reason: {last_state.get('abort_reason') or '(none)'}", flush=True)
        print(f"   final_files:  {last_state.get('final_files', [])}", flush=True)

        # 检查生成的文件
        wd = Path(init_state["working_directory"])
        if wd.exists():
            files = list(wd.rglob("*.py"))
            for f in files:
                content = f.read_text(encoding="utf-8")
                print(f"\n📄 {f.name} ({len(content.splitlines())} lines)", flush=True)
                try:
                    compile(content, str(f), "exec")
                    print(f"   ✅ Syntax OK", flush=True)
                except SyntaxError as e:
                    print(f"   ❌ Syntax Error: {e}", flush=True)

    finally:
        pop_graph_scope()


if __name__ == "__main__":
    asyncio.run(main())
