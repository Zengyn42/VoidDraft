"""LangGraph state schema for the content_retriever pipeline."""

from typing import TypedDict


class ContentRetrieverState(TypedDict):
    config: str       # JSON-serialized PipelineConfig
    posts: str        # JSON-serialized list of posts
    downloads: str    # JSON-serialized list of downloaded files
    analysis: str     # JSON-serialized analysis results
    report: str       # Final report text
    errors: str       # JSON-serialized error list
