#!/usr/bin/env python3
"""
Colony Coder vs ApexCoder — Snake AI Benchmark

1. Run Colony Coder to generate a new Snake AI
2. Import the generated AI
3. Benchmark it against ApexCoder's StrategicAI
4. Print comparison results

Usage: python3 run_colony_vs_apex.py
"""

import asyncio
import logging
import sys
import shutil
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

LOG_DIR = Path("/tmp/colony_debug")
LOG_DIR.mkdir(parents=True, exist_ok=True)

logging.basicConfig(
    level=logging.DEBUG,
    format="%(asctime)s [%(name)s] %(levelname)s %(message)s",
    handlers=[
        logging.FileHandler(LOG_DIR / "colony_coder.log", mode="w"),
        logging.StreamHandler(sys.stderr),
    ],
)

# Register state schema
import blueprints.functional_graphs.colony_coder.state  # noqa: F401

from framework.loader import EntityLoader
from framework.debug import set_debug
from framework.debug_reporter import DebugConsoleReporter
from langchain_core.messages import HumanMessage

SNAKE_BATTLE_DIR = Path("/home/kingy/Foundation/EdenGateway/CompanyTests/CoderTest/snake_battle")
WORKING_DIR = Path("/tmp/colony_snake_ai")

TASK = f"""\
Write a competitive Snake AI for a 2-player Snake Battle game.

## Game Rules
- 30x30 grid, two snakes, simultaneous moves each turn
- Directions: UP(0,-1), DOWN(0,1), LEFT(-1,0), RIGHT(1,0). Cannot reverse.
- Hit wall -> die. Head-to-head -> longer wins (equal -> both die).
- Head hits opponent body -> attacker dies (unless body-bite rules apply).
- Self-bite -> truncated, not dead.
- Eating food: +1 length, +1 score. New food spawns after consumption.
- Turn limit: 1000. If both alive, longer snake wins.
- AI decision time < 100ms.

## Interface You MUST Follow

Your AI must subclass this exact interface (already provided, do NOT redefine it):

```python
from abc import ABC, abstractmethod
from dataclasses import dataclass
from enum import Enum

class Direction(Enum):
    UP = (0, -1)
    DOWN = (0, 1)
    LEFT = (-1, 0)
    RIGHT = (1, 0)

@dataclass(frozen=True)
class Position:
    x: int
    y: int
    def move(self, direction: Direction) -> "Position":
        dx, dy = direction.value
        return Position(self.x + dx, self.y + dy)

@dataclass(frozen=True)
class SnakeView:
    positions: tuple[Position, ...]  # positions[0] is head
    direction: Direction
    score: int
    alive: bool
    name: str
    @property
    def head(self) -> Position: return self.positions[0]
    @property
    def length(self) -> int: return len(self.positions)

@dataclass(frozen=True)
class GameState:
    grid_width: int       # 30
    grid_height: int      # 30
    my_snake: SnakeView
    opponent_snake: SnakeView
    foods: tuple[Position, ...]
    turn: int

class BaseAI(ABC):
    @abstractmethod
    def decide(self, state: GameState) -> Direction:
        ...
```

## Your Task

Create a file `colony_ai.py` in the working directory that:

1. **Imports from ai_base**: `from ai_base import BaseAI, GameState, Direction, Position, SnakeView, _safe_directions, _manhattan`
2. **Defines class `ColonyCoderAI(BaseAI)`** with a `decide(self, state) -> Direction` method
3. The AI should be COMPETITIVE — try to beat strong opponents by combining:
   - Flood-fill or BFS for space control
   - Smart food targeting (prefer food in your territory)
   - Opponent avoidance and trapping strategies
   - Survival-first: never pick a move that leads to immediate death
4. Must run within 100ms per decision (no expensive searches)
5. Include unit tests that verify:
   - AI returns valid Direction values
   - AI avoids immediate wall collisions
   - AI avoids self-collision
   - AI prefers food directions when safe

## Working Directory

All files go in: `{WORKING_DIR}`

The game framework files (ai_base.py, game.py, config.py) will be available in the working directory for import.
"""


async def run_colony_coder():
    """Phase 1: Run Colony Coder to generate the AI."""
    set_debug(True)
    print("=" * 70)
    print("  Phase 1: Colony Coder — Generating Snake AI")
    print("=" * 70, flush=True)

    # Prepare working directory with game framework files
    WORKING_DIR.mkdir(parents=True, exist_ok=True)
    for src_file in ["ai_base.py", "game.py", "config.py"]:
        src = SNAKE_BATTLE_DIR / src_file
        if src.exists():
            shutil.copy2(src, WORKING_DIR / src_file)
            print(f"  Copied {src_file} to working dir", flush=True)

    loader = EntityLoader(Path("/home/kingy/Foundation/VoidDraft/functional_graphs/colony_coder"))
    graph = await loader.build_graph(checkpointer=None)

    reporter = DebugConsoleReporter("colony_coder", log_dir=LOG_DIR)

    init_state = {
        "messages": [HumanMessage(content=TASK)],
        "working_directory": str(WORKING_DIR),
    }

    last_state = {}
    async for namespace, event in graph.astream(
        init_state, stream_mode="updates", subgraphs=True
    ):
        reporter.on_event(namespace, event)

        # Also track top-level state
        if not namespace:
            for node_id, update in event.items():
                if node_id not in ("__start__", "__end__"):
                    last_state.update(update)

    reporter.print_summary()

    print(f"\n  Colony Coder finished: success={last_state.get('success')}", flush=True)
    print(f"  Debug logs: {LOG_DIR}/", flush=True)
    return last_state


