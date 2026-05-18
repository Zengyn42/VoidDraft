# test_e2e_colony_coder_game.py
"""E2E test: Colony Coder full chain (Planner → Executor → QA)
builds a CLI number guessing game.

Strategy:
  - Master graph uses external subgraph (native LangGraph subgraphs).
  - Mock at leaf node level: ClaudeSDKNode, GeminiCLINode, OllamaNode.
  - design_debate subgraph (debate_claude_first): ClaudeSDKNode + GeminiCLINode nodes
    are both intercepted by the catch-all mocks.
  - DETERMINISTIC nodes run for real (validators, run_tests, e2e_route, etc).
  - Real file I/O: game code + test scripts written by mocks.
"""

import json
import logging
import os
import stat
import sys
from pathlib import Path
from unittest.mock import patch

import pytest
from dotenv import load_dotenv

load_dotenv()

logging.basicConfig(level=logging.WARNING, stream=sys.stdout)

# ── Must import before any graph building to register "colony_coder_schema" ──
import functional_graphs.colony_coder.state  # noqa: F401

from framework.loader import EntityLoader
from framework.nodes.llm.claude import ClaudeSDKNode
from framework.nodes.llm.gemini import GeminiCLINode
from framework.nodes.llm.ollama import OllamaNode
from langchain_core.messages import AIMessage, HumanMessage

# ---------------------------------------------------------------------------
# Game source code that the "executor" will write
# ---------------------------------------------------------------------------

GAME_CODE = '''\
import random


def play_game():
    """Play one round of the guessing game."""
    number = random.randint(1, 100)
    attempts = 0

    while True:
        try:
            guess = int(input("Guess a number (1-100): "))
        except ValueError:
            print("Please enter a valid number.")
            continue

        attempts += 1

        if guess < number:
            print("Too low!")
        elif guess > number:
            print("Too high!")
        else:
            print(f"Correct! You guessed it in {attempts} attempts.")
            print(f"Score: {attempts}")
            return attempts


def main():
    """Main game loop with replay support."""
    print("Welcome to the Number Guessing Game!")

    while True:
        score = play_game()
        print(f"Your score: {score}")
        again = input("Play again? (y/n): ").strip().lower()
        if again != "y":
            print("Thanks for playing!")
            break


if __name__ == "__main__":
    main()
'''

# ---------------------------------------------------------------------------
# Task decomposition (Planner output)
# ---------------------------------------------------------------------------

TASKS = [
    {
        "id": "game_core",
        "description": "Create guess_game.py with random number generation, input loop, and hints",
        "dependencies": [],
    },
    {
        "id": "scoring",
        "description": "Add attempt counting and score display",
        "dependencies": ["game_core"],
    },
    {
        "id": "replay",
        "description": "Add play again functionality",
        "dependencies": ["scoring"],
    },
]

EXECUTION_ORDER = ["game_core", "scoring", "replay"]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _ai(text: str) -> AIMessage:
    return AIMessage(content=text)


def _base_return(state: dict, content: str, **extra) -> dict:
    """Build a minimal ClaudeSDKNode-style return dict."""
    result = {
        "messages": [_ai(content)],
        "routing_target": "",
        "routing_context": "",
        "rollback_reason": "",
        "retry_count": 0,
        "node_sessions": dict(state.get("node_sessions") or {}),
    }
    result.update(extra)
    return result


def _make_test_scripts(tmpdir: str) -> None:
    """Create test scripts that pass (unit tests + run_tests.sh)."""
    test_dir = Path(tmpdir) / "test_tool" / "unit_tests"
    test_dir.mkdir(parents=True, exist_ok=True)
    (test_dir / "test_basic.py").write_text(
        "def test_import():\n"
        "    import importlib.util\n"
        f"    spec = importlib.util.spec_from_file_location('game', '{tmpdir}/guess_game.py')\n"
        "    assert spec is not None\n"
    )
    runner = Path(tmpdir) / "test_tool" / "run_tests.sh"
    runner.write_text(
        "#!/bin/bash\nset -e\n"
        f"cd '{tmpdir}'\n"
        "python3 -m pytest test_tool/unit_tests/ -v 2>&1\n"
    )
    runner.chmod(runner.stat().st_mode | stat.S_IEXEC)


