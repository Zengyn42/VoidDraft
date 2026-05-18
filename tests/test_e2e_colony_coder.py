# test_e2e_colony_coder.py
"""E2E tests for Colony Coder with mocked LLM backends."""

import asyncio
import json
import logging
import sys
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest
from dotenv import load_dotenv

load_dotenv()

logging.basicConfig(level=logging.WARNING, stream=sys.stdout)


def _ollama_text(content: str) -> dict:
    return {"role": "assistant", "content": content}


def _ollama_tool_call(name: str, args: dict) -> dict:
    return {"role": "assistant", "content": "", "tool_calls": [
        {"id": "call_0", "type": "function", "function": {"name": name, "arguments": json.dumps(args)}},
    ]}


@pytest.mark.asyncio
async def test_decomposition_validator_flow():
    """Planner validator routing: valid state → __end__ (exits planner subgraph)."""
    from functional_graphs.colony_coder_planner.validators import decomposition_validator
    result = decomposition_validator({
        "tasks": [{"id": "t1", "description": "hello", "dependencies": []}],
        "execution_order": ["t1"],
        "retry_count": 0,
    })
    assert result["routing_target"] == "__end__"


@pytest.mark.asyncio
async def test_executor_inject_and_route_flow():
    """Executor: inject_task_context builds prompt, test_route routes pass/fail."""
    from functional_graphs.colony_coder_executor.validators import (
        inject_task_context, test_route,
    )
    ctx = inject_task_context({
        "refined_plan": "Build something",
        "tasks": [{"id": "t1", "description": "do it", "dependencies": []}],
        "execution_order": ["t1"],
        "working_directory": "/tmp/test",
        "qa_analysis": "",
        "qa_fail_count": 0,
    })
    assert "messages" in ctx
    assert ctx["retry_count"] == 0

    # Pass path
    tr = test_route({"execution_returncode": 0, "retry_count": 0})
    assert tr["routing_target"] == "__end__"

    # Fail path
    tr = test_route({
        "execution_returncode": 1, "retry_count": 0,
        "execution_stdout": "ERROR", "execution_stderr": "",
    })
    assert tr["routing_target"] == "code_gen"
    assert tr["retry_count"] == 1


@pytest.mark.asyncio
async def test_planner_graph_compiles_e2e():
    import functional_graphs.colony_coder.state  # noqa: F401
    from framework.loader import EntityLoader
    g = await EntityLoader(Path(__file__).parent.parent / "functional_graphs/colony_coder_planner").build_graph(checkpointer=None)
    assert set(g.nodes) - {"__start__"} >= {
        "design_debate", "claude_swarm", "task_decompose", "decomposition_validator",
    }


@pytest.mark.asyncio
async def test_executor_graph_compiles_e2e():
    import functional_graphs.colony_coder.state  # noqa: F401
    from framework.loader import EntityLoader
    g = await EntityLoader(Path(__file__).parent.parent / "functional_graphs/colony_coder_executor").build_graph(checkpointer=None)
    assert set(g.nodes) - {"__start__"} >= {
        "inject_task_context", "code_gen", "run_tests", "test_route",
    }


@pytest.mark.asyncio
async def test_integrator_graph_compiles_e2e():
    import functional_graphs.colony_coder.state  # noqa: F401
    from framework.loader import EntityLoader
    g = await EntityLoader(Path(__file__).parent.parent / "functional_graphs/colony_coder_integrator").build_graph(checkpointer=None)
    assert set(g.nodes) - {"__start__"} >= {
        "integration_test", "integration_rescue", "integration_route",
    }


@pytest.mark.asyncio
async def test_master_graph_compiles_e2e():
    import functional_graphs.colony_coder.state  # noqa: F401
    from framework.loader import EntityLoader
    g = await EntityLoader(Path(__file__).parent.parent / "functional_graphs/colony_coder").build_graph(checkpointer=None)
    assert set(g.nodes) - {"__start__"} >= {"plan", "execute", "qa"}
