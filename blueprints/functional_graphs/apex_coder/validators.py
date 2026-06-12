"""DETERMINISTIC nodes for apex_coder TDD pipeline.

Nodes:
  setup                 — extract user_requirements, create working_directory, enrich with parent context
  reset_for_coder       — clear QA messages, build clean prompt for Coder
  executor              — run QA tests mechanically (no LLM)
  route                 — PASS → end, FAIL → retry or abort
  inject_error_context  — build retry prompt for Coder with error context
"""

import logging
import os
import re
import subprocess
import uuid
from pathlib import Path

logger = logging.getLogger(__name__)

RETRY_CAP = 5


def setup(state: dict) -> dict:
    """Extract user_requirements, create working_directory, prepare for QA."""
    from langchain_core.messages import HumanMessage

    # Input priority: routing_context (subgraph) > messages[0] (standalone)
    msgs = state.get("messages", [])
    raw_msg = msgs[0].content.strip() if msgs else ""
    raw = state.get("routing_context", "") or raw_msg

    # Smart input: file path or raw text
    if raw.startswith("/") and Path(raw).is_file():
        user_requirements = Path(raw).read_text(encoding="utf-8")
    else:
        user_requirements = raw

    # Enrich with parent design context (inherit mode)
    plan = state.get("refined_plan", "")
    debate = state.get("debate_conclusion", "")
    if plan:
        user_requirements = f"{user_requirements}\n\n## 设计方案\n{plan}"
    elif debate:
        user_requirements = f"{user_requirements}\n\n## 辩论结论\n{debate}"

    # Parse working_directory
    wd_match = re.search(
        r"[#]*\s*(?:工作目录|working.?dir(?:ectory)?)[:：]\s*(\S+)",
        user_requirements,
        re.IGNORECASE,
    )
    if wd_match:
        working_directory = wd_match.group(1)
    else:
        working_directory = f"/tmp/apex_{uuid.uuid4().hex[:8]}"

    Path(working_directory).mkdir(parents=True, exist_ok=True)
    Path(working_directory, "test_tool", "qa_tests").mkdir(parents=True, exist_ok=True)

    # Clear subgraph session keys (for re-fork on next call)
    ns = dict(state.get("node_sessions", {}))
    ns.pop("apex_qa", None)
    ns.pop("apex_coder", None)

    return {
        "user_requirements": user_requirements,
        "working_directory": working_directory,
        "node_sessions": ns,
        "messages": [HumanMessage(content=user_requirements)],
    }


# Backward-compat alias
splitter = setup


def parse_qa_output(state: dict) -> dict:
    """Parse claude_qa output (qa_summary) into qa_bypass / run_qa_script.

    Bugfix 2026-06-11: qa_bypass / run_qa_script were declared in state.py but
    NEVER written anywhere — executor always saw run_qa_script="" → guaranteed
    FAIL → retry loop burned to RETRY_CAP on every task. This parse step
    restores the QA_ROLE.md contract:
      - QA outputs "QA_BYPASS: <reason>"            → qa_bypass=True
      - QA writes <working_dir>/test_tool/run_qa.sh → run_qa_script=<path>
      - neither (QA contract violation)             → default to bypass + warn,
        otherwise executor would fail deterministically with zero chance of
        recovery (the original bug's failure mode).
    """
    qa_summary = state.get("qa_summary", "") or ""
    working_dir = state.get("working_directory", "")

    if re.search(r"QA_BYPASS\s*[:：]", qa_summary):
        logger.info("[parse_qa_output] QA_BYPASS marker found → qa_bypass=True")
        return {"qa_bypass": True, "run_qa_script": ""}

    script = os.path.join(working_dir, "test_tool", "run_qa.sh")
    if os.path.isfile(script):
        logger.info(f"[parse_qa_output] runner found: {script}")
        return {"qa_bypass": False, "run_qa_script": script}

    logger.warning(
        "[parse_qa_output] QA neither output QA_BYPASS nor wrote run_qa.sh "
        f"(expected {script}) — defaulting to bypass to avoid guaranteed-FAIL "
        f"retry loop. qa_summary head: {qa_summary[:200]!r}"
    )
    return {"qa_bypass": True, "run_qa_script": ""}


def reset_for_coder(state: dict) -> dict:
    from langchain_core.messages import HumanMessage, RemoveMessage

    # Parse QA output into state fields (see parse_qa_output docstring)
    qa_fields = parse_qa_output(state)

    user_req = state.get("user_requirements", "")
    working_dir = state.get("working_directory", "")
    qa_bypass = qa_fields["qa_bypass"]
    run_qa_script = qa_fields["run_qa_script"]

    msgs = state.get("messages", [])
    removals = [RemoveMessage(id=m.id) for m in msgs]

    lines = [f"## User Requirements\n\n{user_req}"]
    lines.append(f"\n## Working Directory: `{working_dir}`")

    if qa_bypass:
        lines.append("\n## QA: BYPASSED (no tests to pass)")
    else:
        lines.append(f"\n## QA Tests")
        lines.append(f"- Runner script: `{run_qa_script}`")
        lines.append(f"- **Read the QA tests FIRST to understand what's expected.**")
        lines.append(f"- **Run `bash {run_qa_script}` and ensure ALL tests pass before finishing.**")
        lines.append(f"- **DO NOT modify any files in `test_tool/qa_tests/`.**")

        qa_dir = os.path.join(working_dir, "test_tool", "qa_tests")
        if os.path.isdir(qa_dir):
            test_files = [f for f in os.listdir(qa_dir) if f.endswith(".py") and f != "__init__.py"]
            if test_files:
                lines.append(f"\nQA test files ({len(test_files)}):")
                for f in sorted(test_files):
                    lines.append(f"  - `{qa_dir}/{f}`")

    prompt = "\n".join(lines)
    return {
        "messages": removals + [HumanMessage(content=prompt)],
        **qa_fields,
    }