def _make_e2e_scripts(tmpdir: str) -> None:
    """Create E2E test scripts that pass."""
    e2e_dir = Path(tmpdir) / "test_tool" / "e2e_tests"
    e2e_dir.mkdir(parents=True, exist_ok=True)
    (e2e_dir / "test_game_e2e.py").write_text(
        "import os\n\n"
        "def test_game_file_exists():\n"
        f"    assert os.path.isfile('{tmpdir}/guess_game.py')\n\n"
        "def test_game_has_main():\n"
        f"    with open('{tmpdir}/guess_game.py') as f:\n"
        "        content = f.read()\n"
        "    assert 'def main' in content\n"
        "    assert 'play_game' in content\n"
    )
    runner = Path(tmpdir) / "test_tool" / "run_e2e.sh"
    runner.write_text(
        "#!/bin/bash\nset -e\n"
        f"cd '{tmpdir}'\n"
        "python3 -m pytest test_tool/e2e_tests/ -v 2>&1\n"
    )
    runner.chmod(runner.stat().st_mode | stat.S_IEXEC)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_e2e_colony_coder_game(tmp_path):
    """Full-chain E2E: master graph (external subgraph) drives Planner → Executor → QA.

    Mocking strategy (leaf-node level):
      - ClaudeSDKNode.__call__ → planner's claude_swarm, task_decompose; QA's generate_e2e;
                                  also debate subgraph's claude_propose/revise/conclusion
      - GeminiCLINode.__call__ → debate subgraph's gemini_critique_1/critique_2
      - OllamaNode.__call__   → executor's code_gen (writes game file + test scripts)
      - DETERMINISTIC nodes run for real (validators, run_tests, e2e_route, etc.)
    """
    tmpdir = str(tmp_path)

    # ── 1. ClaudeSDKNode mock (dispatched by node_id) ─────────────────────
    async def _mock_claude_call(self, state):
        node_id = self._node_id

        if node_id == "claude_swarm":
            return _base_return(
                state,
                "Multi-perspective review: approved. Proceed with implementation.",
            )

        elif node_id == "task_decompose":
            decomposition = {
                "tasks": TASKS,
                "execution_order": EXECUTION_ORDER,
                "refined_plan": "Build a CLI number guessing game",
                "working_directory": tmpdir,
                "e2e_plan": {
                    "acceptance_criteria": [
                        "Game generates random number 1-100",
                        "User gets higher/lower hints",
                        "Score displayed after correct guess",
                    ],
                    "test_scenarios": [
                        "Game file exists and is valid Python",
                        "Game has main() and play_game() functions",
                    ],
                    "run_command": f"python3 {tmpdir}/guess_game.py",
                    "headless_notes": "Pipe input via stdin",
                },
            }
            return _base_return(
                state,
                json.dumps(decomposition, indent=2),
                tasks=TASKS,
                execution_order=EXECUTION_ORDER,
                refined_plan=(
                    "Build a CLI number guessing game with random number "
                    "generation, input loop, hints, scoring, and replay."
                ),
                working_directory=tmpdir,
                e2e_plan=decomposition["e2e_plan"],
            )

        elif node_id == "generate_e2e":
            # QA: write E2E test scripts that will pass
            _make_e2e_scripts(tmpdir)
            return _base_return(state, "E2E_VERDICT: PASS\nAll acceptance criteria met.")

        elif node_id in ("qa_rescue", "integration_rescue"):
            return _base_return(state, "RESCUE_VERDICT: PASS")

        else:
            # Catch-all for any unexpected CLAUDE_SDK node
            return _base_return(state, f"Mock response for {node_id}")

    # ── 2. OllamaNode mock (executor's code_gen) ─────────────────────────
    async def _mock_ollama_call(self, state):
        # Write game source file
        game_path = Path(tmpdir) / "guess_game.py"
        game_path.write_text(GAME_CODE, encoding="utf-8")

        # Write unit test scripts that pass
        _make_test_scripts(tmpdir)

        return {
            "messages": [_ai(
                "All code written. guess_game.py created with play_game(), "
                "main(), random number generation, hints, scoring, and replay. "
                "Unit tests created and passing."
            )],
        }

    # ── 3. GeminiCLINode mock — debate subgraph's critique nodes ─────────
    async def _mock_gemini_call(self, state):
        node_id = self._node_id
        return {
            "messages": [_ai(f"Gemini critique for {node_id}: looks good, proceed.")],
            "routing_target": "",
            "routing_context": "",
        }

    # ── 4. Build & run master graph ──────────────────────────────────────
    with (
        patch.object(ClaudeSDKNode, "__call__", _mock_claude_call),
        patch.object(GeminiCLINode, "__call__", _mock_gemini_call),
        patch.object(OllamaNode, "__call__", _mock_ollama_call),
    ):
        loader = EntityLoader(Path(__file__).parent.parent / "functional_graphs/colony_coder")
        graph = await loader.build_graph(checkpointer=None)

        initial_state = {
            "messages": [HumanMessage(
                content="Build a CLI number guessing game called guess_game.py"
            )],
        }

        # Use astream(updates) to accumulate the full state.
        result: dict = {}
        async for event in graph.astream(initial_state, stream_mode="updates"):
            for node_id, update in event.items():
                if node_id not in ("__start__", "__end__"):
                    result.update(update)

    # ── 5. Assertions ────────────────────────────────────────────────────

    # Success flag
    assert result.get("success") is True, (
        f"Expected success=True, got {result.get('success')!r}. "
        f"abort_reason={result.get('abort_reason')!r}"
    )

    # File exists and has expected content
    game_path = Path(tmpdir) / "guess_game.py"
    assert game_path.exists(), f"guess_game.py not found in {tmpdir}"
    game_content = game_path.read_text(encoding="utf-8")
    assert "import random" in game_content
    assert "play_game" in game_content
    assert "def main" in game_content
    assert "Play again" in game_content or "play again" in game_content

    # No abort
    abort = result.get("abort_reason")
    assert not abort, f"Expected no abort_reason, got {abort!r}"


