"""Deterministic validator nodes for colony_coder_qa.

Nodes (each function = one DETERMINISTIC node):
  inject_e2e_context  — build prompt with e2e_plan + working_dir file listing
  run_e2e             — mechanically run test_tool/run_e2e_{qa_id}.sh
  e2e_route           — route: pass→__end__ or next qa_task, fail→execute (via parent)
"""

import logging
import os
import re
import subprocess

logger = logging.getLogger(__name__)

QA_FAIL_CAP = 5


# ---------------------------------------------------------------------------
# inject_e2e_context
# ---------------------------------------------------------------------------

def _infer_working_dir(state: dict) -> str:
    """Infer working_directory from e2e_plan or task descriptions if not set."""
    working_dir = state.get("working_directory", "")
    if working_dir:
        return working_dir
    e2e_plan = state.get("e2e_plan") or {}
    run_cmd = e2e_plan.get("run_command", "")
    m = re.search(r"(/tmp/[\w._-]+)", run_cmd)
    if m:
        return m.group(1)
    for t in (state.get("tasks") or []):
        m = re.search(r"(/tmp/[\w._-]+)", t.get("description", ""))
        if m:
            return m.group(1)
    return working_dir


def inject_e2e_context(state: dict) -> dict:
    """Route QA entry: inject scoped prompt for the current qa_task (or full e2e_plan fallback).

    qa_tasks mode (new):
      - Each call covers ONE qa_task from state['qa_tasks'][current_qa_task_index]
      - Always generates fresh tests for this qa_task's scope (smaller, faster)

    Legacy mode (no qa_tasks):
      - First entry: builds prompt for generate_e2e from e2e_plan
      - Subsequent entries: skip to run_e2e (e2e_tests_generated flag)
    """
    from langchain_core.messages import HumanMessage

    working_dir = _infer_working_dir(state)
    qa_tasks = state.get("qa_tasks") or []
    qa_task_index = state.get("current_qa_task_index", 0)

    if working_dir and working_dir != state.get("working_directory", ""):
        logger.info(f"[inject_e2e_context] inferred working_dir={working_dir!r}")

    # ── qa_tasks mode ──────────────────────────────────────────────────
    if qa_tasks:
        if qa_task_index >= len(qa_tasks):
            logger.info(
                f"[inject_e2e_context] all {len(qa_tasks)} qa_tasks done → __end__"
            )
            return {"routing_target": "__end__", "success": True}

        qa_task = qa_tasks[qa_task_index]
        qa_id = qa_task.get("id", f"q{qa_task_index + 1}")
        scope = qa_task.get("scope", "(no scope specified)")
        test_file = qa_task.get("test_file", f"test_{qa_id}.py")
        run_script = f"run_e2e_{qa_id}.sh"

        logger.info(
            f"[inject_e2e_context] qa_tasks mode: {qa_id} ({qa_task_index + 1}/{len(qa_tasks)}) "
            f"test_file={test_file}"
        )

        # List only relevant source files (excluding test_tool)
        source_files: list[str] = []
        if working_dir and os.path.isdir(working_dir):
            for root, dirs, files in os.walk(working_dir):
                dirs[:] = [d for d in dirs if d not in ("__pycache__", ".git", "test_tool")]
                for f in files:
                    if f.endswith(".py"):
                        source_files.append(
                            os.path.relpath(os.path.join(root, f), working_dir)
                        )

        # Absolute paths for crystal-clear instructions
        abs_test_file = os.path.join(working_dir, "test_tool", "e2e_tests", test_file)
        abs_run_script = os.path.join(working_dir, "test_tool", run_script)

        lines = [f"## QA Subtask {qa_task_index + 1}/{len(qa_tasks)}: `{qa_id}`\n"]
        lines.append(f"**Working directory**: `{working_dir}`\n")
        lines.append("### ⛔ DO NOT MODIFY SOURCE FILES")
        lines.append(f"**You are FORBIDDEN from writing to `{working_dir}/snake_battle.py` or any source file.**")
        lines.append("You are a TEST WRITER. You ONLY write test files and run scripts.\n")
        lines.append("### Test Scope")
        lines.append(f"{scope}\n")

        e2e_plan = state.get("e2e_plan") or {}
        headless = e2e_plan.get("headless_notes", "")
        if headless:
            lines.append(f"### Headless Testing Notes\n{headless}\n")

        if source_files:
            lines.append("### Source Files (read these to understand the code, DO NOT write to them)")
            for f in sorted(source_files):
                lines.append(f"- `{os.path.join(working_dir, f)}`")
            lines.append("")

        lines.append("### Your Task")
        lines.append(f"**Step 1**: Use `read_file` to read each source file listed above.")
        lines.append("")
        lines.append(f"**Step 2**: Output the complete pytest test file covering the scope above as a single Python code block:")
        lines.append("````")
        lines.append("```python")
        lines.append("# your test code here")
        lines.append("```")
        lines.append("````")
        lines.append("")
        lines.append("The test file will be saved to:")
        lines.append(f"  `{abs_test_file}`")
        lines.append("")
        lines.append("The run script already exists at:")
        lines.append(f"  `{abs_run_script}`")
        lines.append("")
        lines.append("### Rules")
        lines.append("- You can ONLY use `read_file`. Do NOT write files.")
        lines.append("- Import source modules directly (no curses display needed).")
        lines.append("- For curses programs: use `unittest.mock.MagicMock()` for stdscr.")
        lines.append("- Keep each test under 10 seconds; total suite under 90 seconds.")

        prompt_len = sum(len(l) for l in lines)
        logger.info(
            f"[inject_e2e_context] qa_tasks prompt ({prompt_len} chars) → scaffold_e2e"
        )
        updates = {
            "messages": [HumanMessage(content="\n".join(lines))],
            "routing_target": "scaffold_e2e",
        }
        if working_dir:
            updates["working_directory"] = working_dir
        return updates

    # ── Legacy mode (no qa_tasks) ──────────────────────────────────────
    e2e_plan = state.get("e2e_plan") or {}
    qa_plan = state.get("qa_plan", "")
    e2e_tests_generated = state.get("e2e_tests_generated", False)

    logger.info(
        f"[inject_e2e_context] legacy mode: working_dir={working_dir!r} "
        f"e2e_tests_generated={e2e_tests_generated} has_e2e_plan={bool(e2e_plan)}"
    )

    if e2e_tests_generated:
        logger.info("[inject_e2e_context] E2E tests already generated → run_e2e")
        return {"routing_target": "run_e2e"}

    lines = ["## E2E Test Generation Task\n"]
    lines.append(f"**Working directory**: `{working_dir}`\n")

    if e2e_plan:
        lines.append("### E2E Test Plan (from Planner)")
        lines.append(f"```json\n{json.dumps(e2e_plan, indent=2, ensure_ascii=False)}\n```\n")

        criteria = e2e_plan.get("acceptance_criteria") or []
        if criteria:
            lines.append("### Acceptance Criteria")
            for i, c in enumerate(criteria, 1):
                lines.append(f"{i}. {c}")
            lines.append("")

        scenarios = e2e_plan.get("test_scenarios") or []
        if scenarios:
            lines.append("### Test Scenarios")
            for s in scenarios:
                lines.append(f"- {s}")
            lines.append("")

        run_cmd = e2e_plan.get("run_command", "")
        if run_cmd:
            lines.append(f"### Run Command: `{run_cmd}`\n")

        headless = e2e_plan.get("headless_notes", "")
        if headless:
            lines.append(f"### Headless Testing Notes\n{headless}\n")
    elif qa_plan:
        lines.append("### QA Plan (legacy format)")
        lines.append(f"```\n{qa_plan}\n```\n")

    if working_dir and os.path.isdir(working_dir):
        all_files = []
        for root, dirs, files in os.walk(working_dir):
            dirs[:] = [d for d in dirs if d not in ("__pycache__", ".git")]
            for f in files:
                rel = os.path.relpath(os.path.join(root, f), working_dir)
                all_files.append(rel)
        logger.info(f"[inject_e2e_context] found {len(all_files)} files in working_dir")
        if all_files:
            lines.append("### Files in Working Directory")
            for f in sorted(all_files):
                lines.append(f"- `{f}`")
        else:
            lines.append("### Files in Working Directory\n(empty)")

    lines.append("\n### Your Task")
    lines.append("1. Read the acceptance criteria and test scenarios above.")
    lines.append("2. Examine the source files using read_file.")
    lines.append("3. Write E2E test scripts to test_tool/e2e_tests/.")
    lines.append("4. Write test_tool/run_e2e.sh to run all E2E tests.")
    lines.append("5. Run: bash test_tool/run_e2e.sh")
    lines.append("6. Report E2E_VERDICT: PASS or E2E_VERDICT: FAIL.")

    prompt_len = sum(len(l) for l in lines)
    logger.info(f"[inject_e2e_context] legacy prompt ({prompt_len} chars) → scaffold_e2e")
    updates = {
        "messages": [HumanMessage(content="\n".join(lines))],
        "routing_target": "scaffold_e2e",
        "e2e_tests_generated": True,
    }
    if working_dir:
        updates["working_directory"] = working_dir
    return updates


