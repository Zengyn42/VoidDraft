import pytest
import tempfile
from pathlib import Path


def _write_validators(tmp_dir: str, content: str):
    Path(tmp_dir, "validators.py").write_text(content)


@pytest.mark.asyncio
async def test_deterministic_node_calls_function():
    with tempfile.TemporaryDirectory() as tmp:
        _write_validators(tmp, "def my_node(state): return {'result': state['x'] + 1}")
        from framework.nodes.deterministic_node import DeterministicNode
        node = DeterministicNode(config={}, node_config={"id": "my_node", "agent_dir": tmp})
        result = await node({"x": 5})
    assert result == {"result": 6}


@pytest.mark.asyncio
async def test_deterministic_node_missing_fn_raises():
    with tempfile.TemporaryDirectory() as tmp:
        _write_validators(tmp, "def other_node(state): return {}")
        from framework.nodes.deterministic_node import DeterministicNode
        with pytest.raises(AttributeError):
            DeterministicNode(config={}, node_config={"id": "missing", "agent_dir": tmp})


def test_deterministic_registered():
    import framework.builtins
    from framework.registry import get_node_factory
    factory = get_node_factory("DETERMINISTIC")
    assert factory is not None


@pytest.mark.asyncio
async def test_code_execution_success():
    from framework.nodes.external_tool_node import ExternalToolNode
    node = ExternalToolNode(
        config={},
        node_config={"id": "execute", "backend": "code_execution", "timeout": 10},
    )
    result = await node({"execution_command": "echo hello", "working_directory": ""})
    assert result["execution_stdout"].strip() == "hello"
    assert result["execution_returncode"] == 0


@pytest.mark.asyncio
async def test_code_execution_nonzero_exit():
    from framework.nodes.external_tool_node import ExternalToolNode
    node = ExternalToolNode(
        config={},
        node_config={"id": "execute", "backend": "code_execution", "timeout": 10},
    )
    result = await node({"execution_command": "false", "working_directory": ""})
    assert result["execution_returncode"] != 0


def test_tool_registry_has_all_tools():
    from framework.nodes.llm.tools import TOOL_REGISTRY, TOOL_SCHEMAS
    expected = {"read_file", "write_file", "replace_lines", "bash_exec", "list_dir", "submit_validation"}
    assert set(TOOL_REGISTRY.keys()) == expected
    assert set(TOOL_SCHEMAS.keys()) == expected


def test_build_tool_schemas_subset():
    from framework.nodes.llm.tools import build_tool_schemas
    schemas = build_tool_schemas(["read_file", "submit_validation"])
    assert len(schemas) == 2
    names = {s["function"]["name"] for s in schemas}
    assert names == {"read_file", "submit_validation"}


def test_submit_validation_has_required_fields():
    from framework.nodes.llm.tools import TOOL_SCHEMAS
    props = TOOL_SCHEMAS["submit_validation"]["function"]["parameters"]["properties"]
    for f in ("status", "category", "severity", "rationale"):
        assert f in props, f"submit_validation missing field: {f}"


from unittest.mock import AsyncMock, patch


@pytest.mark.asyncio
async def test_ollama_no_tools_uses_base_path():
    """When no tools configured, __call__ is the base class path (no _call_with_tools)."""
    from framework.nodes.llm.ollama import OllamaNode
    node = OllamaNode(config={}, node_config={"id": "code_gen", "model": "qwen3.5:27b"})
    assert node._tools == []


@pytest.mark.asyncio
async def test_ollama_tool_loop_terminates_on_submit_validation():
    """Tool loop ends when submit_validation (_terminal=True) is returned."""
    import json as _json
    from framework.nodes.llm.ollama import OllamaNode

    tool_call_response = {
        "role": "assistant",
        "content": "",
        "tool_calls": [{"id": "call_0", "type": "function", "function": {
            "name": "submit_validation",
            "arguments": _json.dumps({
                "status": "pass",
                "category": "correctness",
                "severity": "low",
                "rationale": "looks good",
            }),
        }}],
    }

    node = OllamaNode(
        config={},
        node_config={"id": "soft_validate", "model": "qwen3.5:27b", "tools": ["submit_validation"]},
    )
    with patch.object(node, "_chat_completions", new_callable=AsyncMock) as mock_chat:
        mock_chat.return_value = tool_call_response
        result = await node._call_with_tools({
            "messages": [{"role": "user", "content": "validate this code"}],
            "node_sessions": {},
            "ollama_sessions": {},
        })

    assert result["validation_output"]["status"] == "pass"
    assert "ollama_sessions" in result


