"""
集成测试：Claude ↔ Gemini 通过 LangGraph 节点轮流发言（3轮）

使用新框架 API（ClaudeNode / GeminiNode async 接口）。
话题：「AI Agent 应该用状态机还是纯 LLM 驱动？」

需要真实 API 凭据（Claude CLI 已登录 + Gemini OAuth 已登录），
否则会在节点调用时失败。

运行：python3 test_debate.py
"""

import asyncio
import sqlite3
from typing import Annotated, TypedDict

from langchain_core.messages import AIMessage, BaseMessage, HumanMessage
from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.graph import END, StateGraph
from langgraph.graph.message import add_messages

TOTAL_ROUNDS = 3
TOPIC = "AI Agent 应该用状态机（如 LangGraph）还是纯 LLM 端到端驱动？请给出核心论点，50字以内。"


class DebateState(TypedDict):
    messages: Annotated[list[BaseMessage], add_messages]
    round: int
    speaker: str


def _format_history(messages: list[BaseMessage]) -> str:
    return "\n".join(m.content for m in messages) if messages else "（无）"


def build_debate_graph(claude_node, gemini_node):
    async def claude_speaks(state: DebateState) -> dict:
        round_n = state["round"]
        history = _format_history(state["messages"])
        if round_n == 1:
            prompt = f"你正在和 Gemini 讨论：{TOPIC}\n请给出开场论点（50字以内，中文）。"
        else:
            prompt = f"你正在进行第 {round_n} 轮辩论：{TOPIC}\n\n之前的讨论：\n{history}\n\n请针对 Gemini 的上一条回复作出回应（50字以内，中文）。"
        response, _ = await claude_node.call_llm(prompt)
        print(f"\n[第{round_n}轮 · Claude] {response}")
        return {"messages": [AIMessage(content=f"[Claude] {response}")], "round": round_n, "speaker": "gemini"}

    async def gemini_speaks(state: DebateState) -> dict:
        round_n = state["round"]
        history = _format_history(state["messages"])
        message = f"辩题：{TOPIC}\n\n目前讨论：\n{history}\n\n请针对 Claude 的上一条回复作出回应（50字以内，中文）。"
        response, _ = await gemini_node.chat(message)
        print(f"[第{round_n}轮 · Gemini] {response}")
        return {"messages": [AIMessage(content=f"[Gemini] {response}")], "round": round_n + 1, "speaker": "claude"}

    def route(state: DebateState) -> str:
        if state["speaker"] == "gemini":
            return "gemini"
        if state["round"] > TOTAL_ROUNDS:
            return "end"
        return "claude"

    g = StateGraph(DebateState)
    g.add_node("claude", claude_speaks)
    g.add_node("gemini", gemini_speaks)
    g.set_entry_point("claude")
    g.add_conditional_edges("claude", lambda s: "gemini", {"gemini": "gemini"})
    g.add_conditional_edges("gemini", route, {"claude": "claude", "end": END})
    return g


async def run():
    from pathlib import Path
    from framework.loader import EntityLoader
    from framework.nodes.llm.claude import ClaudeNode
    from framework.nodes.llm.gemini import GeminiNode

    loader = EntityLoader(Path(__file__).parent.parent / "role_agents/technical_architect")
    cfg = loader.load_config()
    claude = ClaudeNode(cfg, "")
    gemini = GeminiNode(cfg, claude)

    print(f"🎙️  辩题：{TOPIC}")
    print(f"   共 {TOTAL_ROUNDS} 轮，Claude 先发言\n{'─'*60}")

    conn = sqlite3.connect(":memory:", check_same_thread=False)
    conn.execute("PRAGMA journal_mode=WAL")
    graph = build_debate_graph(claude, gemini).compile(checkpointer=SqliteSaver(conn))

    config = {"configurable": {"thread_id": "debate_test_01"}}
    final = await graph.ainvoke(
        {"messages": [], "round": 1, "speaker": "claude"},
        config=config,
    )

    print(f"\n{'─'*60}")
    print(f"✅ 辩论结束，共 {len(final['messages'])} 条发言")


if __name__ == "__main__":
    asyncio.run(run())
