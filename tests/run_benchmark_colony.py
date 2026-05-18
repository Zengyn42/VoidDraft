#!/usr/bin/env python3
"""Run a benchmark task with ColonyCoder. Usage: python3 run_benchmark_colony.py <task_name>"""

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

import blueprints.functional_graphs.colony_coder.state  # noqa: F401

from benchmark_tasks import TASKS
from framework.loader import EntityLoader
from framework.debug import set_debug
from framework.debug_reporter import DebugConsoleReporter
from langchain_core.messages import HumanMessage


async def main():
    if len(sys.argv) != 2 or sys.argv[1] not in TASKS:
        print(f"Usage: {sys.argv[0]} <{'|'.join(TASKS.keys())}>")
        sys.exit(1)

    task_name = sys.argv[1]
    task_desc, working_dir, _ = TASKS[task_name]

    set_debug(True)
    Path(working_dir).mkdir(parents=True, exist_ok=True)

    loader = EntityLoader(Path("/home/kingy/Foundation/VoidDraft/functional_graphs/colony_coder"))
    graph = await loader.build_graph(checkpointer=None)
    reporter = DebugConsoleReporter(f"colony_{task_name}")

    print("=" * 70)
    print(f"  ColonyCoder — {task_name}")
    print("=" * 70, flush=True)

    init_state = {"messages": [HumanMessage(content=task_desc)]}
    async for ns, event in graph.astream(init_state, stream_mode="updates", subgraphs=True):
        reporter.on_event(ns, event)
    reporter.print_summary()


if __name__ == "__main__":
    asyncio.run(main())
