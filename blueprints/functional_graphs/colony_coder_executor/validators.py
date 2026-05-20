"""Deterministic validator nodes for colony_coder_executor.

Nodes (each function = one DETERMINISTIC node):
  inject_task_context  — build prompt with SINGLE task, clear session, inject dependencies
  run_tests            — git stash snapshot + run test_tool/run_tests.sh
  test_route           — compound regression check, task advancement on pass

Per-Task Sequential Execution (2026-04-19):
  Tasks are executed one at a time. Each task gets its own code_gen session.
  On pass → advance to next task → inject_task_context again.
  On all tasks done → __end__.

Context Explosion Fix (2026-04-17):
  Session reset + deterministic summary + git stash anti-regression + replace_lines.
  See Vault: Colony Coder Context Explosion — Final Implementation Plan.md
"""

import logging
import os
import re
import subprocess

logger = logging.getLogger(__name__)

TEST_RETRY_CAP = 2


# ── Git Stash Snapshot Helpers ───────────────────────────────────────


def _ensure_git_repo(working_dir: str) -> bool:
    """Ensure working_dir is a git repo with at least one commit."""
    try:
        r = subprocess.run(
            ["git", "rev-parse", "--git-dir"],
            cwd=working_dir, capture_output=True, timeout=5,
        )
        if r.returncode != 0:
            subprocess.run(
                ["git", "init"], cwd=working_dir,
                capture_output=True, timeout=5, check=True,
            )
            # Set local identity so commits work in any environment
            subprocess.run(
                ["git", "config", "user.email", "colony-coder@zengyn.local"],
                cwd=working_dir, capture_output=True, timeout=5,
            )
            subprocess.run(
                ["git", "config", "user.name", "Colony Coder"],
                cwd=working_dir, capture_output=True, timeout=5,
            )
            logger.info(f"[git] initialized repo in {working_dir}")

        # Ensure at least one commit (stash requires it)
        r = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=working_dir, capture_output=True, timeout=5,
        )
        if r.returncode != 0:
            # Set identity if repo existed but has no commits
            subprocess.run(
                ["git", "config", "user.email", "colony-coder@zengyn.local"],
                cwd=working_dir, capture_output=True, timeout=5,
            )
            subprocess.run(
                ["git", "config", "user.name", "Colony Coder"],
                cwd=working_dir, capture_output=True, timeout=5,
            )
            subprocess.run(
                ["git", "add", "-A"], cwd=working_dir,
                capture_output=True, timeout=10,
            )
            subprocess.run(
                ["git", "commit", "--allow-empty", "-m", "[colony] init"],
                cwd=working_dir, capture_output=True, text=True, timeout=10,
            )
        return True
    except Exception as e:
        logger.warning(f"[git] ensure repo failed: {e}")
        return False


def _git_commit_task(working_dir: str, task_id: str) -> bool:
    """Commit current state as a permanent checkpoint after task passes.

    Creates a real git commit (not stash) as a durable transaction boundary.
    Later tasks failing can roll back to this commit via git reset.
    """
    if not _ensure_git_repo(working_dir):
        return False
    try:
        subprocess.run(
            ["git", "add", "-A"], cwd=working_dir,
            capture_output=True, timeout=10,
        )
        r = subprocess.run(
            ["git", "commit", "-m", f"[colony] task {task_id} complete"],
            cwd=working_dir, capture_output=True, text=True, timeout=10,
        )
        if r.returncode == 0:
            logger.info(f"[git] committed task {task_id}")
            return True
        elif "nothing to commit" in (r.stdout + r.stderr):
            logger.info(f"[git] nothing to commit for task {task_id}")
            return True
        else:
            logger.warning(f"[git] commit failed: {r.stderr[:200]}")
            return False
    except Exception as e:
        logger.warning(f"[git] commit task failed: {e}")
        return False


