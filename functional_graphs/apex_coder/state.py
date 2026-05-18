"""ApexCoderState — apex_coder TDD pipeline state schema.

splitter / ClaudeQA / reset_for_coder / ClaudeCoder 共享此 schema。

Auto-registers as "apex_coder_schema" on import.
"""

from __future__ import annotations

from typing import Annotated, Optional

from framework.schema.base import BaseAgentState
from framework.schema.reducers import _merge_dict
from framework.registry import register_schema


class ApexCoderState(BaseAgentState):
    # Splitter output
    user_requirements: str
    working_directory: str

    # QA output
    qa_bypass: bool
    qa_tests_dir: str
    run_qa_script: str
    qa_summary: str

    # Coder output
    apex_conclusion: str

    # Executor output
    execution_stdout: str
    execution_stderr: str
    execution_returncode: Optional[int]

    # Retry loop
    iteration_history: list          # list of "attempt N: error was X"
    retry_count: int                 # current retry count
    status: str = "PENDING"          # "PENDING", "PASS", "FAIL" — default PENDING to catch state merge bugs

    # Override node_sessions with merge reducer
    node_sessions: Annotated[dict, _merge_dict]


register_schema("apex_coder_schema", ApexCoderState)
