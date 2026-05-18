#!/usr/bin/env python3
"""
直接测试 executor 子图 — 跳过 planner，用固定的 task/plan 数据。
用于快速测试不同 LLM 模型的代码生成能力。
"""

import sys, os\nfrom pathlib import Path\n_ZL = Path(__file__).parent.parent.parent / "ZenithLoom"\nos.chdir(_ZL)\nsys.path.insert(0, str(_ZL))\n
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

# 注册 state schema
import functional_graphs.colony_coder.state  # noqa: F401

from framework.loader import EntityLoader
from framework.debug import set_debug
from framework.debug_reporter import DebugConsoleReporter
from langchain_core.messages import HumanMessage

# ── 从 V1 成功运行提取的 planner 输出 ──
TASK = {
    "id": "t1",
    "description": (
        "Create /tmp/snake_battle_test/snake_battle.py — a complete terminal AI-vs-AI "
        "snake battle game using Python curses.\n\n"
        "Requirements:\n"
        "1. Direction enum (UP/DOWN/LEFT/RIGHT)\n"
        "2. Snake class (deque body, direction, alive, grow_pending)\n"
        "3. AIAlpha (Survivor): A* to nearest food + flood-fill tiebreak\n"
        "4. AIBeta (Aggressor): opponent-aware food scoring + interception\n"
        "5. Game class: simultaneous move resolution, head-to-head = both die\n"
        "6. Curses UI: colored snakes (green/red), yellow food, white border\n"
        "7. Status bar: snake lengths + frame counter\n"
        "8. ~10 FPS, press Q to quit\n"
        "9. 5 food items maintained on board\n"
        "10. Game ends when a snake dies, show winner\n\n"
        "Single file, ~400 lines, Python stdlib only."
    ),
    "dependencies": [],
}

WORKING_DIR = "/tmp/snake_battle_test"

REFINED_PLAN = (
    "Single-file terminal snake battle game. Two AI snakes: "
    "Alpha (A* + flood-fill survivor), Beta (opponent-aware aggressor). "
    "Simultaneous collision resolution. Curses UI with colors."
)

E2E_PLAN = {
    "acceptance_criteria": [
        "Game launches without errors",
        "Two colored snakes move autonomously",
        "Food appears and is consumed",
        "Snakes grow after eating",
        "Game ends on collision with winner display",
        "Q exits cleanly",
    ],
    "test_scenarios": [
        "Launch → snakes visible, food visible, frame counter incrementing",
        "Run 10s → snakes eat food, grow",
        "Wait for death → game over with winner",
        "Press Q → clean exit code 0",
    ],
    "run_command": f"python3 {WORKING_DIR}/snake_battle.py",
    "headless_notes": "Use pty module. Each test < 5 seconds. Send Q quickly.",
}


async def main():
    set_debug(True)
    Path(WORKING_DIR).mkdir(parents=True, exist_ok=True)

    # 只构建 executor 子图
    loader = EntityLoader(Path(__file__).parent.parent / "functional_graphs/colony_coder_executor")
    graph = await loader.build_graph(checkpointer=None)

    reporter = DebugConsoleReporter("executor_test")

    model = loader.json["graph"]["nodes"][1].get("model", "unknown")
    print("=" * 70)
    print(f"  Executor Test — model: {model}")
    print("=" * 70)
    print(flush=True)

    init_state = {
        "messages": [HumanMessage(content="Build the snake battle game.")],
        "tasks": [TASK],
        "execution_order": ["t1"],
        "refined_plan": REFINED_PLAN,
        "e2e_plan": E2E_PLAN,
        "working_directory": WORKING_DIR,
        "current_task_index": 0,
        "current_task_id": "",
        "retry_count": 0,
        "transient_retry_count": 0,
        "error_history": [],
        "completed_tasks": [],
        "cross_task_issues": [],
        "qa_analysis": "",
        "qa_fail_count": 0,
        "node_sessions": {},
        "ollama_sessions": {},
        "execution_command": "",
        "execution_stdout": "",
        "execution_stderr": "",
        "execution_returncode": None,
        "validation_output": None,
        "final_files": [],
        "success": False,
        "abort_reason": "",
    }

    async for namespace, event in graph.astream(
        init_state, stream_mode="updates", subgraphs=True
    ):
        reporter.on_event(namespace, event)

    reporter.print_summary()

    # Check output
    wd = Path(WORKING_DIR)
    py_files = list(wd.glob("*.py")) if wd.exists() else []
    if py_files:
        for pf in py_files:
            content = pf.read_text(encoding="utf-8")
            print(f"\n  {pf.name}: {len(content)} chars, {len(content.splitlines())} lines")
            try:
                compile(content, str(pf), "exec")
                print(f"  Syntax check: PASS")
            except SyntaxError as e:
                print(f"  Syntax check: FAIL — {e}")
    else:
        print(f"\n  No .py files in {WORKING_DIR}")


if __name__ == "__main__":
    asyncio.run(main())
