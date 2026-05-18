#!/usr/bin/env python3
"""
ApexCoder TDD debug runner — 用 DebugConsoleReporter 可视化 ApexCoder 执行过程。

ApexCoder TDD pipeline: splitter → ClaudeQA → reset_for_coder → ClaudeCoder

用法: python3 run_apex_coder_debug.py
"""

import sys, os\nfrom pathlib import Path\n_ZL = Path("/home/kingy/Foundation/ZenithLoom")\nos.chdir(_ZL)\nsys.path.insert(0, str(_ZL))\n
import asyncio
import logging
import sys
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

import functional_graphs.apex_coder.state  # noqa: F401

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s %(message)s",
    stream=sys.stderr,
)

from framework.loader import EntityLoader
from framework.debug import set_debug
from framework.debug_reporter import DebugConsoleReporter
from langchain_core.messages import HumanMessage

SNAKE_TASK = (
    "用 Python 写一个双蛇对战游戏（Snake Battle）。\n"
    "\n"
    "## 核心要求\n"
    "1. 使用 curses 库实现终端 UI\n"
    "2. 两条蛇同时出现在棋盘上，全部由 AI 控制（无人类玩家），玩家只是观战者\n"
    "3. 屏幕上同时存在多个食物，蛇吃到食物后身体变长\n"
    "4. 蛇撞墙、撞自己、或者撞对方身体则死亡\n"
    "5. 最后存活的蛇获胜；如果都活着则比长度\n"
    "\n"
    "## AI 设计\n"
    "你需要自己设计两个不同策略的 AI（AI-Alpha 和 AI-Beta），让它们各控制一条蛇。\n"
    "AI 的目标：尽量吃食物让自己变长，同时尽量消灭对方。\n"
    "两个 AI 必须使用不同的策略，让对战有趣。\n"
    "\n"
    "## UI 要求\n"
    "- 顶部状态栏显示双方信息和当前帧数\n"
    "- 游戏区域有边框\n"
    "- 两条蛇用不同颜色区分\n"
    "- 游戏结束显示获胜者\n"
    "- 按 Q 退出\n"
    "- 帧率默认 ~10 FPS\n"
    "\n"
    "## 技术要求\n"
    "- 单文件实现，保存到 /tmp/snake_battle_apex/snake_battle.py\n"
    "- 代码结构清晰，两个 AI 分别是独立的类\n"
    "- 可直接 python3 snake_battle.py 运行\n"
    "- 必须能在标准 24x80 终端下正常运行\n"
    "\n"
    "## 工作目录: /tmp/snake_battle_apex\n"
)


async def main():
    set_debug(True)

    loader = EntityLoader(Path("/home/kingy/Foundation/VoidDraft/functional_graphs/apex_coder"))
    graph = await loader.build_graph(checkpointer=None)

    reporter = DebugConsoleReporter("apex_coder")

    print("=" * 70)
    print("  ApexCoder TDD Debug Run — Snake Battle")
    print("=" * 70)
    print(flush=True)

    Path("/tmp/snake_battle_apex").mkdir(parents=True, exist_ok=True)

    init_state = {"messages": [HumanMessage(content=SNAKE_TASK)]}

    async for namespace, event in graph.astream(
        init_state, stream_mode="updates", subgraphs=True
    ):
        reporter.on_event(namespace, event)

    reporter.print_summary()

    # Check generated files
    wd = Path("/tmp/snake_battle_apex")
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
        print(f"\n  No .py files found in /tmp/snake_battle_apex")
        if wd.exists():
            all_files = list(wd.rglob("*"))
            print(f"  Files: {[str(f.relative_to(wd)) for f in all_files]}")


if __name__ == "__main__":
    asyncio.run(main())