def _stash_snapshot(working_dir: str, label: str) -> str | None:
    """Save current state via git stash, then re-apply it.

    Returns the stash commit hash for later rollback/diff.
    Working directory is left unchanged after this call.
    """
    if not _ensure_git_repo(working_dir):
        return None
    try:
        # Stage everything (including untracked files)
        subprocess.run(
            ["git", "add", "-A"], cwd=working_dir,
            capture_output=True, timeout=10,
        )
        # Stash with label
        r = subprocess.run(
            ["git", "stash", "push", "-m", f"[colony] {label}"],
            cwd=working_dir, capture_output=True, text=True, timeout=10,
        )
        if "No local changes" in (r.stdout + r.stderr):
            # Nothing to stash — return HEAD as the snapshot
            r2 = subprocess.run(
                ["git", "rev-parse", "HEAD"],
                cwd=working_dir, capture_output=True, text=True, timeout=5,
            )
            logger.info(f"[stash] no changes to stash for '{label}'")
            return r2.stdout.strip() if r2.returncode == 0 else None

        # Get stash commit hash (stable, unlike stash@{{N}} indices which shift)
        r2 = subprocess.run(
            ["git", "rev-parse", "stash@{0}"],
            cwd=working_dir, capture_output=True, text=True, timeout=5,
        )
        stash_hash = r2.stdout.strip() if r2.returncode == 0 else None

        # Re-apply immediately so working directory stays unchanged
        subprocess.run(
            ["git", "stash", "apply"],
            cwd=working_dir, capture_output=True, timeout=10,
        )

        if stash_hash:
            logger.info(f"[stash] snapshot '{label}' → {stash_hash[:8]}")
        return stash_hash
    except Exception as e:
        logger.warning(f"[stash] snapshot failed: {e}")
        return None


def _stash_diff_summary(working_dir: str, stash_hash: str | None) -> str:
    """Compact diff summary between stash snapshot and current working tree."""
    if not stash_hash:
        return ""
    try:
        r = subprocess.run(
            ["git", "diff", "--stat", stash_hash],
            cwd=working_dir, capture_output=True, text=True, timeout=10,
        )
        return r.stdout[-1000:] if r.stdout else ""
    except Exception:
        return ""


def _stash_restore(working_dir: str, stash_hash: str) -> bool:
    """Restore working directory to a stash snapshot.

    Discards all current changes, then re-applies the stash.
    """
    try:
        # Discard all current modifications and untracked files
        subprocess.run(
            ["git", "checkout", "."],
            cwd=working_dir, capture_output=True, timeout=10,
        )
        subprocess.run(
            ["git", "clean", "-fd"],
            cwd=working_dir, capture_output=True, timeout=10,
        )
        # Re-apply the snapshot
        r = subprocess.run(
            ["git", "stash", "apply", stash_hash],
            cwd=working_dir, capture_output=True, text=True, timeout=10,
        )
        if r.returncode == 0:
            logger.info(f"[stash] restored to {stash_hash[:8]}")
            return True
        else:
            logger.error(f"[stash] apply failed: {r.stderr[:200]}")
            return False
    except Exception as e:
        logger.error(f"[stash] restore failed: {e}")
        return False


# ── Test Result Parsing ──────────────────────────────────────────────


def _parse_pytest_results(stdout: str, stderr: str) -> dict:
    """Parse pytest output for counts and test names."""
    output = stdout + "\n" + stderr

    counts = {"passed": 0, "failed": 0, "errors": 0, "total": 0}
    m = re.search(r"(\d+) passed", output)
    if m:
        counts["passed"] = int(m.group(1))
    m = re.search(r"(\d+) failed", output)
    if m:
        counts["failed"] = int(m.group(1))
    m = re.search(r"(\d+) error", output)
    if m:
        counts["errors"] = int(m.group(1))
    counts["total"] = counts["passed"] + counts["failed"] + counts["errors"]

    # Extract test names from PASSED/FAILED lines
    failed_tests = []
    for m in re.finditer(r"FAILED\s+([\w/.:]+)", output):
        failed_tests.append(m.group(1))
    passed_tests = []
    for m in re.finditer(r"PASSED\s+([\w/.:]+)", output):
        passed_tests.append(m.group(1))

    return {
        **counts,
        "failed_tests": failed_tests,
        "passed_tests": passed_tests,
    }


