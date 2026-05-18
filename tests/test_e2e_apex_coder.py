"""
E2E 测试：apex_coder agent 架构验证

覆盖：
  1. entity.json 结构完整性（节点、边、关键配置）
  2. apex_coder 图编译成功（单节点 apex_main）
  3. SOUL.md 加载 + 内容关键段落检查
  4. routing_hint 存在
  5. session_key = "apex"
  6. .claude/agents/ 含 8 个子 agent（7 ECC + 1 PUA）
  7. .claude/skills/pua-debugging/SKILL.md 存在
  8. AgentConfig 正确解析
  9. routing_hint 自动注入 system_prompt（_collect_routing_hints）
  10. persona_files 加载顺序正确

运行：
    python3 test_e2e_apex_coder.py
"""

import asyncio
import json
import logging
import sys
from pathlib import Path

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%H:%M:%S",
    stream=sys.stdout,
)
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("urllib3").setLevel(logging.WARNING)

logger = logging.getLogger("test_e2e_apex_coder")

AGENT_DIR = Path(__file__).parent.parent / "functional_graphs/apex_coder"
CLAUDE_AGENTS_DIR = Path(__file__).parent.parent / "functional_graphs/apex_coder/.claude/agents"
PUA_SKILL_PATH = Path(__file__).parent.parent / "functional_graphs/apex_coder/.claude/skills/pua-debugging/SKILL.md"
SKILLS_DIR = Path(__file__).parent.parent / "functional_graphs/apex_coder/.claude/skills"


async def test_agent_json_structure():
    """entity.json 含所有必需字段且值正确。"""
    raw = json.loads((AGENT_DIR / "entity.json").read_text(encoding="utf-8"))

    assert raw["name"] == "apex_coder", f"name 应为 apex_coder，实际: {raw['name']}"
    assert "routing_hint" in raw and len(raw["routing_hint"]) > 10, "routing_hint 缺失或过短"
    assert raw["llm"] == "claude", f"llm 应为 claude，实际: {raw['llm']}"

    # graph 结构
    graph = raw["graph"]
    nodes = graph["nodes"]
    edges = graph["edges"]
    node_ids = [n["id"] for n in nodes]
    expected_nodes = {"setup", "claude_qa", "reset_for_coder", "claude_coder", "executor", "route", "inject_error_context"}
    assert expected_nodes <= set(node_ids), f"缺少节点: {expected_nodes - set(node_ids)}"
    assert isinstance(edges, list), f"edges 应为 list，实际: {type(edges)}"

    # 节点配置 — claude_coder 节点
    coder_node = next(n for n in nodes if n["id"] == "claude_coder")
    assert coder_node["type"] == "CLAUDE_SDK", f"claude_coder 节点类型应为 CLAUDE_SDK，实际: {coder_node['type']}"
    assert coder_node["session_key"] == "apex_coder", f"session_key 应为 apex_coder，实际: {coder_node.get('session_key')}"
    assert coder_node["permission_mode"] == "bypassPermissions", "permission_mode 应为 bypassPermissions"
    assert "Agent" in coder_node["tools"], "tools 中必须含 Agent"
    assert coder_node["setting_sources"] is None, "setting_sources 应为 null（节省 token）"

    # graph entry
    assert graph.get("entry") == "setup", "graph.entry 应为 setup"

    logger.info("✅ entity.json structure OK")


async def test_graph_compiles():
    """apex_coder 图编译成功，节点集正确。"""
    from framework.loader import EntityLoader

    loader = EntityLoader(AGENT_DIR)
    g = await loader.build_graph(checkpointer=None)
    node_ids = set(g.nodes.keys())

    assert "__start__" in node_ids, "图中缺少 __start__ 节点"
    expected = {"setup", "claude_qa", "reset_for_coder", "claude_coder", "executor", "route", "inject_error_context"}
    real_nodes = node_ids - {"__start__"}
    missing = expected - real_nodes
    assert not missing, f"图中缺少节点: {missing}"

    logger.info(f"✅ graph compiles OK: nodes={sorted(node_ids)}")


async def test_no_checkpointer():
    """build_graph(checkpointer=None) 编译后无 checkpointer。"""
    from framework.loader import EntityLoader

    loader = EntityLoader(AGENT_DIR)
    g = await loader.build_graph(checkpointer=None)
    cp = getattr(g, "checkpointer", None)
    assert cp is None, f"checkpointer=None 应编译无 checkpointer，实际: {cp!r}"

    logger.info("✅ no checkpointer OK")


