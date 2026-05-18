"""
E2E 测试：辩论子图架构验证

覆盖：
  1. BaseAgentState 含新字段（knowledge_vault / project_docs / debate_conclusion）
  2. external subgraph 节点类型编译正确
  3. 两个辩论子图独立编译（debate_gemini_first / debate_claude_first）
  4. Hani 主图含 debate_brainstorm / debate_design 节点
  5. _make_subgraph_input_transform 正确转换 routing_context → HumanMessage
  6. LlmNode output_field 机制：子图末尾节点自动写入 state 字段

运行：
    python3 test_e2e_debate.py
"""

import asyncio
import logging
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

from dotenv import load_dotenv
load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%H:%M:%S",
    stream=sys.stdout,
)
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("urllib3").setLevel(logging.WARNING)

logger = logging.getLogger("test_e2e_debate")


async def test_state_fields():
    """BaseAgentState 含所有新字段。"""
    import typing
    from framework.state import BaseAgentState
    hints = typing.get_type_hints(BaseAgentState)
    for f in ("knowledge_vault", "project_docs", "debate_conclusion", "apex_conclusion"):
        assert f in hints, f"BaseAgentState 缺少字段: {f}"
    # subgraph_call_counts 已移除
    assert "subgraph_call_counts" not in hints, \
        "BaseAgentState 不应再含 subgraph_call_counts 字段"
    logger.info("✅ BaseAgentState fields OK")


async def test_agent_ref_registered():
    """external subgraph 由 agent_loader 直接处理（不在 registry 注册）。"""
    import framework.builtins
    from framework.registry import get_node_factory
    # external subgraph 不在 registry — 由 agent_loader 内联处理
    get_node_factory("CLAUDE_SDK")
    get_node_factory("GEMINI_API")
    get_node_factory("VALIDATE")
    logger.info("✅ Core node types registered OK")


async def test_debate_graphs_compile():
    """两个辩论子图能独立编译，节点数量正确。"""
    from framework.loader import EntityLoader

    expected = {
        str(Path(__file__).parent.parent / "functional_graphs/debate_gemini_first"): {
            "gemini_propose", "claude_critique_1", "gemini_revise",
            "claude_critique_2", "gemini_conclusion",
        },
        str(Path(__file__).parent.parent / "functional_graphs/debate_claude_first"): {
            "claude_propose", "gemini_critique_1", "claude_revise",
            "gemini_critique_2", "claude_conclusion",
        },
    }

    for agent_dir, required_nodes in expected.items():
        g = await EntityLoader(Path(agent_dir)).build_graph()
        node_ids = set(g.nodes) - {"__start__"}
        missing = required_nodes - node_ids
        assert not missing, f"{agent_dir} 缺少节点: {missing}"
        logger.info(f"✅ {agent_dir}: nodes={sorted(node_ids)}")


async def test_hani_graph_with_debate():
    """Hani 主图含 debate_brainstorm / debate_design external subgraph 节点。"""
    from framework.loader import EntityLoader

    g = await EntityLoader(Path(__file__).parent.parent / "role_agents/technical_architect").build_graph()
    node_ids = set(g.nodes)

    required = {
        "claude_main", "validate",
        "git_snapshot", "git_rollback",
        "debate_brainstorm", "debate_design",
        "apex_coder",
    }
    missing = required - node_ids
    assert not missing, f"hani 图缺少节点: {missing}"

    logger.info(f"✅ hani nodes: {sorted(node_ids)}")


def _make_mock_graph(final_state: dict, node_id: str = "gemini_conclusion"):
    """创建支持 astream(stream_mode="updates") 的 mock 图。"""
    class _MockGraph:
        def __init__(self, state, nid):
            self._state = state
            self._node_id = nid
            self._call_args = None

        async def astream(self, sub_state, *, stream_mode="updates"):
            self._call_args = sub_state
            yield {self._node_id: self._state}

    return _MockGraph(final_state, node_id)


async def test_input_transform_state_mapping():
    """SubgraphInputState 不含 messages，routing_context 和 state 字段正确透传。"""
    from framework.schema.base import SubgraphInputState

    fields = SubgraphInputState.__annotations__

    # messages 字段不在 input schema 中（原生隔离父图 messages）
    assert "messages" not in fields, "SubgraphInputState 不应含 messages 字段"

    # 相关 state 字段均在 input schema 中
    assert "routing_context" in fields, "routing_context 应在 SubgraphInputState 中"
    assert "knowledge_vault" in fields, "knowledge_vault 应在 SubgraphInputState 中"
    assert "project_docs" in fields, "project_docs 应在 SubgraphInputState 中"

    # debate_conclusion 是子图 output field，不应在 SubgraphInputState 中
    # （防止父图的上次子图结论流入子图，污染当前调用的上下文）
    assert "debate_conclusion" not in fields, "debate_conclusion 不应在 SubgraphInputState 中（output field）"

    logger.info("✅ SubgraphInputState: routing_context 和 state 字段均正确声明，messages + output fields 已隔离")


