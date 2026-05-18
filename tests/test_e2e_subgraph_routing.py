"""
E2E 测试：子图路由 + LlmNode output_field + 原生子图接入

覆盖：
  1.  LlmNode output_field 把 LLM 输出自动写入指定 state 字段
  2.  output_field 为 None 时不写入额外字段
  3.  SubgraphInputState 不含 messages（LangGraph 原生隔离父图 messages）
  4.  SubgraphInputState 已在 schema 注册表中注册
  5.  LlmNode 子图模式（messages 为空）直接从 routing_context 读取任务
  6.  LlmNode 父图模式（routing_context 为空）fallback 到 messages[-1]
  7.  主图编译后 routing_to 边正常工作
  8.  LlmNode 无路由时重置 rollback_reason 但不重置 retry_count
  9.  LlmNode 有路由信号时保留路由信息

运行：
    pytest test_e2e_subgraph_routing.py -v
"""

import logging
import sys
from pathlib import Path

import pytest
from langchain_core.messages import AIMessage, HumanMessage

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

logger = logging.getLogger("test_e2e_subgraph_routing")


# ---------------------------------------------------------------------------
# Mock 工具
# ---------------------------------------------------------------------------

def _make_mock_graph(final_state: dict, node_id: str = "mock_node"):
    """创建支持 astream(stream_mode="updates") 的 mock 图。"""
    class _MockGraph:
        def __init__(self, state, nid):
            self._state = state
            self._node_id = nid
            self._call_count = 0
            self._received_state = None

        async def astream(self, sub_state, *, stream_mode="updates"):
            self._call_count += 1
            self._received_state = sub_state
            yield {self._node_id: self._state}

    return _MockGraph(final_state, node_id)


def _make_llm_state(**overrides) -> dict:
    """构建 LlmNode 需要的完整 state，减少测试内重复。"""
    state = {
        "messages": [HumanMessage(content="你好")],
        "node_sessions": {},
        "routing_target": "",
        "routing_context": "",
        "rollback_reason": "",
        "project_root": "",
        "workspace": "",
        "project_meta": {},
    }
    state.update(overrides)
    return state


class _MockLlmNode:
    """LlmNode 的 concrete mock 子类，避免在多个测试中重复定义。"""

    @staticmethod
    def create(reply: str, node_id: str = "claude_main", output_field: str | None = None):
        from framework.config import AgentConfig
        from framework.nodes.llm.llm_node import LlmNode

        class _Impl(LlmNode):
            async def call_llm(self, prompt, session_id="", tools=None, cwd=None, history=None, inherit_from=""):
                return reply, session_id or "mock-sid"

        cfg = {"id": node_id}
        if output_field:
            cfg["output_field"] = output_field
        return _Impl(AgentConfig(tools=[]), cfg)


# ---------------------------------------------------------------------------
# 测试用例：LlmNode output_field
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_output_field_writes_to_state():
    """LlmNode 配置 output_field 时，把 LLM 输出写入指定 state 字段。"""
    reply = "这是辩论结论。"
    node = _MockLlmNode.create(reply, output_field="debate_conclusion")

    state = _make_llm_state()
    result = await node(state)

    assert result.get("debate_conclusion") == reply, \
        "output_field 应把 LLM 输出写入 debate_conclusion"

    logger.info("PASS output_field 写入 state")


@pytest.mark.asyncio
async def test_output_field_none_no_extra_keys():
    """LlmNode 不配置 output_field 时，不写入额外字段。"""
    reply = "普通回复"
    node = _MockLlmNode.create(reply)

    state = _make_llm_state()
    result = await node(state)

    for field in ("debate_conclusion", "apex_conclusion", "knowledge_result", "discovery_report"):
        assert field not in result, f"无 output_field 时不应有 {field}"

    logger.info("PASS 无 output_field 时不写入额外字段")


@pytest.mark.asyncio
async def test_output_field_apex_conclusion():
    """output_field='apex_conclusion' 正确写入。"""
    reply = "ApexCoder 修复了 3 个 bug。"
    node = _MockLlmNode.create(reply, output_field="apex_conclusion")

    state = _make_llm_state()
    result = await node(state)

    assert result.get("apex_conclusion") == reply
    logger.info("PASS output_field apex_conclusion")


# ---------------------------------------------------------------------------
# 测试用例：SubgraphInputState input schema 隔离
# ---------------------------------------------------------------------------

def test_subgraph_input_state_excludes_messages():
    """SubgraphInputState 不含 messages 字段（LangGraph 原生隔离机制）。"""
    from framework.schema.base import SubgraphInputState

    fields = SubgraphInputState.__annotations__
    assert "messages" not in fields, "SubgraphInputState 不应含 messages 字段"
    assert "routing_context" in fields, "SubgraphInputState 应含 routing_context"
    assert "workspace" in fields
    assert "project_root" in fields

    logger.info("PASS SubgraphInputState 不含 messages 字段")