# ---------------------------------------------------------------------------
# scaffold_e2e  — create dirs + run script BEFORE generate_e2e runs
# ---------------------------------------------------------------------------

def scaffold_e2e(state: dict) -> dict:
    """Create test_tool/e2e_tests/ dir and run_e2e_{qa_id}.sh before generate_e2e.

    This ensures:
    - The test output directory always exists
    - The run script is always correct (deterministic, not LLM-generated)
    - generate_e2e only needs to write the test file content
    """
    working_dir = state.get("working_directory", "")
    qa_tasks = state.get("qa_tasks") or []
    qa_task_index = state.get("current_qa_task_index", 0)

    if not working_dir or not qa_tasks or qa_task_index >= len(qa_tasks):
        logger.info("[scaffold_e2e] no qa_tasks or working_dir — skip")
        return {}

    qa_task = qa_tasks[qa_task_index]
    qa_id = qa_task.get("id", f"q{qa_task_index + 1}")
    test_file = qa_task.get("test_file", f"test_{qa_id}.py")

    e2e_dir = os.path.join(working_dir, "test_tool", "e2e_tests")
    run_script = os.path.join(working_dir, "test_tool", f"run_e2e_{qa_id}.sh")
    rel_test = f"test_tool/e2e_tests/{test_file}"

    try:
        os.makedirs(e2e_dir, exist_ok=True)
        with open(run_script, "w") as f:
            f.write(
                f"#!/bin/bash\nset -e\n"
                f"cd \"$(dirname \"$0\")/..\"\n"
                f"python3 -m pytest {rel_test} -v 2>&1\n"
            )
        os.chmod(run_script, 0o755)
        logger.info(
            f"[scaffold_e2e] created dir={e2e_dir} run_script={run_script}"
        )
    except OSError as e:
        logger.error(f"[scaffold_e2e] failed: {e}")

    return {}