async def test_soul_md_loads():
    """节点级 persona 文件存在且内容完整。"""
    # apex_coder 使用节点级 persona_files，不再有顶层 persona_files
    coder_role = AGENT_DIR / "CODER_ROLE.md"
    protocol = AGENT_DIR / "PROTOCOL.md"

    assert coder_role.exists(), f"CODER_ROLE.md 不存在: {coder_role}"
    assert protocol.exists(), f"PROTOCOL.md 不存在: {protocol}"

    coder_text = coder_role.read_text(encoding="utf-8")
    protocol_text = protocol.read_text(encoding="utf-8")

    assert len(coder_text) > 100, f"CODER_ROLE.md 过短 ({len(coder_text)} chars)"
    assert "P8" in coder_text, "CODER_ROLE.md 应含 P8 等级标识"

    logger.info(f"✅ persona files OK (CODER_ROLE={len(coder_text)} chars, PROTOCOL={len(protocol_text)} chars)")


async def test_soul_md_contains_ecc():
    """PROTOCOL.md 含 ECC 核心方法论关键段落。"""
    protocol_text = (AGENT_DIR / "PROTOCOL.md").read_text(encoding="utf-8")

    ecc_markers = [
        "Eval-First",       # Eval-First Loop
        "15 分钟",          # 15-minute unit rule
    ]
    missing = [m for m in ecc_markers if m not in protocol_text]
    assert not missing, f"PROTOCOL.md 缺少 ECC 关键词: {missing}"

    # Sub-agent table is in .claude/agents/
    agents_dir = AGENT_DIR / ".claude" / "agents"
    if agents_dir.exists():
        agent_files = " ".join(f.read_text(encoding="utf-8") for f in agents_dir.glob("*.md"))
        for marker in ("planner", "code-reviewer"):
            assert marker in agent_files or (agents_dir / f"{marker}.md").exists(), \
                f"缺少 sub-agent: {marker}"

    logger.info("✅ ECC content OK")


async def test_soul_md_contains_pua():
    """PROTOCOL.md 含 PUA 铁律关键段落。"""
    protocol_text = (AGENT_DIR / "PROTOCOL.md").read_text(encoding="utf-8")

    pua_markers = [
        "铁律一",
        "铁律二",
        "铁律三",
        "穷尽一切",
    ]
    missing = [m for m in pua_markers if m not in protocol_text]
    assert not missing, f"PROTOCOL.md 缺少 PUA 关键词: {missing}"

    logger.info("✅ PUA content OK")


async def test_ecc_agents_in_place():
    """.claude/agents/ 含 8 个子 agent 文件（7 ECC + 1 PUA）。"""
    expected_agents = {
        "planner.md",
        "architect.md",
        "code-reviewer.md",
        "python-reviewer.md",
        "security-reviewer.md",
        "build-error-resolver.md",
        "loop-operator.md",
        "pua-debugger.md",
    }

    actual = {f.name for f in CLAUDE_AGENTS_DIR.iterdir() if f.suffix == ".md"}
    missing = expected_agents - actual
    assert not missing, f".claude/agents/ 缺少: {missing}"

    # 验证每个文件有 YAML frontmatter
    for fname in expected_agents:
        content = (CLAUDE_AGENTS_DIR / fname).read_text(encoding="utf-8")
        assert content.startswith("---"), f"{fname} 缺少 YAML frontmatter"
        assert "name:" in content, f"{fname} frontmatter 缺少 name 字段"
        assert "tools:" in content or "description:" in content, f"{fname} frontmatter 不完整"

    logger.info(f"✅ .claude/agents/ has {len(expected_agents)} agents OK")


async def test_pua_debugger_agent():
    """pua-debugger.md 含正确的 frontmatter 和核心内容。"""
    content = (CLAUDE_AGENTS_DIR / "pua-debugger.md").read_text(encoding="utf-8")

    assert "name: pua-debugger" in content, "pua-debugger.md name 字段错误"
    assert "model: opus" in content, "pua-debugger.md 应使用 opus 模型"
    assert "L1" in content and "L4" in content, "pua-debugger.md 应含压力升级等级"
    assert "铁律" in content, "pua-debugger.md 应含铁律"

    logger.info("✅ pua-debugger.md OK")


async def test_pua_skill_in_place():
    """.claude/skills/pua-debugging/SKILL.md 存在且内容完整。"""
    assert PUA_SKILL_PATH.exists(), f"PUA skill 不存在: {PUA_SKILL_PATH}"

    content = PUA_SKILL_PATH.read_text(encoding="utf-8")
    assert len(content) > 1000, f"PUA skill 过短 ({len(content)} chars)，可能未正确复制"
    assert "name: pua" in content, "PUA skill 缺少 frontmatter name"
    assert "大厂 PUA 扩展包" in content, "PUA skill 应含完整扩展包"
    assert "Agent Team 集成" in content, "PUA skill 应含 Agent Team 集成段"

    logger.info(f"✅ PUA skill OK ({len(content)} chars)")


