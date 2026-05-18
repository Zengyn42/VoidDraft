"""
E2E tests for MCP servers — verify all MCP configurations are valid and servers start.

Tests:
  1. All MCP config files have valid paths
  2. MCP servers can be imported and started
  3. Vault paths are consistent across configs
"""

import asyncio
import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def mcp_json():
    """Load .mcp.json (Claude Code MCP config)."""
    p = PROJECT_ROOT / ".mcp.json"
    if not p.exists():
        pytest.skip(".mcp.json not found")
    return json.loads(p.read_text())


@pytest.fixture
def gemini_mcp():
    """Load .gemini/settings.json (Gemini CLI MCP config)."""
    p = PROJECT_ROOT / ".gemini" / "settings.json"
    if not p.exists():
        pytest.skip(".gemini/settings.json not found")
    return json.loads(p.read_text())


@pytest.fixture
def knowledge_shelf_entity():
    """Load knowledge_shelf entity.json."""
    p = PROJECT_ROOT / "blueprints" / "functional_graphs" / "knowledge_shelf" / "entity.json"
    if not p.exists():
        pytest.skip("knowledge_shelf entity.json not found")
    return json.loads(p.read_text())


def _extract_vault_path_from_args(args: list[str]) -> str | None:
    """Extract --vault path from MCP server args."""
    for i, a in enumerate(args):
        if a == "--vault" and i + 1 < len(args):
            return args[i + 1]
    return None


def _extract_all_vault_paths() -> dict[str, str]:
    """Collect vault paths from all config sources."""
    paths = {}

    # .mcp.json
    mcp_json_path = PROJECT_ROOT / ".mcp.json"
    if mcp_json_path.exists():
        data = json.loads(mcp_json_path.read_text())
        for name, cfg in data.get("mcpServers", {}).items():
            vp = _extract_vault_path_from_args(cfg.get("args", []))
            if vp:
                paths[f".mcp.json:{name}"] = vp

    # .gemini/settings.json
    gemini_path = PROJECT_ROOT / ".gemini" / "settings.json"
    if gemini_path.exists():
        data = json.loads(gemini_path.read_text())
        for name, cfg in data.get("mcpServers", {}).items():
            vp = _extract_vault_path_from_args(cfg.get("args", []))
            if vp:
                paths[f".gemini/settings.json:{name}"] = vp

    # obsidian server.py default
    server_py = PROJECT_ROOT / "mcp_servers" / "obsidian" / "server.py"
    if server_py.exists():
        text = server_py.read_text()
        for line in text.splitlines():
            if "VAULT_BASE_DIR" not in line and "EdenGateway/Vault" in line:
                # Extract hardcoded default path
                import re
                m = re.search(r'"(/[^"]+)"', line)
                if m:
                    paths["server.py:default"] = m.group(1)

    # knowledge_shelf rsync paths (WSL-side only, skip /mnt/ Windows paths)
    ks_path = PROJECT_ROOT / "blueprints" / "functional_graphs" / "knowledge_shelf" / "entity.json"
    if ks_path.exists():
        data = json.loads(ks_path.read_text())
        for node in data.get("graph", {}).get("nodes", []):
            nc = node.get("node_config", {})
            cmd = nc.get("command", [])
            if "rsync" in cmd:
                for arg in cmd:
                    if arg.startswith("/") and "Vault" in arg and not arg.startswith("/mnt/"):
                        paths[f"knowledge_shelf:rsync:{node['id']}"] = arg.rstrip("/")

    return paths


# ---------------------------------------------------------------------------
# Tests: Config Validity
# ---------------------------------------------------------------------------

class TestMCPConfigValidity:
    """Verify MCP config files reference valid paths and commands."""

    def test_mcp_json_vault_paths_exist(self, mcp_json):
        """All vault paths in .mcp.json must exist."""
        for name, cfg in mcp_json.get("mcpServers", {}).items():
            vault_path = _extract_vault_path_from_args(cfg.get("args", []))
            if vault_path:
                assert os.path.isdir(vault_path), (
                    f".mcp.json:{name} vault path does not exist: {vault_path}"
                )

    def test_gemini_mcp_vault_paths_exist(self, gemini_mcp):
        """All vault paths in .gemini/settings.json must exist."""
        for name, cfg in gemini_mcp.get("mcpServers", {}).items():
            vault_path = _extract_vault_path_from_args(cfg.get("args", []))
            if vault_path:
                assert os.path.isdir(vault_path), (
                    f".gemini/settings.json:{name} vault path does not exist: {vault_path}"
                )

    def test_mcp_json_commands_exist(self, mcp_json):
        """All commands in .mcp.json must be resolvable."""
        for name, cfg in mcp_json.get("mcpServers", {}).items():
            cmd = cfg.get("command", "")
            if cmd:
                import shutil
                assert shutil.which(cmd), (
                    f".mcp.json:{name} command not found: {cmd}"
                )

    def test_gemini_mcp_commands_exist(self, gemini_mcp):
        """All commands in .gemini/settings.json must be resolvable."""
        for name, cfg in gemini_mcp.get("mcpServers", {}).items():
            cmd = cfg.get("command", "")
            if cmd:
                import shutil
                assert shutil.which(cmd), (
                    f".gemini/settings.json:{name} command not found: {cmd}"
                )


