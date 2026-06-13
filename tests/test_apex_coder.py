import pytest
from pathlib import Path
from langchain_core.messages import HumanMessage, AIMessage
import tempfile


def test_apex_coder_schema_registered():
    import functional_graphs.apex_coder.state  # noqa: F401
    from framework.registry import get_all_schemas
    schemas = get_all_schemas()
    assert "apex_coder_schema" in schemas


def test_apex_coder_schema_has_required_fields():
    import typing
    from functional_graphs.apex_coder.state import ApexCoderState
    hints = typing.get_type_hints(ApexCoderState, include_extras=True)
    for field in ("user_requirements", "working_directory", "qa_bypass",
                  "qa_tests_dir", "run_qa_script", "qa_summary", "apex_conclusion"):
        assert field in hints, f"ApexCoderState missing field: {field}"


def test_setup_text_input():
    from functional_graphs.apex_coder.validators import setup
    result = setup({
        "messages": [HumanMessage(content="Build a snake game\n\n## 工作目录: /tmp/test_splitter_apex")]
    })
    assert result["user_requirements"] == "Build a snake game\n\n## 工作目录: /tmp/test_splitter_apex"
    assert result["working_directory"] == "/tmp/test_splitter_apex"
    # No background context → only 1 task message
    assert len(result["messages"]) == 1
    # Task message contains the requirements
    assert "Build a snake game" in result["messages"][0].content
    assert "QA Task" in result["messages"][0].content


def test_setup_file_input():
    from functional_graphs.apex_coder.validators import setup
    with tempfile.NamedTemporaryFile(mode="w", suffix=".md", delete=False) as f:
        f.write("Build a todo app")
        f.flush()
        result = setup({"messages": [HumanMessage(content=f.name)]})
    assert result["user_requirements"] == "Build a todo app"
    assert result["working_directory"].startswith("/tmp/apex_")


def test_setup_auto_generates_working_dir():
    from functional_graphs.apex_coder.validators import setup
    result = setup({"messages": [HumanMessage(content="Build something")]})
    assert result["working_directory"].startswith("/tmp/apex_")
    assert Path(result["working_directory"]).exists()
    assert Path(result["working_directory"], "test_tool", "qa_tests").exists()


def test_setup_creates_directories():
    from functional_graphs.apex_coder.validators import setup
    result = setup({
        "messages": [HumanMessage(content="Task\n\n## 工作目录: /tmp/test_splitter_dirs")]
    })
    assert Path("/tmp/test_splitter_dirs/test_tool/qa_tests").is_dir()


def test_reset_for_coder_clears_qa_messages():
    from functional_graphs.apex_coder.validators import reset_for_coder
    with tempfile.TemporaryDirectory() as wd:
        script = Path(wd, "test_tool", "run_qa.sh")
        script.parent.mkdir(parents=True)
        script.write_text("#!/bin/bash\nexit 0\n")
        result = reset_for_coder({
            "messages": [
                HumanMessage(content="user task", id="h1"),
                AIMessage(content="QA reasoning blah blah", id="a1"),
            ],
            "user_requirements": "user task",
            "working_directory": wd,
            "qa_summary": "wrote 5 tests + runner",
        })
    msgs = result["messages"]
    human_msgs = [m for m in msgs if isinstance(m, HumanMessage)]
    assert len(human_msgs) == 1
    assert "user task" in human_msgs[0].content
    assert "run_qa.sh" in human_msgs[0].content
    # Bugfix 2026-06-11: reset_for_coder must write parsed QA fields to state
    assert result["qa_bypass"] is False
    assert result["run_qa_script"] == str(script)


def test_reset_for_coder_bypass_mode():
    from functional_graphs.apex_coder.validators import reset_for_coder
    result = reset_for_coder({
        "messages": [HumanMessage(content="task", id="h1")],
        "user_requirements": "simple task",
        "working_directory": "/tmp/test_bypass_nonexistent",
        "qa_summary": "QA_BYPASS: 纯文档改动，无可测行为",
    })
    human_msgs = [m for m in result["messages"] if isinstance(m, HumanMessage)]
    assert "BYPASSED" in human_msgs[0].content
    assert result["qa_bypass"] is True
    assert result["run_qa_script"] == ""


# --- parse_qa_output (bugfix 2026-06-11: QA output was never parsed into state,
#     so executor always saw run_qa_script="" → guaranteed FAIL → retry storm) ---

def test_parse_qa_output_bypass_marker():
    from functional_graphs.apex_coder.validators import parse_qa_output
    out = parse_qa_output({
        "qa_summary": "分析完成。\nQA_BYPASS: 纯重构无行为变化",
        "working_directory": "/tmp/nonexistent_apex",
    })
    assert out == {"qa_bypass": True, "run_qa_script": ""}


def test_parse_qa_output_bypass_marker_fullwidth_colon():
    from functional_graphs.apex_coder.validators import parse_qa_output
    out = parse_qa_output({
        "qa_summary": "QA_BYPASS： 配置改动",
        "working_directory": "/tmp/nonexistent_apex",
    })
    assert out["qa_bypass"] is True