# ---------------------------------------------------------------------------
# write_test_file  — extract code block from generate_e2e output, write to disk
# ---------------------------------------------------------------------------

def write_test_file(state: dict) -> dict:
    """Extract Python code block from generate_e2e's last message and write to disk.

    generate_e2e outputs the test code inside a ```python ... ``` block.
    This node:
    1. Extracts that code block
    2. Writes it to the exact path: test_tool/e2e_tests/{test_file}
    3. Verifies the file was written
    """
    from langchain_core.messages import AIMessage

    working_dir = state.get("working_directory", "")
    qa_tasks = state.get("qa_tasks") or []
    qa_task_index = state.get("current_qa_task_index", 0)

    if not working_dir or not qa_tasks or qa_task_index >= len(qa_tasks):
        logger.warning("[write_test_file] missing working_dir or qa_tasks — skip")
        return {}

    qa_task = qa_tasks[qa_task_index]
    qa_id = qa_task.get("id", f"q{qa_task_index + 1}")
    test_file = qa_task.get("test_file", f"test_{qa_id}.py")
    test_path = os.path.join(working_dir, "test_tool", "e2e_tests", test_file)

    # Find last AI message
    messages = state.get("messages") or []
    last_ai = None
    for msg in reversed(messages):
        if isinstance(msg, AIMessage) or getattr(msg, "type", None) == "ai":
            last_ai = msg.content if hasattr(msg, "content") else str(msg)
            break

    if not last_ai:
        logger.error("[write_test_file] no AI message found in state")
        return {}

    # Extract ```python ... ``` block (greedy, take the largest block)
    code_blocks = re.findall(r"```(?:python)?\n(.*?)```", last_ai, re.DOTALL)
    if not code_blocks:
        # Fallback: try to extract anything that looks like Python
        logger.warning("[write_test_file] no code block found — trying raw extraction")
        # Look for import / def patterns
        lines = last_ai.splitlines()
        code_lines = []
        in_code = False
        for line in lines:
            if line.startswith(("import ", "from ", "class ", "def ", "@")):
                in_code = True
            if in_code:
                code_lines.append(line)
        code = "\n".join(code_lines) if code_lines else ""
    else:
        # Take the largest Python code block (most likely to be the full test file)
        code = max(code_blocks, key=len)

    if not code.strip():
        logger.error("[write_test_file] could not extract test code from AI output")
        return {}

    try:
        os.makedirs(os.path.dirname(test_path), exist_ok=True)
        with open(test_path, "w", encoding="utf-8") as f:
            f.write(code)
        logger.info(
            f"[write_test_file] wrote {len(code)} chars to {test_path}"
        )
    except OSError as e:
        logger.error(f"[write_test_file] write failed: {e}")

    return {}


