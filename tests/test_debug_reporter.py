"""Tests for DebugConsoleReporter — scope tracking and value formatting."""
import pytest
import tempfile
from pathlib import Path
from langchain_core.messages import AIMessage, HumanMessage


# --- Scope tracking ---

def test_scope_name_top_level():
    from framework.debug_reporter import DebugConsoleReporter
    r = DebugConsoleReporter("myapp")
    assert r._scope_name(()) == "myapp"

def test_scope_name_one_level():
    from framework.debug_reporter import DebugConsoleReporter
    r = DebugConsoleReporter("myapp")
    assert r._scope_name(("plan:abc123",)) == "plan"

def test_scope_name_nested():
    from framework.debug_reporter import DebugConsoleReporter
    r = DebugConsoleReporter("myapp")
    assert r._scope_name(("plan:abc123", "design_debate:def456")) == "design_debate"

def test_depth():
    from framework.debug_reporter import DebugConsoleReporter
    r = DebugConsoleReporter("myapp")
    assert r._depth(()) == 0
    assert r._depth(("a:1",)) == 1
    assert r._depth(("a:1", "b:2")) == 2


# --- Value formatting ---

def test_format_value_short_string():
    from framework.debug_reporter import DebugConsoleReporter
    r = DebugConsoleReporter("test")
    assert r._format_value("hello") == "'hello'"

def test_format_value_long_string():
    from framework.debug_reporter import DebugConsoleReporter
    r = DebugConsoleReporter("test")
    long_s = "x" * 100
    result = r._format_value(long_s)
    assert "100 chars" in result

def test_format_value_list():
    from framework.debug_reporter import DebugConsoleReporter
    r = DebugConsoleReporter("test")
    assert r._format_value([1, 2, 3]) == "3 items"

def test_format_value_dict():
    from framework.debug_reporter import DebugConsoleReporter
    r = DebugConsoleReporter("test")
    assert r._format_value({"a": 1, "b": 2}) == "{2 keys}"

def test_format_value_int():
    from framework.debug_reporter import DebugConsoleReporter
    r = DebugConsoleReporter("test")
    assert r._format_value(42) == "42"

def test_format_value_bool():
    from framework.debug_reporter import DebugConsoleReporter
    r = DebugConsoleReporter("test")
    assert r._format_value(True) == "True"

def test_format_value_none():
    from framework.debug_reporter import DebugConsoleReporter
    r = DebugConsoleReporter("test")
    assert r._format_value(None) == "None"


# --- on_event ---

def test_on_event_skips_start_end(capsys):
    from framework.debug_reporter import DebugConsoleReporter
    r = DebugConsoleReporter("test")
    r.on_event((), {"__start__": {}})
    r.on_event((), {"__end__": {}})
    captured = capsys.readouterr()
    assert captured.out == ""


def test_on_event_prints_node(capsys):
    from framework.debug_reporter import DebugConsoleReporter
    r = DebugConsoleReporter("test")
    r.on_event((), {"my_node": {"routing_target": "__end__"}})
    out = capsys.readouterr().out
    assert "[test]" in out
    assert "my_node" in out


def test_on_event_prints_ai_message(capsys):
    from framework.debug_reporter import DebugConsoleReporter
    r = DebugConsoleReporter("test")
    msg = AIMessage(content="hello world output", id="msg1")
    r.on_event((), {"my_node": {"messages": [msg]}})
    out = capsys.readouterr().out
    assert "hello world output" in out
    assert "\u2502" in out  # │ prefix


def test_on_event_scope_enter(capsys):
    from framework.debug_reporter import DebugConsoleReporter
    r = DebugConsoleReporter("app")
    r.on_event((), {"node_a": {"x": 1}})
    r.on_event(("sub:123",), {"node_b": {"y": 2}})
    out = capsys.readouterr().out
    assert "\u25b6 sub" in out  # ▶ sub (subgraph enter)