@pytest.mark.asyncio
async def test_ollama_tool_loop_text_response():
    """Tool loop ends on text response with no tool_calls."""
    from framework.nodes.llm.ollama import OllamaNode

    text_response = {
        "role": "assistant",
        "content": "here is the code",
    }

    node = OllamaNode(
        config={},
        node_config={"id": "code_gen", "model": "qwen3.5:27b", "tools": ["read_file", "write_file"]},
    )
    with patch.object(node, "_chat_completions", new_callable=AsyncMock) as mock_chat:
        mock_chat.return_value = text_response
        result = await node._call_with_tools({
            "messages": [{"role": "user", "content": "write hello.py"}],
            "node_sessions": {},
            "ollama_sessions": {},
        })

    assert "messages" in result or "ollama_sessions" in result


def test_executor_state_has_required_fields():
    from blueprints.functional_graphs.colony_coder.state import ColonyCoderState
    import typing
    hints = typing.get_type_hints(ColonyCoderState, include_extras=True)
    for f in ("tasks", "ollama_sessions", "validation_output", "success", "abort_reason"):
        assert f in hints, f"ColonyCoderState missing field: {f}"


def test_merge_dict_reducer():
    from blueprints.functional_graphs.colony_coder.state import _merge_dict
    a = {"k1": [1, 2], "k2": [3]}
    b = {"k2": [4], "k3": [5]}
    assert _merge_dict(a, b) == {"k1": [1, 2], "k2": [4], "k3": [5]}


def test_colony_coder_schema_registered():
    from framework.loader.graph_builder import _get_state_schemas
    # Importing state.py should auto-register the schema
    import blueprints.functional_graphs.colony_coder.state  # noqa: F401
    schemas = _get_state_schemas()
    assert "base_schema" in schemas, f"base_schema missing, got: {list(schemas.keys())}"
    assert "debate_schema" in schemas, f"debate_schema missing, got: {list(schemas.keys())}"
    assert "colony_coder_schema" in schemas, f"colony_coder_schema missing, got: {list(schemas.keys())}"


def test_decomposition_validator_pass():
    from blueprints.functional_graphs.colony_coder_planner.validators import decomposition_validator
    result = decomposition_validator({
        "tasks": [{"id": "t1", "description": "write hello.py", "dependencies": []}],
        "execution_order": ["t1"],
        "retry_count": 0,
    })
    assert result["routing_target"] == "__end__"


def test_decomposition_validator_fail_retry():
    from blueprints.functional_graphs.colony_coder_planner.validators import decomposition_validator
    result = decomposition_validator({
        "tasks": [],
        "execution_order": [],
        "retry_count": 1,
    })
    assert result["routing_target"] == "task_decompose"
    assert result["retry_count"] == 2


def test_decomposition_validator_abort_at_cap():
    from blueprints.functional_graphs.colony_coder_planner.validators import decomposition_validator
    result = decomposition_validator({
        "tasks": [],
        "execution_order": [],
        "retry_count": 2,
    })
    assert result["routing_target"] == "__end__"
    assert result["success"] is False


@pytest.mark.asyncio
async def test_planner_graph_compiles():
    import blueprints.functional_graphs.colony_coder.state  # noqa: F401
    from framework.loader import EntityLoader
    from pathlib import Path
    g = await EntityLoader(Path("/home/kingy/Foundation/VoidDraft/functional_graphs/colony_coder_planner")).build_graph(checkpointer=None)
    node_ids = set(g.nodes) - {"__start__"}
    required = {"design_debate", "claude_swarm", "task_decompose", "decomposition_validator"}
    assert required <= node_ids, f"Missing nodes: {required - node_ids}"


def test_inject_task_context_basic():
    from blueprints.functional_graphs.colony_coder_executor.validators import inject_task_context
    result = inject_task_context({
        "refined_plan": "Build a game",
        "tasks": [{"id": "t1", "description": "write code", "dependencies": []}],
        "execution_order": ["t1"],
        "working_directory": "/tmp/test",
        "qa_analysis": "",
        "qa_fail_count": 0,
    })
    assert "messages" in result
    assert result["retry_count"] == 0
    content = result["messages"][0].content
    assert "Build a game" in content
    assert "t1" in content


