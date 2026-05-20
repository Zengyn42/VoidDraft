"""Deterministic validator nodes for colony_coder_integrator.

Nodes:
  integration_test  — run test_tool/run_tests.sh (DETERMINISTIC, no LLM)
  integration_route — routes to __end__ (pass/abort) | integration_rescue (retry)
"""

import logging

logger = logging.getLogger(__name__)

RETRY_CAP = 2


def integration_test(state: dict) -> dict:
    """Run test_tool/run_tests.sh for integration verification. Pure code, no LLM.

    Same protocol as executor's soft_validate:
      - run_tests.sh is the unified test entry point
      - exit code 0 = all tests pass
      - exit code non-zero = failure
    """
    import os
    import subprocess

    working_dir = state.get("working_directory", "")
    runner = os.path.join(working_dir, "test_tool", "run_tests.sh")

    if not os.path.isfile(runner):
        return {"validation_output": {
            "status": "fail",
            "category": "missing_runner",
            "rationale": f"test_tool/run_tests.sh not found in {working_dir}",
        }}

    try:
        r = subprocess.run(
            ["bash", runner],
            cwd=working_dir,
            capture_output=True,
            text=True,
            timeout=120,
        )
        if r.returncode == 0:
            return {"validation_output": {
                "status": "pass",
                "category": "all_tests_pass",
                "rationale": r.stdout[-2000:] if r.stdout else "All tests passed",
            }}

        output = (r.stdout + "\n" + r.stderr)[-3000:]
        return {"validation_output": {
            "status": "fail",
            "category": "test_failure",
            "rationale": output,
        }}
    except subprocess.TimeoutExpired:
        return {"validation_output": {
            "status": "fail",
            "category": "timeout",
            "rationale": "run_tests.sh exceeded 120s timeout",
        }}
    except OSError as e:
        return {"validation_output": {
            "status": "fail",
            "category": "execution_error",
            "rationale": str(e),
        }}


def integration_route(state: dict) -> dict:
    """Route integration test results."""
    vo = state.get("validation_output") or {}
    status = vo.get("status", "fail")
    retry_count = state.get("retry_count", 0)

    if status == "pass":
        return {"routing_target": "__end__", "success": True}

    if status == "abort" or retry_count >= RETRY_CAP:
        return {
            "routing_target": "__end__",
            "success": False,
            "abort_reason": vo.get("rationale", "integration_abort"),
        }

    return {"routing_target": "integration_rescue", "retry_count": retry_count + 1}
