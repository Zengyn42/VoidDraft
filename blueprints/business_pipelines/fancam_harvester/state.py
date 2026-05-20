"""LangGraph state schema for fancam_harvester pipeline.

All fields are JSON strings to satisfy ZenithLoom's DETERMINISTIC node
convention (no custom reducers, fully serialisable checkpoint).

Field shapes (when parsed):
    config      : FancamConfig.__dict__
    posts       : list[PostMeta]          # from fetch node
    downloads   : list[DownloadRecord]    # from download node
    alignments  : list[AlignRecord]       # from align node
    analyses    : list[AnalysisRecord]    # from analyze node
    identities  : list[IdentityRecord]    # from identify node (LLM)
    stored      : list[StoredRecord]      # from store node
    errors      : list[str]
"""

from typing import TypedDict


class FancamState(TypedDict):
    config: str        # JSON: FancamConfig
    posts: str         # JSON: list[PostMeta]
    downloads: str     # JSON: list[DownloadRecord]
    alignments: str    # JSON: list[AlignRecord]
    extracts: str      # JSON: list[ExtractRecord]   — HD clips cut from source
    post_metas: str    # JSON: list[PostMetadata]    — date/song/performer per post
    analyses: str      # JSON: list[AnalysisRecord]
    identities: str    # JSON: list[IdentityRecord] — filled by LLM node
    stored: str        # JSON: list[StoredRecord]
    errors: str        # JSON: list[str]