# ---------------------------------------------------------------------------
# run_e2e
# ---------------------------------------------------------------------------

def _run_e2e_tests(state: dict, caller: str = "run_e2e") -> dict:
    """Shared implementation: run E2E tests mechanically.

    qa_tasks mode: runs test_tool/run_e2e_{qa_id}.sh for the current qa_task.
    Legacy mode: runs test_tool/run_e2e.sh.
    """
    working_dir = state.get("working_directory", "")
    qa_tasks = state.get("qa_tasks") or []
    qa_task_index = state.get("current_qa_task_index", 0)

    # qa_tasks mode: use per-task run script
    test_file_path = ""
    if qa_tasks and qa_task_index < len(qa_tasks):
        qa_task = qa_tasks[qa_task_index]
        qa_id = qa_task.get("id", f"q{qa_task_index + 1}")
        test_file = qa_task.get("test_file", f"test_{qa_id}.py")
        runner = os.path.join(working_dir, "test_tool", f"run_e2e_{qa_id}.sh")
        test_file_path = os.path.join(working_dir, "test_tool", "e2e_tests", test_file)
        # Fallback to generic run_e2e.sh if per-task script missing
        if not os.path.isfile(runner):
            fallback = os.path.join(working_dir, "test_tool", "run_e2e.sh")
            if os.path.isfile(fallback):
                logger.info(f"[{caller}] per-task script missing, falling back to run_e2e.sh")
                runner = fallback
    else:
        runner = os.path.join(working_dir, "test_tool", "run_e2e.sh")

    logger.info(f"[{caller}] runner={runner} exists={os.path.isfile(runner)}")

    if not os.path.isfile(runner):
        # ── Fallback: if test file exists, run pytest directly ──────────
        if test_file_path and os.path.isfile(test_file_path):
            logger.info(f"[{caller}] run script missing but test file found → pytest fallback: {test_file_path}")
            # Auto-create the run script for future iterations
            script_path = os.path.join(working_dir, "test_tool", f"run_e2e_{qa_id}.sh")
            rel_test = os.path.relpath(test_file_path, working_dir)
            try:
                os.makedirs(os.path.dirname(script_path), exist_ok=True)
                with open(script_path, "w") as f:
                    f.write(f"#!/bin/bash\nset -e\ncd \"$(dirname \"$0\")/..\"\npython3 -m pytest {rel_test} -v 2>&1\n")
                os.chmod(script_path, 0o755)
                logger.info(f"[{caller}] auto-created run script: {script_path}")
                runner = script_path
            except OSError as e:
                logger.warning(f"[{caller}] could not auto-create run script: {e} → pytest direct")
                try:
                    r = subprocess.run(
                        ["python3", "-m", "pytest", rel_test, "-v"],
                        cwd=working_dir, capture_output=True, text=True, timeout=120,
                    )
                    return {
                        "execution_stdout": r.stdout[-3000:] if r.stdout else "",
                        "execution_stderr": r.stderr[-3000:] if r.stderr else "",
                        "execution_returncode": r.returncode,
                    }
                except (subprocess.TimeoutExpired, OSError) as e2:
                    return {"execution_stdout": "", "execution_stderr": str(e2), "execution_returncode": -1}
        else:
            # Neither run script nor test file exists at expected paths.
            # Scan test_tool/ for any test file that may have been written to wrong path.
            found_test = None
            found_run_script = None
            test_tool_dir = os.path.join(working_dir, "test_tool")
            if working_dir and os.path.isdir(test_tool_dir):
                for root, dirs, files in os.walk(test_tool_dir):
                    dirs[:] = [d for d in dirs if d not in ("__pycache__", ".git", "unit_tests")]
                    for fname in files:
                        fpath = os.path.join(root, fname)
                        if fname.endswith(".py") and fname.startswith("test_") and fpath != test_file_path:
                            found_test = fpath
                            logger.info(f"[{caller}] found misplaced test file: {fpath}")
                        elif fname.startswith("run_e2e") and fname.endswith(".sh") and fpath != runner:
                            found_run_script = fpath
                            logger.info(f"[{caller}] found misplaced run script: {fpath}")

            if found_test:
                # Move to expected location and auto-create run script
                expected_dir = os.path.dirname(test_file_path)
                try:
                    os.makedirs(expected_dir, exist_ok=True)
                    import shutil
                    shutil.move(found_test, test_file_path)
                    logger.info(f"[{caller}] moved test file to: {test_file_path}")
                    # Create run script
                    script_path = os.path.join(working_dir, "test_tool", f"run_e2e_{qa_id}.sh")
                    rel_test = os.path.relpath(test_file_path, working_dir)
                    with open(script_path, "w") as f:
                        f.write(f"#!/bin/bash\nset -e\ncd \"$(dirname \"$0\")/..\"\npython3 -m pytest {rel_test} -v 2>&1\n")
                    os.chmod(script_path, 0o755)
                    logger.info(f"[{caller}] auto-created run script: {script_path}")
                    runner = script_path
                except (OSError, Exception) as e:
                    logger.warning(f"[{caller}] could not relocate test file: {e}")
                    # Fall through to infra failure
            else:
                # Truly nothing found — infra failure
                logger.warning(f"[{caller}] run script AND test file both missing → infra failure")
                return {
                    "execution_stdout": "",
                    "execution_stderr": (
                        f"INFRA_FAILURE: generate_e2e did not create the required files.\n"
                        f"Missing run script: {runner}\n"
                        f"Missing test file: {test_file_path or '(unknown)'}\n"
                        f"Scanned test_tool/ — no test files found.\n"
                        f"⛔ REMINDER: You must write files to EXACT paths:\n"
                        f"  Test file: {test_file_path}\n"
                        f"  Run script: {os.path.join(working_dir, 'test_tool', f'run_e2e_{qa_id}.sh')}"
                    ),
                    "execution_returncode": 1,
                    "e2e_infra_failure": True,
                }

    try:
        r = subprocess.run(
            ["bash", runner],
            cwd=working_dir,
            capture_output=True,
            text=True,
            timeout=120,
        )
        logger.info(
            f"[{caller}] exit_code={r.returncode} "
            f"stdout_len={len(r.stdout)} stderr_len={len(r.stderr)}"
        )
        if r.returncode != 0:
            logger.debug(f"[{caller}] stderr={r.stderr[-500:]}")
        return {
            "execution_stdout": r.stdout[-3000:] if r.stdout else "",
            "execution_stderr": r.stderr[-3000:] if r.stderr else "",
            "execution_returncode": r.returncode,
        }
    except subprocess.TimeoutExpired:
        logger.error(f"[{caller}] TIMEOUT (120s)")
        return {
            "execution_stdout": "",
            "execution_stderr": "run_e2e.sh exceeded 120s timeout",
            "execution_returncode": -1,
        }
    except OSError as e:
        logger.error(f"[{caller}] OSError: {e}")
        return {
            "execution_stdout": "",
            "execution_stderr": str(e),
            "execution_returncode": -1,
        }


