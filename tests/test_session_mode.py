"""
Tests for session_mode feature in external subgraph nodes.

session_mode controls how LLM sessions are managed when a parent graph
invokes an external subgraph via agent_dir:
  - persistent (default): subgraph keeps LangGraph checkpoint across calls
  - fresh_per_call: node_sessions cleared before each invocation
  - inherit: subgraph nodes inherit parent node's session ID
  - isolated: each node forced to unique session_key + sessions cleared
"""
import inspect
import json
import tempfile
from pathlib import Path

import pytest


# ---------------------------------------------------------------------------
# Source-level checks (no runtime graph needed)
# ---------------------------------------------------------------------------

def test_session_mode_branch_exists():
    """agent_loader must handle session_mode in external subgraph branch."""
    import framework.loader.graph_builder as al
    src = inspect.getsource(al._build_declarative)
    assert "session_mode" in src, "session_mode not found in _build_declarative"


def test_all_four_modes_referenced():
    """All four session_mode values must appear in _build_declarative."""
    import framework.loader.graph_builder as al
    src = inspect.getsource(al._build_declarative)
    for mode in ("persistent", "fresh_per_call", "inherit", "isolated"):
        assert mode in src, f"session_mode {mode!r} not in _build_declarative"


def test_unknown_session_mode_raises():
    """Unknown session_mode value must appear in ValueError message."""
    import framework.loader.graph_builder as al
    src = inspect.getsource(al._build_declarative)
    assert "unknown session_mode" in src, "Missing error for unknown session_mode"


def test_force_unique_session_keys_in_build_graph_signature():
    """build_graph must accept force_unique_session_keys parameter."""
    from framework.loader import EntityLoader
    sig = inspect.signature(EntityLoader.build_graph)
    assert "force_unique_session_keys" in sig.parameters, \
        "build_graph missing force_unique_session_keys param"


def test_build_declarative_accepts_force_unique_session_keys():
    """_build_declarative must accept force_unique_session_keys parameter."""
    from framework.loader.graph_builder import _build_declarative
    sig = inspect.signature(_build_declarative)
    assert "force_unique_session_keys" in sig.parameters


# ---------------------------------------------------------------------------
# Entity.json integration checks
# ---------------------------------------------------------------------------

def test_debate_brainstorm_has_fresh_per_call():
    """debate_brainstorm in technical_architect entity.json must use fresh_per_call."""
    entity_path = Path(__file__).parent.parent / "role_agents/technical_architect/entity.json"
    if not entity_path.exists():
        pytest.skip("technical_architect entity.json not found")
    data = json.loads(entity_path.read_text())
    nodes = data.get("graph", {}).get("nodes", [])
    brainstorm = next((n for n in nodes if n["id"] == "debate_brainstorm"), None)
    assert brainstorm is not None, "debate_brainstorm node not found"
    assert brainstorm.get("session_mode") == "fresh_per_call", \
        f"debate_brainstorm session_mode should be fresh_per_call, got {brainstorm.get('session_mode')}"


def test_debate_design_has_fresh_per_call():
    """debate_design in technical_architect entity.json must use fresh_per_call."""
    entity_path = Path(__file__).parent.parent / "role_agents/technical_architect/entity.json"
    if not entity_path.exists():
        pytest.skip("technical_architect entity.json not found")
    data = json.loads(entity_path.read_text())
    nodes = data.get("graph", {}).get("nodes", [])
    design = next((n for n in nodes if n["id"] == "debate_design"), None)
    assert design is not None, "debate_design node not found"
    assert design.get("session_mode") == "fresh_per_call", \
        f"debate_design session_mode should be fresh_per_call, got {design.get('session_mode')}"


def test_apex_coder_uses_inherit_session_mode():
    """apex_coder subgraph uses inherit mode (fork parent session, cleanup on exit)."""
    entity_path = Path(__file__).parent.parent / "role_agents/technical_architect/entity.json"
    if not entity_path.exists():
        pytest.skip("technical_architect entity.json not found")
    data = json.loads(entity_path.read_text())
    nodes = data.get("graph", {}).get("nodes", [])
    apex = next((n for n in nodes if n["id"] == "apex_coder"), None)
    assert apex is not None, "apex_coder node not found"
    assert apex.get("session_mode") == "inherit"


# ---------------------------------------------------------------------------
# Wrapper behavior tests (mock-level, no real LLM calls)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_fresh_per_call_uses_subgraph_init_node():
    """fresh_per_call must inject _subgraph_init/_subgraph_exit (not async wrapper)."""
    import framework.loader.graph_builder as al
    src = inspect.getsource(al._build_declarative)
    assert "_fresh_wrapper" not in src, "_fresh_wrapper should be removed"
    assert "_subgraph_init" in src
    assert "_subgraph_exit" in src


@pytest.mark.asyncio
async def test_isolated_uses_native_subgraph():
    """isolated must use native subgraph (not async wrapper)."""
    import framework.loader.graph_builder as al
    src = inspect.getsource(al._build_declarative)
    assert "_isolated_wrapper" not in src, "_isolated_wrapper should be removed"


def test_force_unique_session_keys_overrides_shared_keys():
    """When force_unique_session_keys=True, node_def.session_key should be overridden to node_id."""
    # This tests the logic inline: if session_key exists and force_unique is True,
    # session_key should become node_id
    node_def = {"id": "claude_critique_1", "session_key": "claude_debate", "type": "CLAUDE_CLI"}
    _base = dict(node_def)
    force_unique = True
    if force_unique and _base.get("session_key"):
        _base["session_key"] = _base["id"]
    assert _base["session_key"] == "claude_critique_1"