def _check_regression(prev_results: dict | None, curr_results: dict) -> bool:
    """Compound regression detection: 3 conditions must ALL be true.

    1. passed_now < passed_prev
    2. A previously-passing test now fails (true regression)
    3. total_collected didn't increase (rules out 'unlocked more tests')
    """
    if not prev_results:
        return False

    cond1 = curr_results["passed"] < prev_results["passed"]
    prev_passed = set(prev_results.get("passed_tests", []))
    curr_failed = set(curr_results.get("failed_tests", []))
    cond2 = bool(prev_passed & curr_failed)
    cond3 = curr_results["total"] <= prev_results["total"]

    if cond1 and cond2 and cond3:
        regressed = prev_passed & curr_failed
        logger.warning(
            f"[regression] DETECTED: passed {prev_results['passed']}→{curr_results['passed']}, "
            f"regressed tests: {regressed}"
        )
    return cond1 and cond2 and cond3


# ── Session & Summary Helpers ────────────────────────────────────────


def _extract_intent_snippet(state: dict) -> str:
    """Extract the first assistant message from current session (model's initial plan)."""
    node_sessions = state.get("node_sessions") or {}
    session_uuid = node_sessions.get("executor_session")
    if not session_uuid:
        return ""
    ollama_sessions = state.get("ollama_sessions") or {}
    messages = ollama_sessions.get(session_uuid, [])
    for msg in messages:
        if msg.get("role") == "assistant" and msg.get("content"):
            # Take first ~200 tokens (rough: 4 chars/token = 800 chars)
            return msg["content"][:800]
    return ""


def _clear_executor_session(state: dict) -> dict:
    """Return state update dict that clears the executor's Ollama session."""
    node_sessions = state.get("node_sessions") or {}
    session_uuid = node_sessions.get("executor_session")
    if session_uuid:
        ollama_sessions = dict(state.get("ollama_sessions") or {})
        ollama_sessions[session_uuid] = []
        return {"ollama_sessions": ollama_sessions}
    return {}


def _build_deterministic_summary(
    diff_summary: str,
    test_results: dict,
    test_output: str,
    intent_snippet: str = "",
    attempt: int = 0,
) -> str:
    """Build deterministic context summary for session-reset injection."""
    lines = [f"## Iteration Summary (attempt {attempt})\n"]

    if intent_snippet:
        lines.append("### Previous Approach")
        lines.append(f"{intent_snippet}\n")

    if diff_summary:
        lines.append("### Changes Made (diff --stat)")
        lines.append(f"```\n{diff_summary}\n```\n")

    lines.append("### Test Results")
    lines.append(f"- Passed: {test_results.get('passed', 0)}")
    lines.append(f"- Failed: {test_results.get('failed', 0)}")
    lines.append(f"- Errors: {test_results.get('errors', 0)}")

    if test_results.get("failed", 0) > 0 or test_results.get("errors", 0) > 0:
        lines.append("\n### Error Output (truncated)")
        lines.append(f"```\n{test_output[-1500:]}\n```")

    return "\n".join(lines)


# ── Dependency Extraction ────────────────────────────────────────────