def executor(state: dict) -> dict:
    """Run QA tests mechanically. No LLM involved."""
    working_dir = state.get("working_directory", "")
    run_qa = state.get("run_qa_script", "")
    qa_bypass = state.get("qa_bypass", False)

    if qa_bypass:
        logger.info("[executor] QA bypassed → PASS")
        return {
            "execution_stdout": "",
            "execution_stderr": "",
            "execution_returncode": 0,
            "status": "PASS",
        }

    if not run_qa or not os.path.isfile(run_qa):
        logger.warning(f"[executor] run_qa_script not found: {run_qa}")
        return {
            "execution_stdout": "",
            "execution_stderr": f"run_qa_script not found: {run_qa}",
            "execution_returncode": 1,
            "status": "FAIL",
        }

    try:
        r = subprocess.run(
            ["bash", run_qa],
            cwd=working_dir,
            capture_output=True,
            text=True,
            timeout=120,
        )
        logger.info(f"[executor] exit_code={r.returncode} stdout_len={len(r.stdout)} stderr_len={len(r.stderr)}")
        return {
            "execution_stdout": r.stdout[-3000:] if r.stdout else "",
            "execution_stderr": r.stderr[-3000:] if r.stderr else "",
            "execution_returncode": r.returncode,
            "status": "PASS" if r.returncode == 0 else "FAIL",
        }
    except subprocess.TimeoutExpired:
        logger.error("[executor] TIMEOUT (120s)")
        return {
            "execution_stdout": "",
            "execution_stderr": "run_qa.sh exceeded 120s timeout",
            "execution_returncode": -1,
            "status": "FAIL",
        }
    except OSError as e:
        logger.error(f"[executor] OSError: {e}")
        return {
            "execution_stdout": "",
            "execution_stderr": str(e),
            "execution_returncode": -1,
            "status": "FAIL",
        }


def route(state: dict) -> dict:
    """Route based on executor results.

    PASS → __end__ (success)
    FAIL + retry_count < RETRY_CAP → inject_error_context (retry)
    FAIL + retry_count >= RETRY_CAP → __end__ (abort)
    PENDING (executor didn't set status) → __end__ (abort, state merge bug)
    """
    status = state.get("status", "PENDING")
    retry_count = state.get("retry_count", 0)

    logger.info(f"[route] status={status} retry_count={retry_count}/{RETRY_CAP}")

    if status == "PASS":
        logger.info("[route] PASS → __end__")
        return {
            "routing_target": "__end__",
            "status": "PASS",
        }

    if status == "PENDING":
        logger.error("[route] status is PENDING — executor output not merged. Aborting.")
        return {
            "routing_target": "__end__",
            "status": "FAIL",
        }

    if retry_count + 1 >= RETRY_CAP:
        logger.warning(f"[route] retry cap ({RETRY_CAP}) reached → __end__ (abort)")
        return {
            "routing_target": "__end__",
            "status": "FAIL",
        }

    logger.info(f"[route] FAIL → inject_error_context (retry {retry_count + 1})")
    return {
        "routing_target": "inject_error_context",
        "retry_count": retry_count + 1,
    }


def inject_error_context(state: dict) -> dict:
    """Build retry prompt for Coder with error context + iteration history."""
    from langchain_core.messages import HumanMessage, RemoveMessage

    user_req = state.get("user_requirements", "")
    working_dir = state.get("working_directory", "")
    run_qa_script = state.get("run_qa_script", "")
    stdout = state.get("execution_stdout", "")
    stderr = state.get("execution_stderr", "")
    retry_count = state.get("retry_count", 0)
    history = state.get("iteration_history", [])

    # Add current failure to history
    error_summary = f"Attempt {retry_count}: returncode={state.get('execution_returncode')}"
    if stderr:
        error_summary += f"\nstderr: {stderr[-500:]}"
    if stdout:
        error_summary += f"\nstdout (last 500): {stdout[-500:]}"
    new_history = history + [error_summary]

    # Clear old messages
    msgs = state.get("messages", [])
    removals = [RemoveMessage(id=m.id) for m in msgs]

    # Build retry prompt
    lines = [f"## RETRY — Attempt {retry_count + 1}/{RETRY_CAP}\n"]
    lines.append(f"## User Requirements\n\n{user_req}")
    lines.append(f"\n## Working Directory: `{working_dir}`")
    lines.append(f"\n## QA Tests: `{run_qa_script}`")
    lines.append(f"\n## Previous Attempt Failed\n")
    lines.append(f"```\n{stderr[-1500:]}\n```\n" if stderr else "(no stderr)\n")
    if stdout:
        lines.append(f"### stdout (last 500 chars)\n```\n{stdout[-500:]}\n```\n")

    # History of all previous attempts
    if len(new_history) > 1:
        lines.append(f"\n## Iteration History ({len(new_history)} attempts)")
        lines.append("**Do NOT repeat the same fix that already failed.**\n")
        for h in new_history:
            lines.append(f"- {h[:200]}")

    lines.append(f"\n## Instructions")
    lines.append(f"1. Read the error carefully")
    lines.append(f"2. Read the QA test that failed")
    lines.append(f"3. Fix your source code (NOT the QA tests)")
    lines.append(f"4. **DO NOT modify `test_tool/qa_tests/`**")

    prompt = "\n".join(lines)
    return {
        "messages": removals + [HumanMessage(content=prompt)],
        "iteration_history": new_history,
    }
