"""
LangGraph graph definition for the content_retriever pipeline.

Graph topology (linear with conditional skip edges):

    START
      │
      ▼
    [fetch]  ──── no new posts ────────────────────────────► [report] → END
      │
      ▼
    [download]  ── nothing downloaded ─────────────────────► [report] → END
      │
      ▼
    [transcribe]  ── no speech found ──────────────────────► [report] → END
      │
      ▼
    [summarize]   (optional: skipped if summarize_backend = "none")
      │
      ▼
    [report] → END

Entry node : fetch   (declared in entity.json → SubgraphRefNode uses this)
Exit node  : report  (declared in entity.json → SubgraphRefNode uses this)

ZenithLoom EntityLoader detects graph.py and calls build_graph() directly,
bypassing the declarative node/edge spec in entity.json.
"""
from __future__ import annotations

import json

from langgraph.graph import StateGraph, START, END

from pipelines.content_retriever.state import ContentRetrieverState
from pipelines.content_retriever.validators import (
    fetch,
    download,
    transcribe,
    summarize,
    report,
)
from pipelines.content_retriever.config import PipelineConfig


# --------------------------------------------------------------------------- #
# Conditional routing helpers
# --------------------------------------------------------------------------- #

def _route_after_fetch(state: ContentRetrieverState) -> str:
    posts = state.get("posts") or []
    return "download" if posts else "report"


def _route_after_download(state: ContentRetrieverState) -> str:
    downloads = state.get("downloads") or []
    return "transcribe" if downloads else "report"


def _route_after_transcribe(state: ContentRetrieverState) -> str:
    """Go to summarize only when:
    - at least one transcript has non-empty text, AND
    - summarize is enabled (summarize_backend != "none").
    """
    transcripts = state.get("transcripts") or []
    has_text = any(t.get("transcript", "").strip() for t in transcripts)
    if not has_text:
        return "report"

    try:
        cfg = PipelineConfig.from_dict(json.loads(state.get("config", "{}")))
        backend = getattr(cfg, "summarize_backend", "none")
        enabled = getattr(cfg, "summarize", True)
        if enabled and backend != "none":
            return "summarize"
    except Exception:
        pass

    return "report"


# --------------------------------------------------------------------------- #
# Graph factory
# --------------------------------------------------------------------------- #

def build_graph(config: dict | None = None, checkpointer=None):
    """
    Build and compile the content_retriever LangGraph.

    Parameters
    ----------
    config:
        Optional dict forwarded from the ZenithLoom loader (unused here;
        pipeline config is carried inside the state["config"] JSON field).
    checkpointer:
        LangGraph checkpointer (e.g. SqliteSaver) for resumable runs.
        Pass None for stateless execution.

    Returns
    -------
    CompiledStateGraph
    """
    builder = StateGraph(ContentRetrieverState)

    # ---------------------------------------------------------------- nodes
    builder.add_node("fetch",      fetch)
    builder.add_node("download",   download)
    builder.add_node("transcribe", transcribe)
    builder.add_node("summarize",  summarize)
    builder.add_node("report",     report)

    # ---------------------------------------------------------------- edges
    builder.add_edge(START, "fetch")

    builder.add_conditional_edges(
        "fetch",
        _route_after_fetch,
        {"download": "download", "report": "report"},
    )
    builder.add_conditional_edges(
        "download",
        _route_after_download,
        {"transcribe": "transcribe", "report": "report"},
    )
    builder.add_conditional_edges(
        "transcribe",
        _route_after_transcribe,
        {"summarize": "summarize", "report": "report"},
    )
    builder.add_edge("summarize", "report")
    builder.add_edge("report",    END)

    return builder.compile(checkpointer=checkpointer)