def _extract_dependencies(state: dict, working_dir: str) -> str:
    """Extract dependency signatures from Planner-declared dependencies."""
    tasks = state.get("tasks") or []
    dep_lines = []
    seen_files: set[str] = set()

    for task in tasks:
        for dep in task.get("dependencies", []):
            # Skip task-ID dependencies (strings like "t1", "t2")
            if not isinstance(dep, dict):
                continue
            dep_file = dep.get("file", "")
            if not dep_file or dep_file in seen_files:
                continue
            seen_files.add(dep_file)
            full_path = os.path.join(working_dir, dep_file)
            if not os.path.isfile(full_path):
                continue
            try:
                content = open(full_path, encoding="utf-8").read()
                symbols = dep.get("symbols", [])
                if symbols:
                    dep_lines.append(f"#### {dep_file}")
                    for sym in symbols:
                        # Extract symbol name (handle "Class.method() -> Type" format)
                        sym_name = sym.split("(")[0].split(".")[-1].strip()
                        pattern = rf"^((?:class|def|async\s+def)\s+{re.escape(sym_name)}\b.*)"
                        match = re.search(pattern, content, re.MULTILINE)
                        if match:
                            dep_lines.append(f"```python\n{match.group(1)}\n```")
                        else:
                            dep_lines.append(f"- `{sym}` (signature not found)")
            except Exception:
                pass

    return "\n".join(dep_lines) if dep_lines else ""


# ── Existing Files Scanner ──────────────────────────────────────────


def _list_existing_files(working_dir: str) -> list[str]:
    """List .py files in working_dir (excluding test_tool, __pycache__)."""
    if not working_dir or not os.path.isdir(working_dir):
        return []
    result = []
    for root, dirs, files in os.walk(working_dir):
        dirs[:] = [d for d in dirs if d not in ("__pycache__", ".git", "test_tool")]
        for f in files:
            if f.endswith(".py"):
                result.append(os.path.relpath(os.path.join(root, f), working_dir))
    return sorted(result)


# ── QA Fix Mode ─────────────────────────────────────────────────────


def _build_qa_fix_context(
    state: dict, working_dir: str, qa_analysis: str, qa_fail_count: int, tasks: list
) -> dict:
    """Build context for fixing E2E test failures (QA re-entry).

    Unlike per-task mode, this gives Qwen the full picture:
    all existing source files + E2E failure details.
    """
    from langchain_core.messages import HumanMessage

    if working_dir:
        _ensure_git_repo(working_dir)

    lines = ["## E2E Fix Task (QA re-entry)\n"]
    lines.append(f"**Working directory**: `{working_dir}`")
    lines.append(f"**QA attempt**: {qa_fail_count}\n")

    lines.append("### E2E Test Failures (fix these)")
    lines.append(f"```\n{qa_analysis[-3000:]}\n```\n")

    existing_files = _list_existing_files(working_dir)
    if existing_files:
        lines.append("### Source Files to Fix")
        lines.append("Use `read_file` to examine these, then fix with `replace_lines`.\n")
        for f in existing_files:
            lines.append(f"- `{f}`")
        lines.append("")

    lines.append("### Instructions")
    lines.append("1. Read the E2E failure details above carefully.")
    lines.append("2. Use `read_file` to examine the relevant source files.")
    lines.append("3. Fix the issues using `replace_lines` (prefer over `write_file`).")
    lines.append("4. Run unit tests: `bash test_tool/run_tests.sh` via bash_exec")
    lines.append("5. Make sure unit tests still pass after your fixes.")
    lines.append(
        "\n**IMPORTANT**: Do NOT rewrite entire files. "
        "Use `replace_lines` to make targeted fixes. "
        "Always `read_file` first to get current line numbers."
    )

    updates: dict = {
        "routing_target": "code_gen",
        "messages": [HumanMessage(content="\n".join(lines))],
        "retry_count": 0,
        "prev_test_results": None,
        "prev_snapshot_hash": None,
        "intent_snippet": "",
    }
    if working_dir:
        updates["working_directory"] = working_dir
    updates.update(_clear_executor_session(state))
    return updates


# ── Node: inject_task_context ────────────────────────────────────────


