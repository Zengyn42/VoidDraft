"""
Tests for EntityLoader.start_mcp_servers() and stop_mcp_servers().

EntityLoader reads entity.json (blueprint) at construction time for the "mcp"
array. MCPManager.acquire() handles process lifecycle; _connect_proxy() handles
optional proxy connections + tool registration.
"""

import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from framework.loader import EntityLoader

# ---------------------------------------------------------------------------
# Blueprint path (needs a real entity.json to exist for EntityLoader.__init__)
# ---------------------------------------------------------------------------

_BLUEPRINT_DIR = (
    Path(__file__).resolve().parent.parent
    / "role_agents" / "technical_architect"
)

# ---------------------------------------------------------------------------
# Fixture helpers
# ---------------------------------------------------------------------------

def _make_loader(tmp_path: Path, identity: dict | None = None) -> EntityLoader:
    """Create an EntityLoader with a synthetic identity.json in tmp_path."""
    if identity is not None:
        (tmp_path / "identity.json").write_text(
            json.dumps(identity), encoding="utf-8"
        )
    return EntityLoader(_BLUEPRINT_DIR, data_dir=tmp_path)


# ===========================================================================
# start_mcp_servers — MCPManager integration
# ===========================================================================

class TestStartMcpServers:
    @pytest.mark.asyncio
    async def test_acquires_all_mcp_specs(self, tmp_path):
        """MCPManager.acquire() is called for each entry in entity.json mcp array."""
        loader = _make_loader(tmp_path, identity={"name": "test_agent"})

        mock_mgr = MagicMock()
        mock_mgr.acquire = AsyncMock(return_value=True)

        with patch("framework.mcp_manager.MCPManager.get_instance", return_value=mock_mgr):
            started = await loader.start_mcp_servers()

        # Should have called acquire for each mcp entry in entity.json
        assert mock_mgr.acquire.call_count == len(loader._json.get("mcp", []))
        # All should be in started list
        expected_names = [s["name"] for s in loader._json.get("mcp", [])]
        assert started == expected_names

    @pytest.mark.asyncio
    async def test_failed_acquire_not_in_started_list(self, tmp_path):
        """If acquire returns False, that server is not in the started list."""
        loader = _make_loader(tmp_path, identity={"name": "test_agent"})

        mock_mgr = MagicMock()
        # Fail on first, succeed on rest
        mock_mgr.acquire = AsyncMock(side_effect=[False, True, True, True])

        with patch("framework.mcp_manager.MCPManager.get_instance", return_value=mock_mgr):
            started = await loader.start_mcp_servers()

        mcp_specs = loader._json.get("mcp", [])
        assert mcp_specs[0]["name"] not in started

    @pytest.mark.asyncio
    async def test_proxy_connected_for_agent_mail(self, tmp_path):
        """When spec has proxy="agent_mail", _connect_proxy is invoked."""
        loader = _make_loader(tmp_path, identity={"name": "test_agent"})

        mock_mgr = MagicMock()
        mock_mgr.acquire = AsyncMock(return_value=True)

        mock_proxy_instance = AsyncMock()
        mock_proxy_instance.connect = AsyncMock()
        mock_proxy_instance.register = AsyncMock()
        mock_proxy_class = MagicMock(return_value=mock_proxy_instance)

        with patch("framework.mcp_manager.MCPManager.get_instance", return_value=mock_mgr):
            with patch("framework.loader.entity_loader._resolve_proxy_class", return_value=mock_proxy_class):
                with patch("framework.loader.entity_loader._register_mcp_tools"):
                    started = await loader.start_mcp_servers()

        # agent_mail proxy should be stored
        assert "agent_mail" in loader._mcp_proxies
        mock_proxy_instance.connect.assert_called_once()
        mock_proxy_instance.register.assert_called_once_with("test_agent")

    @pytest.mark.asyncio
    async def test_no_proxy_for_non_proxy_specs(self, tmp_path):
        """MCP specs without "proxy" field don't trigger proxy connection."""
        loader = _make_loader(tmp_path, identity={"name": "test_agent"})

        mock_mgr = MagicMock()
        mock_mgr.acquire = AsyncMock(return_value=True)

        with patch("framework.mcp_manager.MCPManager.get_instance", return_value=mock_mgr):
            await loader.start_mcp_servers()

        # obsidian-vault, vault-sync, comfyui-video have no proxy field
        assert "obsidian-vault" not in loader._mcp_proxies
        assert "vault-sync" not in loader._mcp_proxies
        assert "comfyui-video" not in loader._mcp_proxies


# ===========================================================================
# stop_mcp_servers — proxy disconnect + MCPManager release
# ===========================================================================

class TestStopMcpServers:
    @pytest.mark.asyncio
    async def test_disconnects_all_proxies(self, tmp_path):
        loader = _make_loader(tmp_path, identity={"name": "test_agent"})

        proxy_a = AsyncMock()
        proxy_b = AsyncMock()
        loader._mcp_proxies = {"mcp_a": proxy_a, "mcp_b": proxy_b}

        mock_mgr = MagicMock()
        mock_mgr.release = AsyncMock()

        with patch("framework.mcp_manager.MCPManager.get_instance", return_value=mock_mgr):
            await loader.stop_mcp_servers()

        proxy_a.disconnect.assert_called_once()
        proxy_b.disconnect.assert_called_once()
        assert loader._mcp_proxies == {}

    @pytest.mark.asyncio
    async def test_releases_all_mcp_refs(self, tmp_path):
        loader = _make_loader(tmp_path, identity={"name": "test_agent"})

        mock_mgr = MagicMock()
        mock_mgr.release = AsyncMock()

        with patch("framework.mcp_manager.MCPManager.get_instance", return_value=mock_mgr):
            await loader.stop_mcp_servers()

        # Should release each MCP spec
        mcp_specs = loader._json.get("mcp", [])
        assert mock_mgr.release.call_count == len(mcp_specs)

    @pytest.mark.asyncio
    async def test_agent_mail_unregister_before_disconnect(self, tmp_path):
        loader = _make_loader(tmp_path, identity={"name": "my_agent"})

        mock_proxy = AsyncMock()
        mock_proxy.unregister = AsyncMock(return_value="ok")
        loader._mcp_proxies = {"agent_mail": mock_proxy}

        mock_mgr = MagicMock()
        mock_mgr.release = AsyncMock()

        with patch("framework.mcp_manager.MCPManager.get_instance", return_value=mock_mgr):
            await loader.stop_mcp_servers()

        mock_proxy.unregister.assert_called_once_with("my_agent")
        mock_proxy.disconnect.assert_called_once()

    @pytest.mark.asyncio
    async def test_disconnect_error_does_not_prevent_cleanup(self, tmp_path):
        """Even if disconnect() raises, _mcp_proxies is still cleared."""
        loader = _make_loader(tmp_path, identity={"name": "test_agent"})

        proxy_a = AsyncMock()
        proxy_a.disconnect.side_effect = RuntimeError("boom")
        loader._mcp_proxies = {"mcp_a": proxy_a}

        mock_mgr = MagicMock()
        mock_mgr.release = AsyncMock()

        with patch("framework.mcp_manager.MCPManager.get_instance", return_value=mock_mgr):
            await loader.stop_mcp_servers()

        assert loader._mcp_proxies == {}