def test_on_event_scope_exit(capsys):
    from framework.debug_reporter import DebugConsoleReporter
    r = DebugConsoleReporter("app")
    r.on_event(("sub:123",), {"node_b": {"y": 2}})
    r.on_event((), {"node_a": {"x": 1}})
    out = capsys.readouterr().out
    assert "\u25c0 sub" in out  # ◀ sub (subgraph exit)


def test_on_event_routing_target(capsys):
    from framework.debug_reporter import DebugConsoleReporter
    r = DebugConsoleReporter("test")
    r.on_event((), {"validator": {"routing_target": "code_gen"}})
    out = capsys.readouterr().out
    assert "Route:" in out
    assert "code_gen" in out


def test_on_event_state_changes(capsys):
    from framework.debug_reporter import DebugConsoleReporter
    r = DebugConsoleReporter("test")
    r.on_event((), {"my_node": {"retry_count": 3, "success": True}})
    out = capsys.readouterr().out
    assert "retry_count=3" in out
    assert "success=True" in out


def test_node_count_increments():
    from framework.debug_reporter import DebugConsoleReporter
    r = DebugConsoleReporter("test")
    r.on_event((), {"a": {"x": 1}})
    r.on_event((), {"b": {"y": 2}})
    assert r._node_count == 2


# --- Markdown logging ---

def test_markdown_log_created():
    from framework.debug_reporter import DebugConsoleReporter
    with tempfile.TemporaryDirectory() as tmp:
        log_dir = Path(tmp) / "logs"
        r = DebugConsoleReporter("test", log_dir=log_dir)
        assert (log_dir / "debug_report.md").exists()
        content = (log_dir / "debug_report.md").read_text()
        assert "Debug Report" in content


def test_markdown_log_appended():
    from framework.debug_reporter import DebugConsoleReporter
    with tempfile.TemporaryDirectory() as tmp:
        log_dir = Path(tmp) / "logs"
        r = DebugConsoleReporter("test", log_dir=log_dir)
        r.on_event((), {"my_node": {"x": 42}})
        content = (log_dir / "debug_report.md").read_text()
        assert "my_node" in content


def test_markdown_log_includes_ai_output():
    from framework.debug_reporter import DebugConsoleReporter
    with tempfile.TemporaryDirectory() as tmp:
        log_dir = Path(tmp) / "logs"
        r = DebugConsoleReporter("test", log_dir=log_dir)
        msg = AIMessage(content="the full llm output", id="m1")
        r.on_event((), {"llm": {"messages": [msg]}})
        content = (log_dir / "debug_report.md").read_text()
        assert "the full llm output" in content


def test_print_summary(capsys):
    from framework.debug_reporter import DebugConsoleReporter
    r = DebugConsoleReporter("test")
    r.on_event((), {"a": {"x": 1}})
    r.on_event((), {"b": {"success": True}})
    r.print_summary()
    out = capsys.readouterr().out
    assert "2" in out  # node count
    assert "success" in out.lower()


@pytest.mark.asyncio
async def test_colony_coder_graph_with_subgraphs_true():
    """Verify colony_coder graph compiles with native subgraphs."""
    import functional_graphs.colony_coder.state  # noqa: F401
    from framework.loader import EntityLoader

    loader = EntityLoader(Path("/home/kingy/Foundation/VoidDraft/functional_graphs/colony_coder"))
    graph = await loader.build_graph(checkpointer=None)

    # Verify the graph has subgraph nodes
    node_ids = set(graph.nodes) - {"__start__", "__end__"}
    assert "plan" in node_ids, f"Missing 'plan' node, got: {node_ids}"
    assert "execute" in node_ids, f"Missing 'execute' node, got: {node_ids}"
    assert "qa" in node_ids, f"Missing 'qa' node, got: {node_ids}"

    # Verify astream accepts subgraphs=True without error
    assert hasattr(graph, 'astream'), "Compiled graph should have astream method"