def inject_task_context(state: dict) -> dict:
    """Build context for code_gen with SINGLE current task. Clears session.

    Per-task sequential execution:
      - Only injects tasks[current_task_index]
      - Lists existing files from previous tasks as context
      - Resets session and retry counters per task
    """
    from langchain_core.messages import HumanMessage

    tasks = state.get("tasks") or []
    execution_order = state.get("execution_order") or []
    working_dir = state.get("working_directory", "")
    qa_analysis = state.get("qa_analysis", "")
    qa_fail_count = state.get("qa_fail_count", 0)
    task_idx = state.get("current_task_index", 0)

    # Fallback: infer working_directory from task descriptions if Planner omitted it
    if not working_dir and tasks:
        import re as _re
        for t in tasks:
            desc = t.get("description", "")
            m = _re.search(r"(/tmp/[\w._-]+)", desc)
            if m:
                working_dir = m.group(1)
                logger.info(f"[inject_task_context] inferred working_dir={working_dir!r} from tasks")
                break

    # Resolve current task
    total_tasks = len(execution_order) if execution_order else len(tasks)

    # ── QA re-entry: all tasks done, but E2E failed ──
    # Switch to "fix mode": inject qa_analysis + existing files, no per-task split
    if task_idx >= total_tasks and qa_analysis:
        logger.info(
            f"[inject_task_context] QA re-entry: E2E failed "
            f"→ fix mode with qa_analysis ({len(qa_analysis)} chars)"
        )
        return _build_qa_fix_context(state, working_dir, qa_analysis, qa_fail_count, tasks)

    if task_idx >= total_tasks:
        logger.info(f"[inject_task_context] all {total_tasks} tasks done → __end__")
        return {"routing_target": "__end__"}

    current_tid = execution_order[task_idx] if execution_order else f"t{task_idx + 1}"
    current_task = next((t for t in tasks if t.get("id") == current_tid), None)
    if not current_task and tasks and task_idx < len(tasks):
        current_task = tasks[task_idx]

    logger.info(
        f"[inject_task_context] task {task_idx + 1}/{total_tasks} "
        f"id={current_tid} working_dir={working_dir!r} "
        f"qa_fail_count={qa_fail_count}"
    )

    # Ensure git repo for stash snapshots
    if working_dir:
        _ensure_git_repo(working_dir)

    lines = [f"## Task {task_idx + 1} of {total_tasks}: `{current_tid}`\n"]
    lines.append(f"**Working directory**: `{working_dir}`\n")

    # ── Current task description ──
    if current_task:
        lines.append("### Your Task")
        lines.append(current_task.get("description", "(no description)"))
        lines.append("")

    # ── Completed tasks summary (so Qwen knows what exists) ──
    if task_idx > 0:
        completed_tids = execution_order[:task_idx] if execution_order else []
        if completed_tids:
            lines.append("### Already Completed Tasks")
            for tid in completed_tids:
                lines.append(f"- ✅ {tid}")
            lines.append("")

        # Show existing files from previous tasks
        existing_files = _list_existing_files(working_dir)
        if existing_files:
            lines.append("### Existing Files (from previous tasks)")
            lines.append("Use `read_file` to examine these before writing code that depends on them.\n")
            for f in existing_files:
                lines.append(f"- `{f}`")
            lines.append("")

    # ── Upcoming tasks (brief, so Qwen knows what comes next) ──
    remaining_tids = execution_order[task_idx + 1:] if execution_order else []
    if remaining_tids:
        lines.append("### Upcoming Tasks (DO NOT implement these now)")
        for tid in remaining_tids:
            t = next((t for t in tasks if t.get("id") == tid), None)
            desc_preview = (t.get("description", "")[:80] + "...") if t else ""
            lines.append(f"- {tid}: {desc_preview}")
        lines.append("")

    # ── QA feedback (only on QA re-entry, applies to all tasks) ──
    if qa_analysis:
        logger.info(
            f"[inject_task_context] injecting QA feedback "
            f"({len(qa_analysis)} chars, qa_fail_count={qa_fail_count})"
        )
        lines.append("### QA Feedback (from E2E testing)")
        lines.append("Fix these issues in your implementation:")
        lines.append(f"```\n{qa_analysis[-3000:]}\n```\n")

    # ── Planner-declared dependencies for current task ──
    if current_task:
        task_deps_info = []
        for dep in current_task.get("dependencies", []):
            if not isinstance(dep, dict):
                continue
            dep_file = dep.get("file", "")
            full_path = os.path.join(working_dir, dep_file) if dep_file else ""
            if full_path and os.path.isfile(full_path):
                symbols = dep.get("symbols", [])
                if symbols:
                    task_deps_info.append(f"#### {dep_file}")
                    try:
                        content = open(full_path, encoding="utf-8").read()
                        for sym in symbols:
                            sym_name = sym.split("(")[0].split(".")[-1].strip()
                            pattern = rf"^((?:class|def|async\s+def)\s+{re.escape(sym_name)}\b.*)"
                            match = re.search(pattern, content, re.MULTILINE)
                            if match:
                                task_deps_info.append(f"```python\n{match.group(1)}\n```")
                            else:
                                task_deps_info.append(f"- `{sym}` (signature not found)")
                    except Exception:
                        pass
        if task_deps_info:
            lines.append("### Dependencies (interfaces from previous tasks)")
            lines.extend(task_deps_info)
            lines.append("")

    # ── Instructions ──
    lines.append("### Instructions")
    lines.append("⚠️ **STEP 1 IS CRITICAL — DO NOT SKIP IT.**")
    lines.append("1. **FIRST: Write the PRODUCT CODE** for this task only. Do NOT implement upcoming tasks.")
    if task_idx > 0:
        lines.append("2. Use `read_file` to examine existing files from previous tasks.")
        lines.append("3. Use `write_file` for NEW files, `replace_lines` for EDITING existing files.")
    else:
        lines.append("2. Use `write_file` to create new source files.")
    lines.append(f"{'3' if task_idx == 0 else '4'}. Write unit tests for THIS task in test_tool/unit_tests/.")
    lines.append(f"{'4' if task_idx == 0 else '5'}. Write test_tool/run_tests.sh to run all tests.")
    lines.append(f"{'5' if task_idx == 0 else '6'}. Run tests via `bash_exec` and fix until all pass.")
    lines.append(
        "\n**IMPORTANT**: Only implement what this task describes. "
        "Do NOT write code for future tasks. Do NOT import classes/functions that don't exist yet."
    )
    lines.append(
        "**IMPORTANT**: Always `read_file` before `replace_lines` to get current line numbers."
    )

    # State updates: clear session, reset retry counters
    updates: dict = {
        "routing_target": "code_gen",
        "messages": [HumanMessage(content="\n".join(lines))],
        "current_task_index": task_idx,
        "current_task_id": current_tid,
        "retry_count": 0,
        "prev_test_results": None,
        "prev_snapshot_hash": None,
        "intent_snippet": "",
    }
    if working_dir:
        updates["working_directory"] = working_dir
    updates.update(_clear_executor_session(state))
    return updates


