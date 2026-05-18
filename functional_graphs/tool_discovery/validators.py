"""
Tool Discovery — DETERMINISTIC node functions.

DeterministicNode 按 node id 查找同名函数：
  - search_aggregate(state) → dict   GitHub API + Web 搜索聚合
  - sandbox_eval(state) → dict        沙箱评估（Docker / venv / 静态分析降级）

每个函数签名: (state: dict) -> dict
"""

import json
import logging
import os
import re
import shutil
import subprocess
import tempfile
import urllib.request
import urllib.parse
from pathlib import Path

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

GITHUB_API = "https://api.github.com"


def _github_headers() -> dict:
    """Build GitHub API headers with optional token."""
    headers = {
        "Accept": "application/vnd.github+json",
        "User-Agent": "ZenithLoom-ToolDiscovery/1.0",
    }
    token = os.environ.get("GITHUB_TOKEN", "")
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return headers


def _github_search(query: str, per_page: int = 10) -> list[dict]:
    """Search GitHub repositories. Returns list of repo dicts."""
    params = urllib.parse.urlencode({
        "q": query,
        "sort": "stars",
        "order": "desc",
        "per_page": per_page,
    })
    url = f"{GITHUB_API}/search/repositories?{params}"
    req = urllib.request.Request(url, headers=_github_headers())

    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read().decode())
            return data.get("items", [])
    except Exception as e:
        logger.warning(f"[search_aggregate] GitHub search failed for '{query}': {e}")
        return []


def _normalize_candidate(item: dict) -> dict:
    """Normalize a GitHub API repo item to our CandidateRepo schema."""
    return {
        "repo": item.get("full_name", ""),
        "url": item.get("html_url", ""),
        "stars": item.get("stargazers_count", 0),
        "forks": item.get("forks_count", 0),
        "last_commit": item.get("pushed_at", ""),
        "license": (item.get("license") or {}).get("spdx_id", "unknown"),
        "language": item.get("language", "unknown"),
        "description": item.get("description", "") or "",
        "topics": item.get("topics", []),
        "open_issues": item.get("open_issues_count", 0),
        "archived": item.get("archived", False),
    }


def _dedup_candidates(candidates: list[dict]) -> list[dict]:
    """Deduplicate by repo full_name, keeping first occurrence (highest stars)."""
    seen = set()
    result = []
    for c in candidates:
        key = c["repo"].lower()
        if key not in seen:
            seen.add(key)
            result.append(c)
    return result


def _extract_json_from_message(state: dict) -> str:
    """Extract the last AI message content from state messages."""
    messages = state.get("messages", [])
    for msg in reversed(messages):
        content = getattr(msg, "content", "")
        if content:
            return content
    return ""


def _parse_json_from_llm(text: str) -> dict | list | None:
    """Parse JSON from LLM output, handling markdown code fences."""
    # Try to find JSON in code fence
    match = re.search(r"```(?:json)?\s*\n?([\s\S]*?)\n?```", text)
    if match:
        text = match.group(1)

    # Direct parse
    try:
        return json.loads(text.strip())
    except json.JSONDecodeError:
        return None


# ---------------------------------------------------------------------------
# search_aggregate — DETERMINISTIC node
# ---------------------------------------------------------------------------

def search_aggregate(state: dict) -> dict:
    """
    GitHub API 搜索聚合节点。

    读取上游 query_expand 的 JSON 输出（messages 中最后一条 AI 消息），
    执行多个 GitHub 搜索查询，聚合去重，写入 raw_candidates。
    """
    config_str = state.get("discovery_config", "")
    config = {}
    if config_str:
        try:
            config = json.loads(config_str)
        except json.JSONDecodeError:
            pass
    depth = config.get("depth", 5)  # Max candidates per query

    errors = []

    # Parse query_expand output from last message
    last_content = _extract_json_from_message(state)
    search_intent = _parse_json_from_llm(last_content)

    if not search_intent or not isinstance(search_intent, dict):
        errors.append(f"Failed to parse search_intent from query_expand output: {last_content[:200]}")
        return {
            "search_intent": last_content,
            "raw_candidates": "[]",
            "discovery_errors": json.dumps(errors, ensure_ascii=False),
        }

    # Execute GitHub searches
    all_candidates = []

    github_queries = search_intent.get("github_queries", [])
    keywords = search_intent.get("keywords", [])

    # GitHub structured queries
    for query in github_queries[:5]:  # Cap at 5 queries
        items = _github_search(query, per_page=depth)
        for item in items:
            if not item.get("archived", False):
                all_candidates.append(_normalize_candidate(item))

    # Keyword fallback search
    for kw in keywords[:3]:
        if not all_candidates:  # Only if structured queries yielded nothing
            items = _github_search(kw, per_page=depth)
            for item in items:
                if not item.get("archived", False):
                    all_candidates.append(_normalize_candidate(item))

    # Deduplicate
    unique = _dedup_candidates(all_candidates)

    logger.info(
        f"[search_aggregate] {len(github_queries)} queries → "
        f"{len(all_candidates)} raw → {len(unique)} unique candidates"
    )

    from langchain_core.messages import AIMessage

    # Build context message for candidate_filter
    user_query = state.get("user_query", "") or state.get("routing_context", "")
    filter_prompt = (
        f"用户需求：{user_query}\n\n"
        f"搜索意图：{json.dumps(search_intent, ensure_ascii=False)}\n\n"
        f"候选仓库（共 {len(unique)} 个）：\n"
        f"{json.dumps(unique, ensure_ascii=False, indent=2)}"
    )

    return {
        "messages": [AIMessage(content=filter_prompt)],
        "search_intent": json.dumps(search_intent, ensure_ascii=False),
        "raw_candidates": json.dumps(unique, ensure_ascii=False),
        "discovery_errors": json.dumps(errors, ensure_ascii=False) if errors else "",
    }