def test_parse_qa_output_runner_script_found():
    from functional_graphs.apex_coder.validators import parse_qa_output
    with tempfile.TemporaryDirectory() as wd:
        script = Path(wd, "test_tool", "run_qa.sh")
        script.parent.mkdir(parents=True)
        script.write_text("#!/bin/bash\nexit 0\n")
        out = parse_qa_output({
            "qa_summary": "wrote tests and runner",
            "working_directory": wd,
        })
    assert out == {"qa_bypass": False, "run_qa_script": str(script)}


def test_parse_qa_output_contract_violation_defaults_to_bypass():
    """QA neither bypassed nor wrote run_qa.sh → must NOT cause a guaranteed-FAIL
    retry loop; defaults to bypass with a warning."""
    from functional_graphs.apex_coder.validators import parse_qa_output
    out = parse_qa_output({
        "qa_summary": "did some analysis but forgot the runner",
        "working_directory": "/tmp/nonexistent_apex",
    })
    assert out == {"qa_bypass": True, "run_qa_script": ""}


import subprocess
import json


def test_hook_blocks_qa_test_write():
    hook_path = str(Path(__file__).parent.parent / "functional_graphs/apex_coder/hooks/protect_qa_tests.py")
    data = {"tool_input": {"file_path": "/tmp/game/test_tool/qa_tests/test_foo.py"}}
    result = subprocess.run(
        ["python3", hook_path],
        input=json.dumps(data),
        capture_output=True,
        text=True,
    )
    output = json.loads(result.stdout)
    assert output["decision"] == "block"


def test_hook_allows_source_write():
    hook_path = str(Path(__file__).parent.parent / "functional_graphs/apex_coder/hooks/protect_qa_tests.py")
    data = {"tool_input": {"file_path": "/tmp/game/main.py"}}
    result = subprocess.run(
        ["python3", hook_path],
        input=json.dumps(data),
        capture_output=True,
        text=True,
    )
    output = json.loads(result.stdout)
    assert output["decision"] == "allow"


def test_hook_allows_unit_test_write():
    hook_path = str(Path(__file__).parent.parent / "functional_graphs/apex_coder/hooks/protect_qa_tests.py")
    data = {"tool_input": {"file_path": "/tmp/game/test_tool/unit_tests/test_main.py"}}
    result = subprocess.run(
        ["python3", hook_path],
        input=json.dumps(data),
        capture_output=True,
        text=True,
    )
    output = json.loads(result.stdout)
    assert output["decision"] == "allow"


@pytest.mark.asyncio
async def test_apex_coder_graph_compiles():
    import functional_graphs.apex_coder.state  # noqa: F401
    from framework.loader import EntityLoader
    g = await EntityLoader(Path(__file__).parent.parent / "functional_graphs/apex_coder").build_graph(checkpointer=None)
    node_ids = set(g.nodes) - {"__start__", "__end__"}
    required = {"setup", "claude_qa", "reset_for_coder", "claude_coder", "executor", "route", "inject_error_context"}
    assert required <= node_ids, f"Missing nodes: {required - node_ids}, got: {node_ids}"


def test_state_has_executor_fields():
    import typing
    from functional_graphs.apex_coder.state import ApexCoderState
    hints = typing.get_type_hints(ApexCoderState, include_extras=True)
    for field in ("execution_stdout", "execution_stderr", "execution_returncode",
                  "iteration_history", "status"):
        assert field in hints, f"ApexCoderState missing field: {field}"


def test_executor_bypass():
    from functional_graphs.apex_coder.validators import executor
    result = executor({"qa_bypass": True, "working_directory": "/tmp", "run_qa_script": ""})
    assert result["status"] == "PASS"
    assert result["execution_returncode"] == 0


def test_executor_missing_script():
    from functional_graphs.apex_coder.validators import executor
    result = executor({
        "qa_bypass": False,
        "working_directory": "/tmp",
        "run_qa_script": "/tmp/nonexistent_script.sh",
    })
    assert result["status"] == "FAIL"
    assert result["execution_returncode"] == 1


def test_route_pass():
    from functional_graphs.apex_coder.validators import route
    result = route({"status": "PASS", "retry_count": 0})
    assert result["routing_target"] == "__end__"
    assert result["status"] == "PASS"


def test_route_fail_retry():
    from functional_graphs.apex_coder.validators import route
    result = route({"status": "FAIL", "retry_count": 0})
    assert result["routing_target"] == "inject_error_context"
    assert result["retry_count"] == 1


def test_route_fail_exhausted():
    from functional_graphs.apex_coder.validators import route
    result = route({"status": "FAIL", "retry_count": 4})
    assert result["routing_target"] == "__end__"
    assert result["status"] == "FAIL"


def test_route_pending_aborts():
    from functional_graphs.apex_coder.validators import route
    result = route({"retry_count": 0})  # no status field at all
    assert result["routing_target"] == "__end__"
    assert result["status"] == "FAIL"