def run_e2e(state: dict) -> dict:
    """Run E2E tests after generate_e2e."""
    logger.info("[run_e2e] → running E2E tests (post generate_e2e)")
    return _run_e2e_tests(state, caller="run_e2e")




# ---------------------------------------------------------------------------
# e2e_route
# ---------------------------------------------------------------------------

def e2e_route(state: dict) -> dict:
    """Route E2E test results.

    qa_tasks mode:
      Pass → advance current_qa_task_index; if more qa_tasks → inject_e2e_context; else → __end__
      Fail → route back to execute (reset current_qa_task_index=0 so all tests re-run after fix)

    Legacy mode:
      Pass (rc==0)                       → __end__ (success)
      Fail, qa_fail_count < QA_FAIL_CAP  → __end__ with routing_target="execute"
      Fail, qa_fail_count >= QA_FAIL_CAP → inject_rescue_context (escalate)
    """
    rc = state.get("execution_returncode")
    qa_fail_count = state.get("qa_fail_count", 0)
    qa_tasks = state.get("qa_tasks") or []
    qa_task_index = state.get("current_qa_task_index", 0)
    infra_failure = state.get("e2e_infra_failure", False)
    infra_retry_count = state.get("e2e_infra_retry_count", 0)
    INFRA_RETRY_CAP = 2

    logger.info(
        f"[e2e_route] rc={rc} qa_fail_count={qa_fail_count}/{QA_FAIL_CAP} "
        f"qa_tasks_mode={bool(qa_tasks)} qa_task_index={qa_task_index}/{len(qa_tasks)} "
        f"infra_failure={infra_failure} infra_retry={infra_retry_count}/{INFRA_RETRY_CAP}"
    )

    # ── Infra failure: generate_e2e didn't create required files ──────────
    if infra_failure and rc != 0:
        if infra_retry_count < INFRA_RETRY_CAP:
            logger.warning(
                f"[e2e_route] INFRA_FAILURE: test files not created → retry generate_e2e "
                f"({infra_retry_count + 1}/{INFRA_RETRY_CAP})"
            )
            return {
                "routing_target": "inject_e2e_context",
                "e2e_infra_failure": False,
                "e2e_infra_retry_count": infra_retry_count + 1,
            }
        else:
            logger.error(
                f"[e2e_route] INFRA_FAILURE: generate_e2e failed to create files "
                f"after {INFRA_RETRY_CAP} retries → abort"
            )
            return {
                "routing_target": "__end__",
                "success": False,
                "qa_fail_count": qa_fail_count + 1,
                "e2e_infra_failure": False,
            }

    if rc == 0:
        # ── qa_tasks mode: advance index ──
        if qa_tasks:
            next_idx = qa_task_index + 1
            qa_id = qa_tasks[qa_task_index].get("id", f"q{qa_task_index + 1}") if qa_task_index < len(qa_tasks) else "?"
            if next_idx >= len(qa_tasks):
                logger.info(f"[e2e_route] qa_task {qa_id} PASSED — all {len(qa_tasks)} qa_tasks done → __end__")
                return {
                    "routing_target": "__end__",
                    "current_qa_task_index": next_idx,
                    "success": True,
                }
            else:
                next_id = qa_tasks[next_idx].get("id", f"q{next_idx + 1}")
                logger.info(
                    f"[e2e_route] qa_task {qa_id} PASSED → next qa_task {next_id} "
                    f"({next_idx + 1}/{len(qa_tasks)})"
                )
                return {
                    "routing_target": "inject_e2e_context",
                    "current_qa_task_index": next_idx,
                }

        # ── Legacy mode: all done ──
        logger.info("[e2e_route] E2E tests PASSED → __end__ (success)")
        return {
            "routing_target": "__end__",
            "success": True,
        }

    logger.info(
        f"[e2e_route] E2E tests FAILED "
        f"(qa_fail_count will be {qa_fail_count + 1}/{QA_FAIL_CAP})"
    )

    if qa_fail_count + 1 >= QA_FAIL_CAP:
        logger.warning(
            f"[e2e_route] QA fail cap ({QA_FAIL_CAP}) reached → abort (no rescue)"
        )
        return {
            "routing_target": "__end__",
            "success": False,
            "qa_fail_count": qa_fail_count + 1,
        }

    # qa_tasks mode: reset index so all qa_tasks re-run after executor fix
    extra = {"current_qa_task_index": 0} if qa_tasks else {}

    stdout = state.get("execution_stdout", "")
    stderr = state.get("execution_stderr", "")
    output = (stdout + "\n" + stderr).strip()

    failed_tests = []
    for m in re.finditer(r"FAILED\s+([\w/.:]+)", output):
        failed_tests.append(m.group(1))

    # ── Extract per-test failure tracebacks ──
    # pytest FAILURES section is delimited by ____ TestName ____ headers
    failure_blocks: list[str] = []
    seen_root_causes: set[str] = set()
    for m in re.finditer(
        r"^_{3,}\s+(.+?)\s+_{3,}\n(.*?)(?=^_{3,}|\n={3,}\s+short)",
        output,
        re.MULTILINE | re.DOTALL,
    ):
        test_name = m.group(1).strip()
        traceback_text = m.group(2).strip()

        # Deduplicate by root cause error type
        root_cause_match = re.search(r"^E\s+(\w+Error:.+)$", traceback_text, re.MULTILINE)
        root_cause = root_cause_match.group(1).strip() if root_cause_match else ""

        # Skip if we already have a block with the same root cause
        if root_cause and root_cause in seen_root_causes:
            continue
        if root_cause:
            seen_root_causes.add(root_cause)

        # Truncate very long tracebacks (keep last 600 chars per block)
        if len(traceback_text) > 600:
            traceback_text = "...\n" + traceback_text[-600:]
        failure_blocks.append(f"### {test_name}\n```\n{traceback_text}\n```")

    qa_analysis_lines = [
        f"E2E test failed (attempt {qa_fail_count + 1}/{QA_FAIL_CAP}).\n",
    ]

    # Per-test tracebacks — most actionable info for the coder
    if failure_blocks:
        qa_analysis_lines.append("**Failure details (fix these):**\n")
        # Cap at 5 unique failure blocks to stay within context budget
        for block in failure_blocks[:5]:
            qa_analysis_lines.append(block)
            qa_analysis_lines.append("")
        if len(failure_blocks) > 5:
            qa_analysis_lines.append(
                f"*... {len(failure_blocks) - 5} more unique failures omitted*\n"
            )

    if failed_tests:
        qa_analysis_lines.append(f"**All failed tests ({len(failed_tests)}):**")
        for t in failed_tests[:15]:
            qa_analysis_lines.append(f"  - {t}")
        if len(failed_tests) > 15:
            qa_analysis_lines.append(f"  - ... and {len(failed_tests) - 15} more")
        qa_analysis_lines.append("")

    qa_analysis_lines.append(f"Summary: {output.splitlines()[-1] if output else ''}")
    qa_analysis = "\n".join(qa_analysis_lines)

    logger.info(
        f"[e2e_route] → routing_target='execute' "
        f"(qa_analysis={len(qa_analysis)} chars, qa_fail_count→{qa_fail_count + 1})"
    )

    return {
        "routing_target": "execute",
        "qa_fail_count": qa_fail_count + 1,
        "qa_analysis": qa_analysis,
        **extra,
    }


