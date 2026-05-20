#!/usr/bin/env python3
"""PreToolUse hook: block Write/Edit to test_tool/qa_tests/ directory.

Reads tool_input from stdin (JSON), checks if file_path targets QA tests.
Outputs JSON decision: {"decision": "allow"} or {"decision": "block", "reason": "..."}.
"""
import json
import sys

data = json.load(sys.stdin)
tool_input = data.get("tool_input", {})
path = tool_input.get("file_path", "") or tool_input.get("path", "")

if "test_tool/qa_tests" in path:
    print(json.dumps({
        "decision": "block",
        "reason": f"BLOCKED: Cannot modify QA test file '{path}'. Fix your source code instead.",
    }))
else:
    print(json.dumps({"decision": "allow"}))