# ── Node: run_tests ──────────────────────────────────────────────────


def run_tests(state: dict) -> dict:
    """Git stash snapshot current code, then run test_tool/run_tests.sh."""
    working_dir = state.get("working_directory", "")
    runner = os.path.join(working_dir, "test_tool", "run_tests.sh")
    retry_count = state.get("retry_count", 0)

    logger.info(f"[run_tests] runner={runner} exists={os.path.isfile(runner)}")

    # Stash snapshot before testing
    stash_hash = _stash_snapshot(working_dir, f"pre-test-attempt-{retry_count}")

    if not os.path.isfile(runner):
        # Auto-generate run_tests.sh if test directory exists
        test_dir = os.path.join(working_dir, "test_tool", "unit_tests")
        if os.path.isdir(test_dir):
            logger.info(f"[run_tests] auto-generating {runner} (model didn't create it)")
            os.makedirs(os.path.dirname(runner), exist_ok=True)
            with open(runner, "w") as f:
                f.write(
                    '#!/bin/bash\nset -e\n'
                    'cd "$(dirname "$0")/.."\n'
                    'python3 -m pytest test_tool/unit_tests/ -v 2>&1\n'
                )
            os.chmod(runner, 0o755)
        else:
            logger.warning(f"[run_tests] no runner and no test_tool/unit_tests/: {runner}")
            return {
                "execution_stdout": "",
                "execution_stderr": f"test_tool/run_tests.sh not found and no test_tool/unit_tests/ in {working_dir}",
                "execution_returncode": 1,
                "prev_snapshot_hash": stash_hash,
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
            f"[run_tests] exit_code={r.returncode} "
            f"stdout_len={len(r.stdout)} stderr_len={len(r.stderr)}"
        )
        if r.returncode != 0:
            logger.debug(f"[run_tests] stderr={r.stderr[-500:]}")
        return {
            "execution_stdout": r.stdout[-3000:] if r.stdout else "",
            "execution_stderr": r.stderr[-3000:] if r.stderr else "",
            "execution_returncode": r.returncode,
            "prev_snapshot_hash": stash_hash,
        }
    except subprocess.TimeoutExpired:
        logger.error("[run_tests] TIMEOUT (120s)")
        return {
            "execution_stdout": "",
            "execution_stderr": "run_tests.sh exceeded 120s timeout",
            "execution_returncode": -1,
            "prev_snapshot_hash": stash_hash,
        }
    except OSError as e:
        logger.error(f"[run_tests] OSError: {e}")
        return {
            "execution_stdout": "",
            "execution_stderr": str(e),
            "execution_returncode": -1,
            "prev_snapshot_hash": stash_hash,
        }


