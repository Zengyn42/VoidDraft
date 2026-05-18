"""
框架导入与配置验证测试

不做实际 API 调用，只验证：
  - AgentLoader 能正确加载 Hani / Asa 配置
  - ClaudeNode / LlamaNode / GeminiNode 可实例化
  - call_llm 接口存在
  - db_path / sessions_file 路径在 agent 目录内

运行：python3 test_cli.py
"""

import asyncio
from pathlib import Path


def test_agent_loader_hani():
    print("--- AgentLoader: Hani ---")
    from framework.loader import EntityLoader
    from pathlib import Path as _Path
    data_dir = _Path.home() / "Foundation" / "EdenGateway" / "agents" / "hani"
    loader = EntityLoader(Path(__file__).parent.parent / "role_agents/technical_architect", data_dir=data_dir)
    assert loader.name == "hani", f"Expected 'hani', got '{loader.name}'"
    assert loader.json["llm"] == "claude"

    cfg = loader.load_config()
    assert cfg.db_path.endswith("hani.db"), f"db_path 应指向 EdenGateway: {cfg.db_path}"
    assert cfg.sessions_file.endswith("sessions.json")

    prompt = loader.load_system_prompt()
    assert len(prompt) > 100, "system_prompt 为空"
    print(f"   db_path: {cfg.db_path}")
    print(f"   system_prompt: {len(prompt)} chars")
    print("✅ AgentLoader Hani OK\n")


def test_agent_loader_asa():
    print("--- AgentLoader: Asa ---")
    from framework.loader import EntityLoader
    from pathlib import Path as _Path
    data_dir = _Path.home() / "Foundation" / "EdenGateway" / "agents" / "asa"
    loader = EntityLoader(Path(__file__).parent.parent / "role_agents/administrative_officer", data_dir=data_dir)
    assert loader.name == "asa", f"Expected 'asa', got '{loader.name}'"
    assert loader.json["llm"] == "llama"
    cfg = loader.load_config()
    assert cfg.db_path.endswith("asa.db"), f"db_path should end with asa.db: {cfg.db_path}"
    print(f"   llm: {loader.json['llm']}")
    print("✅ AgentLoader Asa OK\n")


def test_claude_node_interface():
    print("--- ClaudeNode 接口 ---")
    from framework.nodes.llm.claude import ClaudeNode
    from framework.config import AgentConfig
    node = ClaudeNode(AgentConfig(), {}, "test system prompt")
    assert hasattr(node, "call_claude"), "缺少 call_claude"
    assert hasattr(node, "call_llm"), "缺少 call_llm 标准接口"
    assert ClaudeNode.call_llm is ClaudeNode.call_claude, "call_llm 应是 call_claude 的别名"
    print("✅ ClaudeNode 接口 OK\n")


def test_llama_node_interface():
    print("--- LlamaNode 接口 ---")
    from framework.nodes.llm.ollama import LlamaNode
    from framework.config import AgentConfig
    node = LlamaNode(AgentConfig(), {})
    assert hasattr(node, "call_llm"), "缺少 call_llm"
    print("✅ LlamaNode 接口 OK\n")


def test_agent_node():
    print("--- AgentNode (通用节点) ---")
    from framework.nodes.llm.llm_node import LlmNode as AgentNode, AgentClaudeNode
    assert AgentClaudeNode is AgentNode, "AgentClaudeNode 向后兼容别名失效"
    print("✅ AgentNode + AgentClaudeNode 别名 OK\n")


def test_tool_rules():
    print("--- tool_rules 关键词匹配 ---")
    from framework.loader import EntityLoader
    from framework.nodes.llm.claude import ClaudeNode

    loader = EntityLoader(Path(__file__).parent.parent / "role_agents/technical_architect")
    cfg = loader.load_config()
    node_cfg = next(
        n for n in loader.json.get("graph", {}).get("nodes", []) if n.get("id") == "claude_main"
    )
    node = ClaudeNode(cfg, node_cfg)

    # 中文搜索关键词应触发 WebSearch/WebFetch
    tools = node._select_tools("帮我搜索一下最新的论文")
    assert "WebFetch" in tools or "WebSearch" in tools, f"搜索关键词应追加 WebFetch/WebSearch: {tools}"

    # 普通指令不应追加额外工具
    tools_normal = node._select_tools("帮我修改这个文件")
    assert "WebFetch" not in tools_normal, f"普通指令不应有 WebFetch: {tools_normal}"
    print(f"   搜索触发工具: {tools}")
    print("✅ tool_rules OK\n")


def test_hani_loader():
    print("--- AgentLoader for hani ---")
    from pathlib import Path
    from framework.loader import EntityLoader
    loader = EntityLoader(Path(__file__).parent.parent / "role_agents/technical_architect")
    assert callable(loader.get_engine)
    assert callable(loader.invalidate_engine)
    assert callable(loader.get_controller)
    print("✅ EntityLoader(hani) OK\n")


def test_awaken_arg_parsing():
    print("--- awaken.py --entity 解析 ---")
    import sys, importlib.util
    from pathlib import Path as _Path
    entity_path = str(_Path.home() / "Foundation" / "EdenGateway" / "agents" / "hani")
    orig = sys.argv[:]
    sys.argv = ["awaken.py", "--entity", entity_path, "--connector", "cli"]
    spec = importlib.util.spec_from_file_location("awaken", str(Path(__file__).parent.parent.parent / "ZenithLoom" / "awaken.py"))
    awk_mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(awk_mod)
    entity_p, connector, debug, _debug_output = awk_mod._parse_args()
    sys.argv = orig
    assert entity_p == entity_path, f"entity_path={entity_p}"
    assert connector == "cli", f"connector={connector}"
    assert debug is False
    print(f"   parsed: entity={entity_p} connector={connector} debug={debug}")
    print("✅ awaken.py 参数解析 OK\n")


if __name__ == "__main__":
    test_agent_loader_hani()
    test_agent_loader_asa()
    test_claude_node_interface()
    test_llama_node_interface()
    test_agent_node()
    test_tool_rules()
    test_hani_loader()
    test_awaken_arg_parsing()
    print("🎉 全部通过")