# ---------------------------------------------------------------------------
# sandbox_eval — DETERMINISTIC node
# ---------------------------------------------------------------------------

def _detect_project_type(repo_dir: Path) -> str:
    """Detect project type from files in cloned repo."""
    if (repo_dir / "pyproject.toml").exists() or (repo_dir / "setup.py").exists():
        return "python"
    if (repo_dir / "package.json").exists():
        return "node"
    if (repo_dir / "Cargo.toml").exists():
        return "rust"
    if (repo_dir / "go.mod").exists():
        return "go"
    return "unknown"


def _count_code_lines(repo_dir: Path, lang: str) -> int:
    """Rough line count for primary language files."""
    exts = {
        "python": [".py"],
        "node": [".js", ".ts", ".mjs"],
        "rust": [".rs"],
        "go": [".go"],
    }.get(lang, [".py", ".js"])
    count = 0
    for ext in exts:
        for f in repo_dir.rglob(f"*{ext}"):
            try:
                count += sum(1 for _ in f.open())
            except Exception:
                pass
    return count


def _static_scan_suspicious(repo_dir: Path) -> list[str]:
    """
    Static scan for suspicious patterns in setup/build files.
    Returns list of warnings.
    """
    warnings = []
    suspicious_patterns = [
        r"\bos\.system\b",
        r"\bsubprocess\b",
        r"\beval\b\(",
        r"\bexec\b\(",
        r"\b__import__\b",
    ]
    scan_files = [
        "setup.py", "pyproject.toml", "setup.cfg",
        "Makefile", "install.sh", "postinstall.sh",
    ]
    for fname in scan_files:
        fpath = repo_dir / fname
        if fpath.exists():
            try:
                content = fpath.read_text(errors="replace")
                for pattern in suspicious_patterns:
                    if re.search(pattern, content):
                        warnings.append(f"Suspicious pattern '{pattern}' in {fname}")
            except Exception:
                pass
    return warnings


def _eval_single_repo_static(repo_url: str, repo_name: str) -> dict:
    """
    Static-only evaluation for languages without Docker sandbox support.
    Clone → detect type → count lines → check docs/examples.
    """
    result = {
        "repo": repo_name,
        "clone_ok": False,
        "install_ok": False,
        "install_time_s": 0.0,
        "test_count": 0,
        "test_pass_rate": 0.0,
        "dependency_count": 0,
        "code_lines": 0,
        "has_docs": False,
        "has_examples": False,
        "project_type": "unknown",
        "eval_mode": "static",
        "errors": [],
        "security_warnings": [],
    }

    with tempfile.TemporaryDirectory(prefix="bb_static_") as tmpdir:
        repo_dir = Path(tmpdir) / repo_name.replace("/", "_")
        try:
            subprocess.run(
                ["git", "clone", "--depth=1", repo_url, str(repo_dir)],
                capture_output=True, text=True, timeout=60,
            )
            if not repo_dir.exists():
                result["errors"].append("Clone produced no directory")
                return result
            result["clone_ok"] = True
        except subprocess.TimeoutExpired:
            result["errors"].append("Clone timed out (60s)")
            return result
        except Exception as e:
            result["errors"].append(f"Clone failed: {e}")
            return result

        project_type = _detect_project_type(repo_dir)
        result["project_type"] = project_type
        result["security_warnings"] = _static_scan_suspicious(repo_dir)
        result["code_lines"] = _count_code_lines(repo_dir, project_type)
        result["has_docs"] = (repo_dir / "docs").is_dir() or (repo_dir / "README.md").exists()
        result["has_examples"] = (repo_dir / "examples").is_dir() or (repo_dir / "example").is_dir()
        result["errors"].append(f"No Docker sandbox for {project_type}, static analysis only")

    return result