@pytest.mark.asyncio
async def test_planner_decomposition_validator_rejects_empty():
    """Planner validator rejects empty tasks and retries task_decompose."""
    from functional_graphs.colony_coder_planner.validators import (
        decomposition_validator,
    )

    # Empty tasks → retry
    r = decomposition_validator({"tasks": [], "execution_order": [], "retry_count": 0})
    assert r["routing_target"] == "task_decompose"
    assert r["retry_count"] == 1

    # After RETRY_CAP → abort
    r = decomposition_validator({"tasks": [], "execution_order": [], "retry_count": 2})
    assert r["routing_target"] == "__end__"
    assert r["success"] is False


@pytest.mark.asyncio
async def test_planner_decomposition_validator_accepts_valid():
    """Planner validator accepts valid decomposition."""
    from functional_graphs.colony_coder_planner.validators import (
        decomposition_validator,
    )

    r = decomposition_validator({
        "tasks": TASKS,
        "execution_order": EXECUTION_ORDER,
        "retry_count": 0,
    })
    assert r["routing_target"] == "__end__"


@pytest.mark.asyncio
async def test_executor_new_validators():
    """Executor validators: inject_task_context, test_route."""
    from functional_graphs.colony_coder_executor.validators import (
        inject_task_context, test_route,
    )
    ctx = inject_task_context({
        "refined_plan": "Build a game",
        "tasks": TASKS,
        "execution_order": EXECUTION_ORDER,
        "working_directory": "/tmp/test",
        "qa_analysis": "",
        "qa_fail_count": 0,
    })
    assert "messages" in ctx

    # Pass
    tr = test_route({"execution_returncode": 0, "retry_count": 0})
    assert tr["routing_target"] == "__end__"

    # Fail retry
    tr = test_route({
        "execution_returncode": 1, "retry_count": 2,
        "execution_stdout": "FAIL", "execution_stderr": "",
    })
    assert tr["routing_target"] == "code_gen"
    assert tr["retry_count"] == 3


@pytest.mark.asyncio
async def test_integrator_route_pass():
    """Integrator route: pass → __end__ with success."""
    from functional_graphs.colony_coder_integrator.validators import (
        integration_route,
    )

    r = integration_route({
        "validation_output": {"status": "pass"},
        "retry_count": 0,
    })
    assert r["routing_target"] == "__end__"
    assert r["success"] is True


@pytest.mark.asyncio
async def test_integrator_route_fail_retry():
    """Integrator route: fail → rescue then retry."""
    from functional_graphs.colony_coder_integrator.validators import (
        integration_route,
    )

    r = integration_route({
        "validation_output": {"status": "fail", "category": "test_failure"},
        "retry_count": 0,
    })
    assert r["routing_target"] == "integration_rescue"
    assert r["retry_count"] == 1


@pytest.mark.asyncio
async def test_game_file_is_syntactically_valid(tmp_path):
    """The game code we inject must be valid Python."""
    game_path = tmp_path / "guess_game.py"
    game_path.write_text(GAME_CODE, encoding="utf-8")
    compile(GAME_CODE, str(game_path), "exec")  # raises SyntaxError if invalid


@pytest.mark.asyncio
async def test_master_graph_node_wiring():
    """Master graph has plan → execute → qa wiring."""
    loader = EntityLoader(Path(__file__).parent.parent / "functional_graphs/colony_coder")
    graph = await loader.build_graph(checkpointer=None)
    assert set(graph.nodes) - {"__start__"} >= {"plan", "execute", "qa"}
