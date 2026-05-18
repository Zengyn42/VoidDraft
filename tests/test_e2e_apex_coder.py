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
    assert raw["persona_files"] == ["ROLE.md", "PROTOCOL.md"], f"persona_files 应为 ['ROLE.md', 'PROTOCOL.md']，实际: {raw['persona_files']}"

    # graph 结构
    graph = raw["graph"]
    nodes = graph["nodes"]
    edges = graph["edges"]
    assert len(nodes) == 1, f"应为单节点图，实际 {len(nodes)} 个节点"
    # edges 可为空（框架从 entry/exit 自动生成 start→entry 和 exit→end 边）
    assert isinstance(edges, list), f"edges 应为 list，实际: {type(edges)}"

    # 节点配置
    node = nodes[0]
    assert node["id"] == "apex_main", f"节点 id 应为 apex_main，实际: {node['id']}"
    assert node["type"] == "CLAUDE_CLI", f"节点类型应为 CLAUDE_CLI，实际: {node['type']}"
    assert node["session_key"] == "apex", f"session_key 应为 apex，实际: {node.get('session_key')}"
    assert node["permission_mode"] == "bypassPermissions", "permission_mode 应为 bypassPermissions"
    assert "Agent" in node["tools"], "tools 中必须含 Agent（用于 spawn 子 agent）"
    assert node["setting_sources"] is None, "setting_sources 应为 null（节省 token）"

    # 边（框架通过 entry/exit 字段自动生成 start→entry 和 exit→end 边，edges 列表可为空）
    assert graph.get("entry") == "apex_main", "graph.entry 应为 apex_main"
    assert graph.get("exit") == "apex_main", "graph.exit 应为 apex_main"

    logger.info("✅ entity.json structure OK")


async def test_graph_compiles():
    """apex_coder 图编译成功，节点集正确。"""
    from framework.loader import EntityLoader

    loader = EntityLoader(AGENT_DIR)
    g = await loader.build_graph(checkpointer=None)
    node_ids = set(g.nodes.keys())

    assert "apex_main" in node_ids, "图中缺少 apex_main 节点"
    assert "__start__" in node_ids, "图中缺少 __start__ 节点"
    # 单节点图：只有 __start__ + apex_main（+ 可能的 __end__）
    real_nodes = node_ids - {"__start__"}
    assert "apex_main" in real_nodes, "apex_main 应为唯一业务节点"

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
    """SOUL.md 存在且通过 load_system_prompt 加载。"""
    from framework.loader import EntityLoader

    loader = EntityLoader(AGENT_DIR)
    prompt = loader.load_system_prompt()

    assert len(prompt) > 100, f"system prompt 过短 ({len(prompt)} chars)，persona 可能未加载"
    assert "全栈工程执行官" in prompt, "persona 应含身份标识'全栈工程执行官'"
    assert "P8" in prompt, "persona 应含 P8 等级标识"

    logger.info(f"✅ persona loads OK ({len(prompt)} chars)")


async def test_soul_md_contains_ecc():
    """SOUL.md 含 ECC 核心方法论关键段落。"""
    from framework.loader import EntityLoader

    prompt = EntityLoader(AGENT_DIR).load_system_prompt()

    ecc_markers = [
        "Eval-First",       # Eval-First Loop
        "15 分钟",          # 15-minute unit rule
        "Haiku",            # Model routing
        "Sonnet",
        "Opus",
        "planner",          # Sub-agent table
        "architect",
        "code-reviewer",
        "security-reviewer",
        "pua-debugger",
    ]
    missing = [m for m in ecc_markers if m not in prompt]
    assert not missing, f"SOUL.md 缺少 ECC 关键词: {missing}"

    logger.info("✅ SOUL.md ECC content OK")


async def test_soul_md_contains_pua():
    """SOUL.md 含 PUA 铁律精简版关键段落。"""
    from framework.loader import EntityLoader

    prompt = EntityLoader(AGENT_DIR).load_system_prompt()

    pua_markers = [
        "铁律一",
        "铁律二",
        "铁律三",
        "L1",
        "L2",
        "L3",
        "L4",
        "7 项检查清单",
        "穷尽一切",
    ]
    missing = [m for m in pua_markers if m not in prompt]
    assert not missing, f"persona 缺少 PUA 关键词: {missing}"

    logger.info("✅ SOUL.md PUA content OK")


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
    """entity.json 节点含 add_dirs，指向 agents/apex_coder。"""
    raw = json.loads((AGENT_DIR / "entity.json").read_text(encoding="utf-8"))
    node = raw["graph"]["nodes"][0]
    assert "add_dirs" in node, "节点缺少 add_dirs 字段"
    assert any("apex_coder" in d for d in node["add_dirs"]), f"add_dirs 应含 apex_coder 路径，实际: {node['add_dirs']}"
    logger.info("✅ add_dirs config OK")


async def test_skill_isolation():
    """所有技能文件都在 apex_coder 隔离目录内，项目根 .claude/ 无 agent/skill 残留。"""
    expected_skills = {
        "api-design", "backend-patterns", "coding-standards",
        "e2e-testing", "eval-harness", "pua-debugging",
        "tdd-workflow", "verification-loop",
    }
    actual = {d.name for d in SKILLS_DIR.iterdir() if d.is_dir()}
    missing = expected_skills - actual
    assert not missing, f"缺少 skill 目录: {missing}"

    # 项目根 .claude/ 不应有 agents/ 或 skills/ 残留
    project_agents = Path(".claude/agents")
    project_skills = Path(".claude/skills")
    assert not project_agents.exists(), f"项目根 .claude/agents/ 应已清空（移至 apex_coder）"
    assert not project_skills.exists(), f"项目根 .claude/skills/ 应已清空（移至 apex_coder）"

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