def test_inject_task_context_with_qa_feedback():
    from blueprints.functional_graphs.colony_coder_executor.validators import inject_task_context
    result = inject_task_context({
        "refined_plan": "Build a game",
        "tasks": [],
        "execution_order": [],
        "working_directory": "/tmp/test",
        "qa_analysis": "E2E tests failed: function not found",
        "qa_fail_count": 1,
    })
    content = result["messages"][0].content
    assert "QA Feedback" in content
    assert "E2E tests failed" in content


def test_run_tests_missing_runner():
    from blueprints.functional_graphs.colony_coder_executor.validators import run_tests
    result = run_tests({"working_directory": "/tmp/nonexistent_dir_xyz"})
    assert result["execution_returncode"] == 1
    assert "not found" in result["execution_stderr"]


def test_test_route_pass():
    from blueprints.functional_graphs.colony_coder_executor.validators import test_route
    result = test_route({"execution_returncode": 0, "retry_count": 0})
    assert result["routing_target"] == "__end__"


def test_test_route_fail_retry():
    from blueprints.functional_graphs.colony_coder_executor.validators import test_route
    result = test_route({
        "execution_returncode": 1, "retry_count": 0,
        "execution_stdout": "FAILED test_foo", "execution_stderr": "",
    })
    assert result["routing_target"] == "code_gen"
    assert result["retry_count"] == 1


def test_test_route_exhausted():
    from blueprints.functional_graphs.colony_coder_executor.validators import test_route
    result = test_route({"execution_returncode": 1, "retry_count": 5})
    assert result["routing_target"] == "__end__"


@pytest.mark.asyncio
async def test_executor_graph_compiles():
    import blueprints.functional_graphs.colony_coder.state  # noqa: F401
    from framework.loader import EntityLoader
    from pathlib import Path
    g = await EntityLoader(Path("/home/kingy/Foundation/VoidDraft/functional_graphs/colony_coder_executor")).build_graph(checkpointer=None)
    node_ids = set(g.nodes) - {"__start__"}
    required = {"inject_task_context", "code_gen", "run_tests", "test_route"}
    assert required <= node_ids, f"Missing: {required - node_ids}"


def test_integration_route_pass():
    from blueprints.functional_graphs.colony_coder_integrator.validators import integration_route
    result = integration_route({"validation_output": {"status": "pass"}, "retry_count": 0})
    assert result["routing_target"] == "__end__"
    assert result["success"] is True


def test_integration_route_fail_rescue():
    from blueprints.functional_graphs.colony_coder_integrator.validators import integration_route
    result = integration_route({"validation_output": {"status": "fail"}, "retry_count": 0})
    assert result["routing_target"] == "integration_rescue"
    assert result["retry_count"] == 1


def test_integration_route_abort_at_cap():
    from blueprints.functional_graphs.colony_coder_integrator.validators import integration_route
    result = integration_route({"validation_output": {"status": "fail"}, "retry_count": 2})
    assert result["routing_target"] == "__end__"
    assert result["success"] is False


@pytest.mark.asyncio
async def test_integrator_graph_compiles():
    import blueprints.functional_graphs.colony_coder.state  # noqa: F401
    from framework.loader import EntityLoader
    from pathlib import Path
    g = await EntityLoader(Path("/home/kingy/Foundation/VoidDraft/functional_graphs/colony_coder_integrator")).build_graph(checkpointer=None)
    node_ids = set(g.nodes) - {"__start__"}
    required = {"integration_test", "integration_rescue", "integration_route"}
    assert required <= node_ids, f"Missing: {required - node_ids}"


@pytest.mark.asyncio
async def test_master_graph_compiles():
    # Must import state.py first to register "colony_coder_schema"
    import blueprints.functional_graphs.colony_coder.state  # noqa: F401
    from framework.loader import EntityLoader
    from pathlib import Path
    g = await EntityLoader(Path("/home/kingy/Foundation/VoidDraft/functional_graphs/colony_coder")).build_graph(checkpointer=None)
    node_ids = set(g.nodes) - {"__start__"}
    assert {"plan", "execute", "qa"} <= node_ids