# (qa_rescue removed — gemma4 was unreliable as a rescue agent)
def inject_rescue_context(state: dict) -> dict:
    """Stub: qa_rescue removed. Should not be called."""
    return {"routing_target": "__end__", "success": False}
    """Build full context for qa_rescue: e2e_plan + failure details + source code."""
    from langchain_core.messages import HumanMessage

    working_dir = state.get("working_directory", "")
    e2e_plan = state.get("e2e_plan") or {}
    qa_plan = state.get("qa_plan", "")
    refined_plan = state.get("refined_plan", "")
    qa_fail_count = state.get("qa_fail_count", 0)

    logger.info(
        f"[inject_rescue_context] working_dir={working_dir!r} "
        f"qa_fail_count={qa_fail_count} has_e2e_plan={bool(e2e_plan)} "
        f"has_refined_plan={bool(refined_plan)}"
    )

    stdout = state.get("execution_stdout", "")
    stderr = state.get("execution_stderr", "")
    test_output = (stdout + "\n" + stderr).strip()

    lines = ["## Rescue Mission — Full Context\n"]
    lines.append(f"**Working directory**: `{working_dir}`")
    lines.append(f"**Previous attempts exhausted**: {qa_fail_count} QA cycles failed\n")

    # E2E plan
    if e2e_plan:
        lines.append("### E2E Test Plan (Acceptance Criteria)")
        lines.append(f"```json\n{json.dumps(e2e_plan, indent=2, ensure_ascii=False)}\n```\n")
    elif qa_plan:
        lines.append("### QA Plan (legacy)")
        lines.append(f"```\n{qa_plan}\n```\n")

    # Design
    if refined_plan:
        lines.append("### Design Plan")
        lines.append(f"```\n{refined_plan[:2000]}\n```\n")

    # E2E failure output
    if test_output:
        lines.append("### E2E Test Failure Output")
        lines.append(f"```\n{test_output[-3000:]}\n```\n")

    # Source code
    if working_dir and os.path.isdir(working_dir):
        source_files = []
        for root, dirs, files in os.walk(working_dir):
            dirs[:] = [d for d in dirs if d not in ("__pycache__", ".git", "test_tool")]
            for f in files:
                if f.endswith(".py"):
                    source_files.append(os.path.join(root, f))

        if source_files:
            lines.append("### Current Source Files (fix these)")
            for sf in sorted(source_files):
                rel = os.path.relpath(sf, working_dir)
                try:
                    with open(sf, "r", encoding="utf-8") as fh:
                        content = fh.read()
                    lines.append(f"\n**`{rel}`** ({len(content.splitlines())} lines):")
                    lines.append(f"```python\n{content}\n```")
                except OSError:
                    lines.append(f"\n**`{rel}`**: could not read file")

    lines.append("\n### Your Mission")
    lines.append("1. Study the E2E test plan — it defines what 'correct' means.")
    lines.append("2. Read the E2E test failure output — it shows what's broken.")
    lines.append("3. Fix or rewrite the SOURCE CODE (do NOT modify E2E tests).")
    lines.append("4. Run: bash test_tool/run_e2e.sh to verify your fix.")
    lines.append("5. Report RESCUE_VERDICT: PASS or RESCUE_VERDICT: FAIL.")

    prompt_len = sum(len(l) for l in lines)
    logger.info(f"[inject_rescue_context] built rescue prompt ({prompt_len} chars)")
    return {"messages": [HumanMessage(content="\n".join(lines))]}


def rescue_route(state: dict) -> dict:
    """Stub: qa_rescue removed. Should not be called."""
    return {"routing_target": "__end__", "success": False}