def test_inject_error_context_builds_retry_prompt():
    from functional_graphs.apex_coder.validators import inject_error_context
    result = inject_error_context({
        "messages": [HumanMessage(content="old", id="h1")],
        "user_requirements": "build a game",
        "working_directory": "/tmp/test",
        "run_qa_script": "/tmp/test/run_qa.sh",
        "execution_stdout": "test output",
        "execution_stderr": "AssertionError: expected 4 got 3",
        "execution_returncode": 1,
        "retry_count": 1,
        "iteration_history": [],
    })
    human_msgs = [m for m in result["messages"] if isinstance(m, HumanMessage)]
    assert len(human_msgs) == 1
    assert "RETRY" in human_msgs[0].content
    assert "AssertionError" in human_msgs[0].content
    assert "build a game" in human_msgs[0].content
    assert len(result["iteration_history"]) == 1


def test_inject_error_context_includes_history():
    from functional_graphs.apex_coder.validators import inject_error_context
    result = inject_error_context({
        "messages": [HumanMessage(content="old", id="h1")],
        "user_requirements": "task",
        "working_directory": "/tmp/test",
        "run_qa_script": "/tmp/test/run_qa.sh",
        "execution_stdout": "",
        "execution_stderr": "new error",
        "execution_returncode": 1,
        "retry_count": 2,
        "iteration_history": ["Attempt 1: old error"],
    })
    assert len(result["iteration_history"]) == 2
    human_msgs = [m for m in result["messages"] if isinstance(m, HumanMessage)]
    assert "Do NOT repeat" in human_msgs[0].content


def test_setup_reads_refined_plan():
    from functional_graphs.apex_coder.validators import setup
    result = setup({
        "messages": [HumanMessage(content="Build a game")],
        "refined_plan": "Use MVC pattern with curses UI",
        "node_sessions": {"claude_main": "uuid-A", "apex_qa": "old-uuid"},
    })
    # user_requirements stays clean (just the raw task)
    assert result["user_requirements"] == "Build a game"
    # refined_plan appears in the background context message (message[0])
    all_content = " ".join(m.content for m in result["messages"])
    assert "MVC pattern" in all_content
    assert "Implementation Plan" in all_content
    # Two messages: background + task
    assert len(result["messages"]) == 2
    # Should clear subgraph session keys
    assert "apex_qa" not in result["node_sessions"]
    assert "claude_main" in result["node_sessions"]


def test_setup_reads_debate_conclusion():
    from functional_graphs.apex_coder.validators import setup
    result = setup({
        "messages": [HumanMessage(content="Build a game")],
        "debate_conclusion": "Use event-driven architecture",
        "node_sessions": {"claude_main": "uuid-A", "apex_coder": "old-uuid"},
    })
    # user_requirements stays clean
    assert result["user_requirements"] == "Build a game"
    # debate_conclusion appears in background message
    all_content = " ".join(m.content for m in result["messages"])
    assert "event-driven" in all_content
    assert "Design Decision" in all_content
    assert len(result["messages"]) == 2
    assert "apex_coder" not in result["node_sessions"]


def test_setup_routing_context_priority():
    from functional_graphs.apex_coder.validators import setup
    result = setup({
        "messages": [HumanMessage(content="ignored message")],
        "routing_context": "Build a CLI tool\n\n## 工作目录: /tmp/test_routing_ctx",
    })
    assert result["user_requirements"] == "Build a CLI tool\n\n## 工作目录: /tmp/test_routing_ctx"
    assert result["working_directory"] == "/tmp/test_routing_ctx"


def test_subgraph_exit_inherit_clears_session_keys():
    from framework.nodes.subgraph_init_node import make_subgraph_exit
    from langchain_core.messages import HumanMessage
    exit_fn = make_subgraph_exit(session_mode="inherit", subgraph_session_keys=["apex_qa", "apex_coder"])
    result = exit_fn({
        "messages": [HumanMessage(content="test", id="m1")],
        "node_sessions": {"claude_main": "uuid-A", "apex_qa": "uuid-B", "apex_coder": "uuid-C"},
    })
    # Messages should be removed
    assert any(hasattr(m, 'id') for m in result["messages"])
    # Subgraph session keys should be cleared
    ns = result["node_sessions"]
    assert "claude_main" in ns  # parent key preserved
    assert "apex_qa" not in ns  # subgraph key cleared
    assert "apex_coder" not in ns  # subgraph key cleared


def test_subgraph_exit_persistent_keeps_session_keys():
    from framework.nodes.subgraph_init_node import make_subgraph_exit
    from langchain_core.messages import HumanMessage
    exit_fn = make_subgraph_exit(session_mode="persistent", subgraph_session_keys=["apex_qa"])
    result = exit_fn({
        "messages": [HumanMessage(content="test", id="m1")],
        "node_sessions": {"claude_main": "uuid-A", "apex_qa": "uuid-B"},
    })
    # persistent mode should NOT clear session keys
    assert "node_sessions" not in result