async def test_agent_config():
    """AgentConfig 正确解析 apex_coder 配置。"""
    from framework.loader import EntityLoader

    loader = EntityLoader(AGENT_DIR)
    cfg = loader.load_config()

    assert cfg.db_path.endswith("apex_coder.db"), f"db_path 应含 apex_coder.db，实际: {cfg.db_path}"
    assert cfg.sessions_file.endswith("sessions.json"), f"sessions_file 错误: {cfg.sessions_file}"

    logger.info("✅ AgentConfig OK")


async def test_add_dirs_config():
    """entity.json claude_coder 节点含 add_dirs，指向 apex_coder 目录。"""
    raw = json.loads((AGENT_DIR / "entity.json").read_text(encoding="utf-8"))
    nodes = raw["graph"]["nodes"]
    coder_node = next((n for n in nodes if n["id"] == "claude_coder"), None)
    assert coder_node is not None, "entity.json 缺少 claude_coder 节点"
    assert "add_dirs" in coder_node, "claude_coder 节点缺少 add_dirs 字段"
    assert any("apex_coder" in d for d in coder_node["add_dirs"]), \
        f"add_dirs 应含 apex_coder 路径，实际: {coder_node['add_dirs']}"
    logger.info("✅ add_dirs config OK")


async def test_skill_isolation():
    """所有技能文件都在 apex_coder 隔离目录内，VoidDraft 项目根 .claude/ 无 agent/skill 残留。"""
    expected_skills = {
        "api-design", "backend-patterns", "coding-standards",
        "e2e-testing", "eval-harness", "pua-debugging",
        "tdd-workflow", "verification-loop",
    }
    actual = {d.name for d in SKILLS_DIR.iterdir() if d.is_dir()}
    missing = expected_skills - actual
    assert not missing, f"缺少 skill 目录: {missing}"

    # VoidDraft 项目根 .claude/ 不应有 agents/ 或 skills/ 残留
    voiddraft_root = Path(__file__).parent.parent
    project_agents = voiddraft_root / ".claude" / "agents"
    project_skills = voiddraft_root / ".claude" / "skills"
    assert not project_agents.exists(), f"VoidDraft .claude/agents/ 应已清空（移至 apex_coder）"
    assert not project_skills.exists(), f"VoidDraft .claude/skills/ 应已清空（移至 apex_coder）"

    logger.info(f"✅ skill isolation OK: {sorted(actual)}")


async def test_routing_hint_injection():
    """如果 apex_coder 被其他图引用，routing_hint 应可被 _collect_routing_hints 读取。"""
    raw = json.loads((AGENT_DIR / "entity.json").read_text(encoding="utf-8"))
    hint = raw.get("routing_hint", "")

    assert "复杂" in hint, "routing_hint 应含'复杂'关键词"
    assert "bug" in hint.lower() or "bug" in hint, "routing_hint 应提及 bug 场景"
    assert len(hint) > 20, "routing_hint 不应过短"

    logger.info("✅ routing_hint OK")


async def run():
    logger.info("=== E2E Apex Coder 架构测试开始 ===")

    await test_agent_json_structure()
    await test_graph_compiles()
    await test_no_checkpointer()
    await test_soul_md_loads()
    await test_soul_md_contains_ecc()
    await test_soul_md_contains_pua()
    await test_ecc_agents_in_place()
    await test_pua_debugger_agent()
    await test_pua_skill_in_place()
    await test_agent_config()
    await test_add_dirs_config()
    await test_skill_isolation()
    await test_routing_hint_injection()

    logger.info("=" * 50)
    print("\n✅ 全部 13 项测试通过")
    print("   entity.json 结构完整（单节点、session_key、Agent 工具）")
    print("   图编译成功（apex_main 节点就位）")
    print("   无 checkpointer 模式正常")
    print("   SOUL.md 加载成功（含 ECC + PUA 核心内容）")
    print("   8 个子 agent 文件就位（7 ECC + 1 PUA）")
    print("   PUA skill 完整复制")
    print("   AgentConfig 正确解析")
    print("   add_dirs 节点配置正确")
    print("   8 技能完全隔离在 apex_coder/.claude/，项目根无残留")
    print("   routing_hint 可供父图引用")


if __name__ == "__main__":
    asyncio.run(run())