def _eval_single_repo_docker(repo_url: str, repo_name: str, project_type: str, timeout: int = 300) -> dict:
    """
    Evaluate a single repo using Docker sandbox (--rm: auto-cleanup).

    Steps: clone → install (with deps) → test → collect metrics.
    Container is always removed after execution via --rm.
    """
    result = {
        "repo": repo_name,
        "clone_ok": False,
        "install_ok": False,
        "install_time_s": 0.0,
        "test_count": 0,
        "test_pass_rate": 0.0,
        "dependency_count": 0,
        "code_lines": 0,
        "has_docs": False,
        "has_examples": False,
        "project_type": project_type,
        "eval_mode": "docker",
        "errors": [],
        "security_warnings": [],
    }

    # Choose Docker image
    image_map = {
        "python": "bb-sandbox-python:latest",
        "node": "bb-sandbox-node:latest",
    }
    image = image_map.get(project_type)
    if not image:
        return None  # caller falls back to static

    # Check if image exists
    check = subprocess.run(
        ["docker", "image", "inspect", image],
        capture_output=True, text=True,
    )
    if check.returncode != 0:
        result["errors"].append(f"Docker image {image} not found")
        return None  # caller falls back to static

    # Build install+test script per project type
    if project_type == "python":
        eval_script = (
            "cd /workspace && git clone --depth=1 {url} repo && cd repo && "
            "echo '===CLONE_OK===' && "
            # Install with full dependencies
            "if [ -f requirements.txt ]; then "
            "  pip install -r requirements.txt -q 2>&1; "
            "elif [ -f pyproject.toml ] || [ -f setup.py ]; then "
            "  pip install -e . -q 2>&1; "
            "fi && "
            "echo '===INSTALL_OK===' && "
            # Metrics
            "pip freeze 2>/dev/null | wc -l && echo '===DEP_COUNT===' && "
            "find . -name '*.py' | xargs wc -l 2>/dev/null | tail -1 && echo '===CODE_LINES===' && "
            "[ -d docs ] || [ -f README.md ] && echo '===HAS_DOCS===' || true && "
            "[ -d examples ] || [ -d example ] && echo '===HAS_EXAMPLES===' || true && "
            # Tests
            "python -m pytest --tb=no -q --no-header 2>&1; echo \"===TEST_EXIT=$?===\""
        ).format(url=repo_url)
    else:  # node
        eval_script = (
            "cd /workspace && git clone --depth=1 {url} repo && cd repo && "
            "echo '===CLONE_OK===' && "
            "npm install 2>&1 && "
            "echo '===INSTALL_OK===' && "
            "find . -name '*.js' -o -name '*.ts' -o -name '*.mjs' | xargs wc -l 2>/dev/null | tail -1 && echo '===CODE_LINES===' && "
            "[ -d docs ] || [ -f README.md ] && echo '===HAS_DOCS===' || true && "
            "[ -d examples ] || [ -d example ] && echo '===HAS_EXAMPLES===' || true && "
            "npm test 2>&1; echo \"===TEST_EXIT=$?===\""
        ).format(url=repo_url)

    docker_cmd = [
        "docker", "run", "--rm",
        "--memory=2g", "--cpus=1", "--pids-limit=512",
        "--tmpfs", "/workspace:rw,size=1g,uid=1000",
        "--tmpfs", "/tmp:rw,size=256m,uid=1000",
        "--network=bridge",
        image,
        "/bin/bash", "-c", eval_script,
    ]

    try:
        proc = subprocess.run(
            docker_cmd,
            capture_output=True, text=True, timeout=timeout,
        )
        output = proc.stdout
        stderr = proc.stderr

        result["clone_ok"] = "===CLONE_OK===" in output
        result["install_ok"] = "===INSTALL_OK===" in output
        result["has_docs"] = "===HAS_DOCS===" in output
        result["has_examples"] = "===HAS_EXAMPLES===" in output

        # Parse dependency count
        dep_match = re.search(r"(\d+)\n===DEP_COUNT===", output)
        if dep_match:
            result["dependency_count"] = int(dep_match.group(1))

        # Parse code lines (wc -l total line: "  12345 total")
        code_match = re.search(r"(\d+)\s+total\n===CODE_LINES===", output)
        if code_match:
            result["code_lines"] = int(code_match.group(1))

        # Parse test results
        m_passed = re.search(r"(\d+) passed", output)
        m_failed = re.search(r"(\d+) failed", output)
        passed = int(m_passed.group(1)) if m_passed else 0
        failed = int(m_failed.group(1)) if m_failed else 0
        total = passed + failed
        if total > 0:
            result["test_count"] = total
            result["test_pass_rate"] = round(passed / total, 2)

        if not result["install_ok"]:
            # Capture install error from stderr/stdout
            err_preview = (stderr or output)[-500:]
            result["errors"].append(f"Install failed: {err_preview}")

    except subprocess.TimeoutExpired:
        result["errors"].append(f"Docker evaluation timed out ({timeout}s)")
    except Exception as e:
        result["errors"].append(f"Docker evaluation error: {e}")

    return result


