"""
ContentRetrieverState — LangGraph state for the content_retriever pipeline.

Uses Annotated[list, operator.add] reducers so each node only appends
its own results without overwriting other nodes' contributions.

Compatible with ZenithLoom SubgraphRefNode: entry/exit are declared in
agent.json. For full ZenithLoom integration, extend this to inherit from
BaseAgentState and add field mapping in the parent agent.json.
"""
from __future__ import annotations

import operator
from typing import Annotated
from typing_extensions import TypedDict


class ContentRetrieverState(TypedDict, total=False):
    # ------------------------------------------------------------------ config
    config: str                                       # JSON-serialised PipelineConfig

    # -------------------------------------------------------- pipeline outputs
    posts: Annotated[list[dict], operator.add]        # fetched post dicts
    downloads: Annotated[list[dict], operator.add]    # per-post download records
    transcripts: Annotated[list[dict], operator.add]  # per-video transcript records
    summaries: Annotated[list[dict], operator.add]    # per-transcript LLM summaries
    errors: Annotated[list[str], operator.add]        # accumulated error strings

    # ------------------------------------------------------- terminal output
    report: str                                       # final markdown report