async def test_llm_node_output_field():
    """LlmNode output_field 机制：配置后把 LLM 输出写入指定 state 字段。"""
    from langchain_core.messages import HumanMessage
    from framework.config import AgentConfig
    from framework.nodes.llm.llm_node import LlmNode

    class _MockNode(LlmNode):
        async def call_llm(self, prompt, session_id="", tools=None, cwd=None, history=None, inherit_from=""):
            return "辩论结论内容", session_id or "mock-sid"

    node = _MockNode(AgentConfig(tools=[]), {
        "id": "test_node",
        "output_field": "debate_conclusion",
    })

    state = {
        "messages": [HumanMessage(content="测试输入")],
        "node_sessions": {},
        "routing_target": "",
        "routing_context": "",
        "rollback_reason": "",
        "project_root": "",
        "workspace": "",
        "project_meta": {},
    }

    result = await node(state)
    stored = result.get("debate_conclusion", "")
    assert stored.startswith("辩论结论内容"), \
        f"output_field 应把 LLM 输出写入 debate_conclusion，实际: {stored!r}"

    logger.info("✅ LlmNode output_field OK")


async def test_debate_state_schema():
    """辩论子图使用 DebateState（add_messages），消息列表应累积而非截断。"""
    import typing
    from langgraph.graph.message import add_messages
    from framework.state import DebateState

    hints = typing.get_type_hints(DebateState, include_extras=True)
    assert "messages" in hints, "DebateState 缺少 messages 字段"

    messages_annotation = hints["messages"]
    metadata = getattr(messages_annotation, "__metadata__", ())
    assert any(m is add_messages for m in metadata), (
        "DebateState.messages 应使用 add_messages reducer，而非 _keep_last_2"
    )
    logger.info("✅ DebateState uses add_messages OK")


async def test_gemini_node_system_prompt():
    """GeminiNode 从 node_config['system_prompt'] 读取自定义 system prompt。"""
    from framework.nodes.llm.gemini import GeminiNode, _GEMINI_SYSTEM
    from framework.config import AgentConfig

    cfg = AgentConfig(tools=[])
    custom_prompt = "你是一位专业辩手，请犀利发言。"

    node_with = GeminiNode(cfg, {"id": "test_node", "system_prompt": custom_prompt})
    assert node_with._system_prompt == custom_prompt, "system_prompt 未从 node_config 读取"

    node_without = GeminiNode(cfg, {"id": "test_node2"})
    assert node_without._system_prompt == _GEMINI_SYSTEM, "缺少 system_prompt 时应 fallback 到 _GEMINI_SYSTEM"

    logger.info("✅ GeminiNode system_prompt OK")


async def test_no_checkpointer_build():
    """build_graph(checkpointer=None) 编译后无 checkpointer。"""
    from framework.loader import EntityLoader

    loader = EntityLoader(Path(__file__).parent.parent / "functional_graphs/debate_gemini_first")
    graph = await loader.build_graph(checkpointer=None)

    cp = getattr(graph, "checkpointer", None)
    assert cp is None, f"checkpointer=None 应编译无 checkpointer，实际: {cp!r}"
    logger.info("✅ build_graph(checkpointer=None) → no checkpointer OK")


async def test_gemini_lru_eviction():
    """GeminiNode LRU 淘汰旧 session 缓存。"""
    from framework.nodes.llm.gemini import GeminiNode, _GeminiSessionMixin
    from framework.config import AgentConfig

    cfg = AgentConfig(tools=[])
    node = GeminiNode(cfg, {"id": "test_lru", "model": "gemini-2.5-flash"})

    from framework.nodes.llm.gemini_session import ConversationRecord
    record1 = ConversationRecord(
        sessionId="aaa-111", projectHash="test", startTime="", lastUpdated="", messages=[]
    )

    node._records["aaa-111"] = record1
    assert len(node._records) == 1

    node._evict_old_sessions()
    assert len(node._records) == 0, "evict 后 cache 应为空"

    assert _GeminiSessionMixin._SESSION_CACHE_LIMIT == 1

    logger.info("✅ GeminiNode LRU eviction OK")


async def test_gemini_delete_session():
    """gemini_session.delete_session 能删除磁盘文件。"""
    import tempfile
    from unittest.mock import patch
    import framework.nodes.llm.gemini_session as gem_sess

    with tempfile.TemporaryDirectory() as tmpdir:
        with patch.object(gem_sess, '_GEMINI_DIR', Path(tmpdir)):
            record = gem_sess.new_session("test-project", "gemini-2.5-flash")
            sid = record.sessionId
            saved_path = gem_sess.save_session(record, "test-project")
            assert saved_path.exists(), "session 文件应存在"

            deleted = gem_sess.delete_session(sid, "test-project")
            assert deleted, "delete_session 应返回 True"
            assert not saved_path.exists(), "session 文件应已被删除"

            deleted_again = gem_sess.delete_session(sid, "test-project")
            assert not deleted_again, "重复删除应返回 False"

    logger.info("✅ gemini_session.delete_session OK")


async def run():
    logger.info("=== E2E Debate 架构测试开始 ===")

    await test_state_fields()
    await test_agent_ref_registered()
    await test_debate_graphs_compile()
    await test_hani_graph_with_debate()
    await test_input_transform_state_mapping()
    await test_llm_node_output_field()
    await test_debate_state_schema()
    await test_gemini_node_system_prompt()
    await test_no_checkpointer_build()
    await test_gemini_lru_eviction()
    await test_gemini_delete_session()

    logger.info("=" * 50)
    print("\n✅ 全部测试通过")


if __name__ == "__main__":
    asyncio.run(run())