# ── Node: test_route ─────────────────────────────────────────────────


def test_route(state: dict) -> dict:
    """Route based on test results with per-task advancement.

    Pass  -> advance current_task_index:
             - more tasks? → inject_task_context (next task)
             - all done?   → __end__
    Fail  -> compound regression check -> rollback if needed -> code_gen (retry)
    Cap   -> skip to next task (or __end__ if last task)
    """
    from langchain_core.messages import HumanMessage

    rc = state.get("execution_returncode")
    retry_count = state.get("retry_count", 0)
    working_dir = state.get("working_directory", "")
    stdout = state.get("execution_stdout", "")
    stderr = state.get("execution_stderr", "")
    output = (stdout + "\n" + stderr).strip()
    task_idx = state.get("current_task_index", 0)
    execution_order = state.get("execution_order") or []
    total_tasks = len(execution_order) if execution_order else len(state.get("tasks") or [])

    # Parse test results
    curr_results = _parse_pytest_results(stdout, stderr)

    if rc == 5:
        # ── No tests collected (exit=5): executor didn't write unit tests, treat as pass ──
        # Testing responsibility belongs to the QA subgraph, not the executor.
        next_idx = task_idx + 1
        current_tid = execution_order[task_idx] if task_idx < len(execution_order) else f"t{task_idx + 1}"
        if working_dir:
            _git_commit_task(working_dir, current_tid)
        if next_idx >= total_tasks:
            logger.info(f"[test_route] task {current_tid} no-tests (rc=5) — ALL {total_tasks} tasks done → __end__")
            return {"routing_target": "__end__", "prev_test_results": curr_results, "current_task_index": next_idx}
        else:
            next_tid = execution_order[next_idx] if next_idx < len(execution_order) else f"t{next_idx + 1}"
            logger.info(f"[test_route] task {current_tid} no-tests (rc=5) → next task {next_tid} ({next_idx + 1}/{total_tasks})")
            return {"routing_target": "inject_task_context", "prev_test_results": curr_results, "current_task_index": next_idx}

    if rc == 0:
        # ── Task passed → git commit as transaction boundary → advance ──
        next_idx = task_idx + 1
        current_tid = execution_order[task_idx] if task_idx < len(execution_order) else f"t{task_idx + 1}"
        if working_dir:
            _git_commit_task(working_dir, current_tid)

        if next_idx >= total_tasks:
            logger.info(
                f"[test_route] task {current_tid} PASSED "
                f"({curr_results['passed']} passed) — ALL {total_tasks} tasks done → __end__"
            )
            return {
                "routing_target": "__end__",
                "prev_test_results": curr_results,
                "current_task_index": next_idx,
            }
        else:
            next_tid = execution_order[next_idx] if next_idx < len(execution_order) else f"t{next_idx + 1}"
            logger.info(
                f"[test_route] task {current_tid} PASSED "
                f"({curr_results['passed']} passed) → next task {next_tid} "
                f"({next_idx + 1}/{total_tasks})"
            )
            return {
                "routing_target": "inject_task_context",
                "prev_test_results": curr_results,
                "current_task_index": next_idx,
            }

    # ── Task failed ──
    logger.info(
        f"[test_route] tests FAILED rc={rc} "
        f"(attempt {retry_count + 1}/{TEST_RETRY_CAP}) "
        f"passed={curr_results['passed']} failed={curr_results['failed']}"
    )

    if retry_count >= TEST_RETRY_CAP:
        # Skip to next task (don't block entire pipeline on one failing task)
        next_idx = task_idx + 1
        if next_idx >= total_tasks:
            logger.warning(
                f"[test_route] retry cap ({TEST_RETRY_CAP}) exhausted on last task → __end__"
            )
            return {
                "routing_target": "__end__",
                "prev_test_results": curr_results,
                "current_task_index": next_idx,
            }
        else:
            next_tid = execution_order[next_idx] if next_idx < len(execution_order) else f"t{next_idx + 1}"
            logger.warning(
                f"[test_route] retry cap ({TEST_RETRY_CAP}) exhausted "
                f"→ skipping to next task {next_tid}"
            )
            return {
                "routing_target": "inject_task_context",
                "prev_test_results": curr_results,
                "current_task_index": next_idx,
            }

    # ── Compound regression check ──
    prev_results = state.get("prev_test_results")
    prev_hash = state.get("prev_snapshot_hash")
    is_regression = _check_regression(prev_results, curr_results)

    if is_regression and prev_hash and working_dir:
        logger.warning(f"[test_route] REGRESSION detected, restoring stash {prev_hash[:8]}")
        _stash_restore(working_dir, prev_hash)

    # ── Capture intent snippet before clearing session ──
    intent_snippet = _extract_intent_snippet(state)

    # ── Build deterministic summary ──
    diff_sum = _stash_diff_summary(working_dir, prev_hash) if not is_regression else ""
    summary = _build_deterministic_summary(
        diff_summary=diff_sum,
        test_results=curr_results,
        test_output=output,
        intent_snippet=intent_snippet,
        attempt=retry_count + 1,
    )

    # ── Build fresh context message ──
    msg_lines = [summary]
    msg_lines.append("\n## Your Task")

    if is_regression:
        msg_lines.append(
            "**WARNING: Your last change caused a REGRESSION (previously-passing tests now fail). "
            "The code has been rolled back to the last good state.**"
        )
        msg_lines.append("Try a different approach this time.")
    else:
        msg_lines.append("1. Use `read_file` to examine relevant source files (note line numbers).")
        msg_lines.append("2. Fix the failing tests using `replace_lines` or `write_file`.")
        msg_lines.append("3. Tests will be re-run automatically after you finish.")

    # ── Session reset + state updates ──
    updates: dict = {
        "routing_target": "code_gen",
        "retry_count": retry_count + 1,
        "prev_test_results": curr_results,
        "intent_snippet": intent_snippet,
        "messages": [HumanMessage(content="\n".join(msg_lines))],
    }
    updates.update(_clear_executor_session(state))
    return updates
