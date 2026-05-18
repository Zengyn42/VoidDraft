#!/usr/bin/env python3
"""
Colony Coder 完整 E2E 测试 — 真实 LLM 调用。

流程：plan → execute → integrate
所有 LLM 节点使用真实 API 调用（Claude SDK + Gemini CLI）。
Debug 模式全开，输出完整 state snapshot。
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
from framework.debug import set_debug, push_graph_scope, pop_graph_scope
from langchain_core.messages import HumanMessage


async def main():
    set_debug(True)
    print("🔧 Colony Coder 完整 E2E 测试 — Debug ON\n", flush=True)

    loader = EntityLoader(Path(__file__).parent.parent / "functional_graphs/colony_coder")
    graph = await loader.build_graph(checkpointer=None)

    task = (
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
        "- 单文件实现，保存到 /tmp/snake_battle_v3/snake_battle.py\n"
        "- 代码结构清晰，两个 AI 分别是独立的类\n"
        "- 可直接 python3 snake_battle.py 运行\n"
    )

    init_state = {
        "messages": [HumanMessage(content=task)],
    }

    push_graph_scope("colony_coder_e2e")
    try:
        print("🚀 Starting colony_coder (plan → execute → integrate)\n")
        print("=" * 70, flush=True)

        last_state = {}
        async for event in graph.astream(init_state, stream_mode="updates"):
            for node_id, update in event.items():
                if node_id in ("__start__", "__end__"):
                    continue
                last_state.update(update)

                # 打印节点输出摘要
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

                # 关键 state 变化
                tasks = update.get("tasks")
                if tasks:
                    print(f"  📦 tasks: {len(tasks)} items", flush=True)
                    for t in tasks:
                        if isinstance(t, dict):
                            print(f"     {t.get('id','?')}: {str(t.get('description',''))[:80]}", flush=True)

                eo = update.get("execution_order")
                if eo:
                    print(f"  📋 execution_order: {eo}", flush=True)

                completed = update.get("completed_tasks")
                if completed:
                    print(f"  ✅ completed_tasks: {completed}", flush=True)

                files = update.get("final_files")
                if files:
                    print(f"  📁 final_files: {files}", flush=True)

                success = update.get("success")
                if success is not None:
                    print(f"  {'✅' if success else '❌'} success: {success}", flush=True)

                abort = update.get("abort_reason", "")
                if abort:
                    print(f"\n  🛑 ABORT: {abort}", flush=True)

        print("\n" + "=" * 70, flush=True)

        # ── 最终结果 ──
        print(f"\n📋 Final Results:", flush=True)
        print(f"   success:         {last_state.get('success')}", flush=True)
        print(f"   abort_reason:    {last_state.get('abort_reason') or '(none)'}", flush=True)
        print(f"   tasks:           {len(last_state.get('tasks', []))} items", flush=True)
        print(f"   completed_tasks: {last_state.get('completed_tasks', [])}", flush=True)
        print(f"   final_files:     {last_state.get('final_files', [])}", flush=True)

        # ── 检查生成的文件 ──
        working_dir = last_state.get("working_directory", "/tmp/snake_battle_v3")
        # Find any .py files (game might be snake_battle.py or similar)
        wd = Path(working_dir)
        py_files = list(wd.glob("*.py")) if wd.exists() else []
        snake_path = py_files[0] if py_files else wd / "snake_battle.py"
        if snake_path.exists():
            content = snake_path.read_text(encoding="utf-8")
            print(f"\n🐍 {snake_path.name} exists ({len(content)} chars, {len(content.splitlines())} lines)")
            # 语法检查
            try:
                compile(content, str(snake_path), "exec")
                print(f"   ✅ Syntax check: PASS", flush=True)
            except SyntaxError as e:
                print(f"   ❌ Syntax check: FAIL — {e}", flush=True)
        else:
            print(f"\n❌ snake.py not found at {snake_path}", flush=True)
            # 看看 working_dir 下有什么
            wd = Path(working_dir)
            if wd.exists():
                files = list(wd.rglob("*"))
                print(f"   Files in {working_dir}: {[str(f.relative_to(wd)) for f in files]}", flush=True)

    finally:
        pop_graph_scope()

    # ── Debug logs ──
    from datetime import datetime
    date = datetime.now().strftime("%Y-%m-%d")
    log_base = Path("logs") / date / "colony_coder_e2e"
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
