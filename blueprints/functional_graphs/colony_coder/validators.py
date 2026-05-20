"""Deterministic validator nodes for colony_coder (parent graph).

Nodes:
  e2e_gate  — after executor, route to qa (first time) or run E2E directly (subsequent)
"""

import logging
import os
import re
import subprocess

logger = logging.getLogger(__name__)

QA_FAIL_CAP = 5


def e2e_gate(state: dict) -> dict:
    """Route after executor completes.

    First time (e2e_tests_generated=False):
        → qa subgraph (generates E2E tests + runs them)

    Subsequent (e2e_tests_generated=True):
        → run E2E tests mechanically, route based on results:
          - pass → __end__
          - fail under cap → back to execute with qa_analysis
          - fail at cap → __end__ (abort)
    """
    e2e_tests_generated = state.get("e2e_tests_generated", False)
    working_dir = state.get("working_directory", "")

    if not e2e_tests_generated:
        logger.info("[e2e_gate] first pass → routing to qa subgraph")
        # Save source code snapshot as git commit before QA can corrupt it
        if working_dir and os.path.isdir(working_dir):
            try:
                r_add = subprocess.run(
                    ["git", "add", "-A"], cwd=working_dir, capture_output=True, timeout=10
                )
                r_commit = subprocess.run(
                    ["git", "commit", "-m", "[colony] pre-qa source snapshot"],
                    cwd=working_dir, capture_output=True, text=True, timeout=10,
                )
                if r_commit.returncode == 0:
                    r_hash = subprocess.run(
                        ["git", "rev-parse", "HEAD"],
                        cwd=working_dir, capture_output=True, text=True, timeout=5,
                    )
                    snap = r_hash.stdout.strip()[:8]
                    logger.info(f"[e2e_gate] source snapshot committed: {snap}")
                else:
                    logger.info(f"[e2e_gate] source snapshot: nothing to commit (already committed)")
            except Exception as e:
                logger.warning(f"[e2e_gate] source snapshot failed: {e}")
        return {"routing_target": "qa"}

    # ── Run E2E tests directly (no QA subgraph needed) ──
    working_dir = state.get("working_directory", "")
    qa_fail_count = state.get("qa_fail_count", 0)
    runner = os.path.join(working_dir, "test_tool", "run_e2e.sh")

    logger.info(
        f"[e2e_gate] e2e_tests exist → running directly "
        f"(qa_fail_count={qa_fail_count}/{QA_FAIL_CAP})"
    )

    if not os.path.isfile(runner):
        logger.warning(f"[e2e_gate] runner not found: {runner} → qa fallback")
        return {"routing_target": "qa"}

    # Run E2E tests
    try:
        r = subprocess.run(
            ["bash", runner],
            cwd=working_dir,
            capture_output=True,
            text=True,
            timeout=120,
        )
        rc = r.returncode
        stdout = r.stdout[-3000:] if r.stdout else ""
        stderr = r.stderr[-3000:] if r.stderr else ""
    except subprocess.TimeoutExpired:
        logger.error("[e2e_gate] E2E TIMEOUT (120s)")
        rc = -1
        stdout = ""
        stderr = "run_e2e.sh exceeded 120s timeout"
    except OSError as e:
        logger.error(f"[e2e_gate] OSError: {e}")
        rc = -1
        stdout = ""
        stderr = str(e)

    logger.info(f"[e2e_gate] E2E rc={rc}")

    # ── Pass ──
    if rc == 0:
        logger.info("[e2e_gate] E2E PASSED → __end__")
        return {
            "routing_target": "__end__",
            "success": True,
            "execution_stdout": stdout,
            "execution_stderr": stderr,
            "execution_returncode": rc,
        }

    # ── Fail: check cap ──
    if qa_fail_count + 1 >= QA_FAIL_CAP:
        logger.warning(f"[e2e_gate] QA fail cap ({QA_FAIL_CAP}) reached → abort")
        return {
            "routing_target": "__end__",
            "success": False,
            "qa_fail_count": qa_fail_count + 1,
            "execution_stdout": stdout,
            "execution_stderr": stderr,
            "execution_returncode": rc,
        }

    # ── Fail: build qa_analysis and loop back to executor ──
    output = (stdout + "\n" + stderr).strip()

    failed_tests = []
    for m in re.finditer(r"FAILED\s+([\w/.:]+)", output):
        failed_tests.append(m.group(1))

    # Extract per-test failure tracebacks (same logic as qa validators)
    failure_blocks: list[str] = []
    seen_root_causes: set[str] = set()
    for m in re.finditer(
        r"^_{3,}\s+(.+?)\s+_{3,}\n(.*?)(?=^_{3,}|\n={3,}\s+short)",
        output,
        re.MULTILINE | re.DOTALL,
    ):
        test_name = m.group(1).strip()
        traceback_text = m.group(2).strip()

        root_cause_match = re.search(
            r"^E\s+(\w+Error:.+)$", traceback_text, re.MULTILINE
        )
        root_cause = root_cause_match.group(1).strip() if root_cause_match else ""

        if root_cause and root_cause in seen_root_causes:
            continue
        if root_cause:
            seen_root_causes.add(root_cause)

        if len(traceback_text) > 600:
            traceback_text = "...\n" + traceback_text[-600:]
        failure_blocks.append(f"### {test_name}\n```\n{traceback_text}\n```")

    qa_lines = [f"E2E test failed (attempt {qa_fail_count + 1}/{QA_FAIL_CAP}).\n"]

    if failure_blocks:
        qa_lines.append("**Failure details (fix these):**\n")
        for block in failure_blocks[:5]:
            qa_lines.append(block)
            qa_lines.append("")
        if len(failure_blocks) > 5:
            qa_lines.append(
                f"*... {len(failure_blocks) - 5} more unique failures omitted*\n"
            )

    if failed_tests:
        qa_lines.append(f"**All failed tests ({len(failed_tests)}):**")
        for t in failed_tests[:15]:
            qa_lines.append(f"  - {t}")
        if len(failed_tests) > 15:
            qa_lines.append(f"  - ... and {len(failed_tests) - 15} more")
        qa_lines.append("")

    qa_lines.append(f"Summary: {output.splitlines()[-1] if output else ''}")
    qa_analysis = "\n".join(qa_lines)

    logger.info(
        f"[e2e_gate] → execute (qa_analysis={len(qa_analysis)} chars, "
        f"qa_fail_count→{qa_fail_count + 1})"
    )

    return {
        "routing_target": "execute",
        "qa_fail_count": qa_fail_count + 1,
        "qa_analysis": qa_analysis,
        "execution_stdout": stdout,
        "execution_stderr": stderr,
        "execution_returncode": rc,
    }
