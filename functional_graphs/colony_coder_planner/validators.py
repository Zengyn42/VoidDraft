"""Deterministic validator nodes for colony_coder_planner.

Node: decomposition_validator
  1. Parse JSON from last AI message → populate state fields (tasks, execution_order, etc.)
  2. Validate: tasks non-empty, execution_order matches task IDs
  3. Routes to: __end__ (pass or abort) | task_decompose (retry)
"""

import json
import logging
import re

logger = logging.getLogger(__name__)

RETRY_CAP = 2


def _extract_json(text: str) -> dict | None:
    """Try to extract a JSON object from text.

    Handles:
      - Pure JSON string
      - JSON inside ```json ... ``` fences
      - JSON embedded in surrounding text
    """
    if not text:
        return None

    # 1. Direct parse
    text = text.strip()
    try:
        obj = json.loads(text)
        if isinstance(obj, dict):
            return obj
    except json.JSONDecodeError:
        pass

    # 2. Extract from markdown fences
    fence_match = re.search(r"```(?:json)?\s*\n?(.*?)\n?```", text, re.DOTALL)
    if fence_match:
        try:
            obj = json.loads(fence_match.group(1).strip())
            if isinstance(obj, dict):
                return obj
        except json.JSONDecodeError:
            pass

    # 3. Find first { ... } block (greedy for the outermost braces)
    brace_match = re.search(r"\{.*\}", text, re.DOTALL)
    if brace_match:
        try:
            obj = json.loads(brace_match.group())
            if isinstance(obj, dict):
                return obj
        except json.JSONDecodeError:
            pass

    return None


def decomposition_validator(state: dict) -> dict:
    """Validate task decomposition output from task_decompose.

    Step 1: Extract JSON from the last AI message and populate state fields.
    Step 2: Validate tasks + execution_order consistency.
    Step 3: Route accordingly.
    """
    retry_count = state.get("retry_count", 0)

    logger.info(
        f"[decomposition_validator] entry: retry_count={retry_count}/{RETRY_CAP} "
        f"messages={len(state.get('messages') or [])}"
    )

    # ── Step 1: Parse JSON from last message ──
    messages = state.get("messages") or []
    last_content = ""
    for msg in reversed(messages):
        content = getattr(msg, "content", None)
        if content and hasattr(msg, "type") and msg.type == "ai":
            last_content = content
            break

    parsed = _extract_json(last_content)
    parsed_updates = {}

    if parsed:
        logger.info(f"[decomposition_validator] Parsed JSON keys: {list(parsed.keys())}")
        if "tasks" in parsed and isinstance(parsed["tasks"], list):
            parsed_updates["tasks"] = parsed["tasks"]
        if "execution_order" in parsed and isinstance(parsed["execution_order"], list):
            parsed_updates["execution_order"] = parsed["execution_order"]
        if "refined_plan" in parsed and isinstance(parsed["refined_plan"], str):
            parsed_updates["refined_plan"] = parsed["refined_plan"]
        if "working_directory" in parsed and isinstance(parsed["working_directory"], str):
            parsed_updates["working_directory"] = parsed["working_directory"]
        if "qa_plan" in parsed and isinstance(parsed["qa_plan"], str):
            parsed_updates["qa_plan"] = parsed["qa_plan"]
        if "e2e_plan" in parsed and isinstance(parsed["e2e_plan"], dict):
            parsed_updates["e2e_plan"] = parsed["e2e_plan"]
            logger.info(f"[decomposition_validator] e2e_plan keys: {list(parsed['e2e_plan'].keys())}")
        if "qa_tasks" in parsed and isinstance(parsed["qa_tasks"], list):
            parsed_updates["qa_tasks"] = parsed["qa_tasks"]
            parsed_updates["current_qa_task_index"] = 0
            logger.info(f"[decomposition_validator] qa_tasks: {[t.get('id') for t in parsed['qa_tasks']]}")
    else:
        logger.warning(f"[decomposition_validator] Failed to parse JSON from last message ({len(last_content)} chars)")

    # ── Step 2: Validate ──
    tasks = parsed_updates.get("tasks") or state.get("tasks") or []
    execution_order = parsed_updates.get("execution_order") or state.get("execution_order") or []

    def _is_valid() -> bool:
        if not tasks or not execution_order:
            return False
        task_ids = {t["id"] for t in tasks if isinstance(t, dict) and "id" in t}
        return bool(task_ids) and all(oid in task_ids for oid in execution_order)

    # ── Step 3: Route ──
    if _is_valid():
        logger.info(f"[decomposition_validator] VALID: {len(tasks)} tasks, order={execution_order}")
        result = {"routing_target": "__end__"}
        result.update(parsed_updates)
        return result

    if retry_count >= RETRY_CAP:
        logger.warning(f"[decomposition_validator] ABORT after {retry_count} retries")
        result = {
            "routing_target": "__end__",
            "success": False,
            "abort_reason": "decomposition_failed_after_retries",
        }
        result.update(parsed_updates)  # still save whatever was parsed
        return result

    logger.info(f"[decomposition_validator] RETRY ({retry_count + 1}/{RETRY_CAP})")
    result = {"routing_target": "task_decompose", "retry_count": retry_count + 1}
    result.update(parsed_updates)
    return result