def test_subgraph_input_state_registered():
    """SubgraphInputState 已在 schema 注册表中注册。"""
    from framework.registry import get_all_schemas
    schemas = get_all_schemas()
    assert "subgraph_input_schema" in schemas, "subgraph_input_schema 应在注册表中"
    from framework.schema.base import SubgraphInputState
    assert schemas["subgraph_input_schema"] is SubgraphInputState

    logger.info("PASS subgraph_input_schema 已注册")


@pytest.mark.asyncio
async def test_llm_node_reads_routing_context_directly():
    """LlmNode 在子图模式（messages 为空）时直接从 routing_context 获取任务。"""
    captured = {}

    class _Impl:
        async def call_llm(self, prompt, session_id="", tools=None, cwd=None, history=None, inherit_from=""):
            captured["prompt"] = prompt
            return "回复", "mock-sid"

    from framework.config import AgentConfig
    from framework.nodes.llm.llm_node import LlmNode

    class _MockImpl(LlmNode):
        async def call_llm(self, prompt, session_id="", tools=None, cwd=None, history=None, inherit_from=""):
            captured["prompt"] = prompt
            return "回复", "mock-sid"

    node = _MockImpl(AgentConfig(tools=[]), {"id": "test_node"})

    # 子图模式：messages 为空，routing_context 有值
    state = {
        "messages": [],
        "routing_context": "微服务架构选型",
        "routing_target": "",
        "node_sessions": {},
        "rollback_reason": "",
        "project_root": "",
        "workspace": "",
        "project_meta": {},
    }
    await node(state)
    assert "微服务架构选型" in captured["prompt"], \
        "routing_context 应直接出现在 prompt 中"

    logger.info("PASS LlmNode 直接读 routing_context")


@pytest.mark.asyncio
async def test_llm_node_fallback_to_messages():
    """LlmNode 在父图模式（routing_context 为空）时从 messages[-1] 获取输入。"""
    captured = {}

    from framework.config import AgentConfig
    from framework.nodes.llm.llm_node import LlmNode

    class _MockImpl(LlmNode):
        async def call_llm(self, prompt, session_id="", tools=None, cwd=None, history=None, inherit_from=""):
            captured["prompt"] = prompt
            return "回复", "mock-sid"

    node = _MockImpl(AgentConfig(tools=[]), {"id": "test_node"})

    # 父图模式：messages 有内容，routing_context 为空
    state = {
        "messages": [HumanMessage(content="这是用户输入")],
        "routing_context": "",
        "routing_target": "",
        "node_sessions": {},
        "rollback_reason": "",
        "project_root": "",
        "workspace": "",
        "project_meta": {},
    }
    await node(state)
    assert "这是用户输入" in captured["prompt"], \
        "routing_context 为空时应 fallback 到 messages[-1]"

    logger.info("PASS LlmNode fallback to messages[-1]")


# ---------------------------------------------------------------------------
# 测试用例：主图编译
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_main_graph_routing_edges_no_block():
    """主图的 routing_to 边正常工作，子图节点就位。"""
    from framework.loader import EntityLoader

    loader = EntityLoader(Path("/home/kingy/Foundation/VoidDraft/role_agents/technical_architect"))
    g = await loader.build_graph()

    node_ids = set(g.nodes)
    for name in ("debate_brainstorm", "debate_design", "apex_coder"):
        assert name in node_ids, f"主图应含子图节点: {name}"

    logger.info("PASS 主图编译成功，子图节点就位")


# ---------------------------------------------------------------------------
# 测试用例：LlmNode 路由行为
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_llm_node_no_routing_resets_fields():
    """LlmNode 无路由时重置 rollback_reason 但不重置 retry_count。"""
    node = _MockLlmNode.create("普通回复")

    state = _make_llm_state(rollback_reason="之前的失败原因")
    result = await node(state)

    assert result.get("rollback_reason") == "", "无路由时 rollback_reason 应被清空"
    assert "retry_count" not in result, "LLM 节点不应在 result 中包含 retry_count（由 validator 管理）"
    assert result.get("routing_target") == "", "routing_target 应为空"

    logger.info("PASS LlmNode 无路由时重置附属字段")


@pytest.mark.asyncio
async def test_llm_node_routing_preserves_target():
    """LlmNode 有路由信号时保留路由信息。"""
    node = _MockLlmNode.create('{"route": "debate_design", "context": "测试"}\n后续内容')

    state = _make_llm_state(
        messages=[HumanMessage(content="讨论一下架构")],
    )
    result = await node(state)

    assert result.get("routing_target") == "debate_design", "应检测到路由信号"
    assert result.get("routing_context") == "测试", "routing_context 应被提取"

    logger.info("PASS LlmNode 有路由信号时保留路由信息")