def _is_docker_available() -> bool:
    """Check if Docker is available and running."""
    try:
        result = subprocess.run(
            ["docker", "info"],
            capture_output=True, text=True, timeout=5,
        )
        return result.returncode == 0
    except Exception:
        return False


def sandbox_eval(state: dict) -> dict:
    """
    沙箱评估节点。

    读取上游 candidate_score 的 JSON 输出（{dimensions, candidates}），
    对每个候选执行：克隆 → 安装 → 测试。

    Docker 可用时使用 Docker 沙箱，否则降级到 venv + 静态分析。
    """
    errors = json.loads(state.get("discovery_errors", "[]") or "[]")
    config_str = state.get("discovery_config", "")
    config = {}
    if config_str:
        try:
            config = json.loads(config_str)
        except json.JSONDecodeError:
            pass
    timeout = config.get("timeout", 120)

    # Parse filtered candidates from last AI message
    # candidate_filter outputs {dimensions, candidates} or legacy [candidate, ...]
    last_content = _extract_json_from_message(state)
    parsed = _parse_json_from_llm(last_content)

    if isinstance(parsed, dict) and "candidates" in parsed:
        filtered = parsed["candidates"]
    elif isinstance(parsed, list):
        filtered = parsed
    else:
        filtered = None

    if not filtered or not isinstance(filtered, list):
        errors.append(f"Failed to parse filtered_candidates: {last_content[:200]}")
        from langchain_core.messages import AIMessage
        return {
            "messages": [AIMessage(content=f"评估失败：无法解析候选列表。\n错误: {errors}")],
            "filtered_candidates": last_content,
            "evaluation_results": "[]",
            "discovery_errors": json.dumps(errors, ensure_ascii=False),
        }

    if not _is_docker_available():
        errors.append("Docker is not available — sandbox evaluation requires Docker")
        from langchain_core.messages import AIMessage
        return {
            "messages": [AIMessage(content=f"评估失败：Docker 不可用。\n错误: {errors}")],
            "filtered_candidates": json.dumps(filtered, ensure_ascii=False),
            "evaluation_results": "[]",
            "discovery_errors": json.dumps(errors, ensure_ascii=False),
        }

    eval_results = []

    for candidate in filtered[:5]:  # Max 5 repos
        repo_name = candidate.get("repo", "")
        repo_url = candidate.get("url", "")
        if not repo_url:
            repo_url = f"https://github.com/{repo_name}.git"
        elif not repo_url.endswith(".git"):
            repo_url = repo_url + ".git"

        # Map language to Docker sandbox type
        project_type = candidate.get("language", "unknown").lower()
        project_type = {"javascript": "node", "typescript": "node"}.get(
            project_type, project_type
        )

        logger.info(f"[sandbox_eval] evaluating {repo_name} type={project_type}")

        # Docker evaluation; returns None if no sandbox image for this language
        result = _eval_single_repo_docker(repo_url, repo_name, project_type, timeout)
        if result is None:
            # No Docker image for this language → static analysis only
            result = _eval_single_repo_static(repo_url, repo_name)

        eval_results.append(result)

    logger.info(f"[sandbox_eval] evaluated {len(eval_results)} repos")

    # Build context message for report_gen
    user_query = state.get("user_query", "") or state.get("routing_context", "")
    from langchain_core.messages import AIMessage

    # Include evaluation dimensions if available (from candidate_filter's structured output)
    dimensions = parsed.get("dimensions", []) if isinstance(parsed, dict) else []
    dimensions_section = ""
    if dimensions:
        dimensions_section = f"评估维度：\n{json.dumps(dimensions, ensure_ascii=False, indent=2)}\n\n"

    report_context = (
        f"用户需求：{user_query}\n\n"
        f"{dimensions_section}"
        f"筛选后的候选工具：\n{json.dumps(filtered, ensure_ascii=False, indent=2)}\n\n"
        f"沙箱评估结果：\n{json.dumps(eval_results, ensure_ascii=False, indent=2)}"
    )

    return {
        "messages": [AIMessage(content=report_context)],
        "filtered_candidates": json.dumps(filtered, ensure_ascii=False),
        "evaluation_results": json.dumps(eval_results, ensure_ascii=False),
        "discovery_errors": json.dumps(errors, ensure_ascii=False) if errors else "",
    }