# ---------------------------------------------------------------------------
# Tests: Path Consistency
# ---------------------------------------------------------------------------

class TestVaultPathConsistency:
    """All vault paths across configs must point to the same directory."""

    def test_all_vault_paths_consistent(self):
        """Every vault path reference must resolve to the same directory."""
        paths = _extract_all_vault_paths()
        if len(paths) < 2:
            pytest.skip("Less than 2 vault path references found")

        unique = set(p.rstrip("/") for p in paths.values())
        assert len(unique) == 1, (
            f"Inconsistent vault paths found:\n"
            + "\n".join(f"  {k}: {v}" for k, v in paths.items())
        )

    def test_mcp_json_matches_gemini_settings(self, mcp_json, gemini_mcp):
        """.mcp.json and .gemini/settings.json must have same vault paths."""
        mcp_paths = {}
        for name, cfg in mcp_json.get("mcpServers", {}).items():
            vp = _extract_vault_path_from_args(cfg.get("args", []))
            if vp:
                mcp_paths[name] = vp.rstrip("/")

        gemini_paths = {}
        for name, cfg in gemini_mcp.get("mcpServers", {}).items():
            vp = _extract_vault_path_from_args(cfg.get("args", []))
            if vp:
                gemini_paths[name] = vp.rstrip("/")

        for name in set(mcp_paths) & set(gemini_paths):
            assert mcp_paths[name] == gemini_paths[name], (
                f"Server '{name}' vault path mismatch:\n"
                f"  .mcp.json: {mcp_paths[name]}\n"
                f"  .gemini/settings.json: {gemini_paths[name]}"
            )


# ---------------------------------------------------------------------------
# Tests: MCP Server Startup
# ---------------------------------------------------------------------------

class TestMCPServerStartup:
    """Verify MCP servers can import and start without errors."""

    def test_obsidian_server_imports(self):
        """Obsidian MCP server module can be imported."""
        sys.path.insert(0, str(PROJECT_ROOT))
        try:
            from mcp_servers.obsidian.server import _vault  # noqa: F401
        except ImportError as e:
            if "mcp" in str(e).lower():
                pytest.skip(f"mcp package not installed: {e}")
            raise

    def test_heartbeat_server_imports(self):
        """Heartbeat MCP server module can be imported."""
        sys.path.insert(0, str(PROJECT_ROOT))
        try:
            from mcp_servers.heartbeat.server import mcp  # noqa: F401
        except ImportError as e:
            if "mcp" in str(e).lower():
                pytest.skip(f"mcp package not installed: {e}")
            raise

    def test_obsidian_server_starts(self):
        """Obsidian MCP server process starts and responds."""
        vault_paths = _extract_all_vault_paths()
        vault = None
        for k, v in vault_paths.items():
            if os.path.isdir(v):
                vault = v
                break
        if not vault:
            pytest.skip("No valid vault path found")

        proc = subprocess.Popen(
            [sys.executable, "-m", "mcp_servers.obsidian.server", "--vault", vault],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            cwd=str(PROJECT_ROOT),
        )
        time.sleep(2)

        # Server should still be running (waiting for stdio input)
        assert proc.poll() is None, (
            f"Obsidian MCP server exited immediately.\n"
            f"stderr: {proc.stderr.read().decode()[:500]}"
        )
        proc.terminate()
        proc.wait(timeout=5)

    def test_obsidian_server_rejects_bad_vault(self):
        """Obsidian MCP server rejects non-existent vault path."""
        proc = subprocess.run(
            [sys.executable, "-m", "mcp_servers.obsidian.server", "--vault", "/nonexistent/path"],
            capture_output=True,
            text=True,
            timeout=10,
            cwd=str(PROJECT_ROOT),
        )
        assert proc.returncode != 0, "Server should fail with bad vault path"
