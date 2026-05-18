#!/usr/bin/env python3
"""
ColonyCoder debug runner — 用 DebugConsoleReporter 可视化完整执行过程。

用法: python3 run_colony_coder_debug.py
"""

import sys, os\nfrom pathlib import Path\n_ZL = Path(__file__).parent.parent.parent / "ZenithLoom"\nos.chdir(_ZL)\nsys.path.insert(0, str(_ZL))\n
import asyncio
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
from framework.debug import set_debug, set_debug_output_file
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
    "## 对战规则（AI 必须理解并利用的规则）\n"
    "- **主动攻击**：蛇可以主动用头部撞击对方身体，导致对方死亡（自己存活）\n"
    "- **头对头碰撞**：两蛇头部同时到达同一格，长度更长的蛇存活，短蛇死亡；等长则双亡\n"
    "- **空间控制**：蛇的身体是障碍物，可以通过走位封锁对方活动空间，逼对方撞墙/撞自己\n"
    "- **食物争抢**：食物数量有限，抢先吃到食物能增加长度优势\n"
    "- **尾部特性**：蛇每帧尾部缩一格（除非刚吃了食物），AI 可以利用这个时序追尾穿越\n"
    "\n"
    "## AI 设计（最重要的部分）\n"
    "你的核心目标是：**设计出尽可能强的 AI 策略，以在对战中取胜为唯一目标。**\n"
    "\n"
    "AI 应该具备以下高级能力（不限于此）：\n"
    "- **空间评估**：Voronoi 分区或 flood-fill 计算可达空间，避免进入死胡同\n"
    "- **攻击决策**：判断何时主动攻击（用身体封锁对方、制造头对头碰撞优势）\n"
    "- **多步前瞻**：不只看当前帧最优，而是预判 N 步后的局面\n"
    "- **状态机切换**：根据局势（长度优势/劣势、空间大小、对方位置）动态切换策略\n"
    "- **追击模式**：当己方长度占优时，主动追击对方蛇头，制造碰撞\n"
    "\n"
    "两个 AI（AI-Alpha 和 AI-Beta）必须使用不同的策略体系，但都要追求最大胜率。\n"
    "**不要因为实现复杂度而降低设计水平——尽全力设计最强的 AI。**\n"
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
    "- 单文件实现，保存到 /tmp/snake_battle_v3/snake_battle.py\n"
    "- 代码结构清晰，两个 AI 分别是独立的类\n"
    "- 可直接 python3 snake_battle.py 运行\n"
)


async def main():
    set_debug(True)
    from datetime import datetime
    debug_file = f"logs/{datetime.now().strftime('%Y-%m-%d')}/colony_coder_debug_output.md"
    set_debug_output_file(debug_file)
    print(f"  Debug output file: {debug_file}\n", flush=True)

    loader = EntityLoader(Path(__file__).parent.parent / "functional_graphs/colony_coder")
    graph = await loader.build_graph(checkpointer=None)

    reporter = DebugConsoleReporter("colony_coder")

    print("=" * 70)
    print("  ColonyCoder Debug Run — Snake Battle")
    print("=" * 70)
    print(flush=True)

    init_state = {"messages": [HumanMessage(content=SNAKE_TASK)]}

    async for namespace, event in graph.astream(
        init_state, stream_mode="updates", subgraphs=True
    ):
        reporter.on_event(namespace, event)

    reporter.print_summary()

    # Check generated files
    working_dir = reporter._last_state.get("working_directory", "/tmp/snake_battle_v3")
    wd = Path(working_dir)
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
        print(f"\n  No .py files found in {working_dir}")
        if wd.exists():
            all_files = list(wd.rglob("*"))
            print(f"  Files: {[str(f.relative_to(wd)) for f in all_files]}")


if __name__ == "__main__":
    asyncio.run(main())