def run_benchmark(rounds: int = 100):
    """Phase 2: Benchmark ColonyCoderAI vs StrategicAI (ApexCoder)."""
    print("\n" + "=" * 70)
    print("  Phase 2: Benchmark — ColonyCoderAI vs StrategicAI (ApexCoder)")
    print("=" * 70, flush=True)

    # Check if colony_ai.py was generated
    colony_ai_path = WORKING_DIR / "colony_ai.py"
    if not colony_ai_path.exists():
        print(f"  ❌ colony_ai.py not found at {colony_ai_path}")
        # Try to find any AI file
        py_files = list(WORKING_DIR.glob("*.py"))
        print(f"  Files in working dir: {[f.name for f in py_files]}")
        return

    # Syntax check
    content = colony_ai_path.read_text()
    print(f"  colony_ai.py: {len(content)} chars, {len(content.splitlines())} lines")
    try:
        compile(content, str(colony_ai_path), "exec")
        print("  ✅ Syntax check: PASS", flush=True)
    except SyntaxError as e:
        print(f"  ❌ Syntax check: FAIL — {e}")
        return

    # Copy to snake_battle dir for import
    dst = SNAKE_BATTLE_DIR / "colony_ai.py"
    shutil.copy2(colony_ai_path, dst)
    print(f"  Copied to {dst}", flush=True)

    # Import and register
    sys.path.insert(0, str(SNAKE_BATTLE_DIR))
    try:
        from ai_base import AI_REGISTRY
        from colony_ai import ColonyCoderAI
        AI_REGISTRY["ColonyCoderAI"] = ColonyCoderAI
        print(f"  ✅ ColonyCoderAI imported and registered", flush=True)
    except Exception as e:
        print(f"  ❌ Import failed: {e}")
        return

    # Run benchmark: ColonyCoderAI vs StrategicAI
    from config import GameConfig
    from game import GameEngine
    from ai_base import get_ai

    results = {"colony_wins": 0, "apex_wins": 0, "draws": 0,
               "colony_score": 0, "apex_score": 0, "total_turns": 0}

    print(f"\n  Running {rounds} rounds...", flush=True)
    for i in range(rounds):
        # Alternate who goes first (Blue/Red position)
        if i % 2 == 0:
            ai1, ai2 = ColonyCoderAI(), get_ai("StrategicAI")
            ai1_is_colony = True
        else:
            ai1, ai2 = get_ai("StrategicAI"), ColonyCoderAI()
            ai1_is_colony = False

        config = GameConfig(grid_width=30, grid_height=30, max_turns=1000)
        engine = GameEngine(config, ai1, ai2)

        # Run game to completion
        while engine.tick():
            pass

        colony_score = engine.snake1.score if ai1_is_colony else engine.snake2.score
        apex_score = engine.snake2.score if ai1_is_colony else engine.snake1.score
        results["colony_score"] += colony_score
        results["apex_score"] += apex_score
        results["total_turns"] += engine.turn

        winner = engine.winner
        if winner is None:
            results["draws"] += 1
        elif ("(Blue)" in winner and ai1_is_colony) or ("(Red)" in winner and not ai1_is_colony):
            results["colony_wins"] += 1
        else:
            results["apex_wins"] += 1

        if (i + 1) % 10 == 0:
            print(f"  [{i+1}/{rounds}] Colony {results['colony_wins']}W "
                  f"Apex {results['apex_wins']}W "
                  f"Draw {results['draws']}", flush=True)

    # Final results
    print("\n" + "=" * 70)
    print("  FINAL RESULTS")
    print("=" * 70)
    print(f"  ColonyCoderAI wins: {results['colony_wins']} ({results['colony_wins']/rounds*100:.1f}%)")
    print(f"  StrategicAI wins:   {results['apex_wins']} ({results['apex_wins']/rounds*100:.1f}%)")
    print(f"  Draws:              {results['draws']} ({results['draws']/rounds*100:.1f}%)")
    print(f"  Colony avg score:   {results['colony_score']/rounds:.1f}")
    print(f"  Apex avg score:     {results['apex_score']/rounds:.1f}")
    print(f"  Avg turns:          {results['total_turns']/rounds:.0f}")
    print("=" * 70, flush=True)


async def main():
    # Phase 1: Generate AI
    state = await run_colony_coder()

    # Phase 2: Benchmark
    if state.get("success"):
        run_benchmark(rounds=100)
    else:
        print(f"\n  ❌ Colony Coder failed: {state.get('abort_reason', 'unknown')}")
        print("  Skipping benchmark.", flush=True)

        # Still check if any file was generated
        colony_ai_path = WORKING_DIR / "colony_ai.py"
        if colony_ai_path.exists():
            print("  (colony_ai.py exists despite failure — attempting benchmark anyway)")
            run_benchmark(rounds=100)


if __name__ == "__main__":
    asyncio.run(main())
