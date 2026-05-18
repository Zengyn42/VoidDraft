"""
Jei Atomize 端到端测试 — 验证 Jei 的完整 atomize 能力。

测试流程（三步 pipeline）：
  A1 — atomize_scan:    Jei 扫描靶文档，返回 section 结构
  A2 — atomize_propose: Jei 分配 KNOW-ID、提出语义 claims（含 title/body/ontology_type）
  A3 — atomize_apply:   Jei 执行 apply，创建 KNOW 文件并 patch 源文档

靶文档：实验/atomize-test-doc.md（3 个明确章节，内容安全可修改）

PASS 标准：
  A1: 响应包含 scan_id 或 section 相关信息
  A2: 响应包含 KNOW- ID 或 proposal_id 相关信息
  A3: 响应包含 apply / created / 成功 等确认词

Usage:
    systemctl --user stop jei
    cd /path/to/VoidDraft/tests
    python3 run_jei_atomize_etest.py
    systemctl --user start jei

注意：A3 会实际修改 vault（创建 KNOW 文件 + patch 源文档）。
      测试后如需还原，删除 knowledge/ 下新建的 KNOW 文件并恢复源文档。
"""
import asyncio
import sys
import os
import logging
from pathlib import Path

framework_dir = Path(__file__).parent.parent.parent / "ZenithLoom"
os.chdir(framework_dir)
sys.path.insert(0, str(framework_dir))

logging.basicConfig(level=logging.WARNING)
logging.getLogger("httpx").setLevel(logging.ERROR)
logging.getLogger("httpcore").setLevel(logging.ERROR)

from framework.loader import EntityLoader

BLUEPRINT_DIR = Path(__file__).parent.parent / "role_agents/knowledge_curator"
DATA_DIR      = Path(__file__).parent.parent.parent / "EdenGateway/agents/jei"
TARGET_DOC    = "实验/atomize-test-doc.md"

# ── 断言关键词 ─────────────────────────────────────────────────────────────────
A1_PASS_KEYWORDS = ["scan_id", "section", "扫描", "章节", "结构", "heading"]
A2_PASS_KEYWORDS = ["KNOW-", "proposal_id", "claim", "知识点", "已分配", "propose"]
A3_PASS_KEYWORDS = ["apply", "applied", "created", "成功", "已创建", "已应用", "写入", "完成"]

# ── 颜色输出 ──────────────────────────────────────────────────────────────────
def green(s): return f"\033[92m{s}\033[0m"
def yellow(s): return f"\033[93m{s}\033[0m"
def red(s): return f"\033[91m{s}\033[0m"


def verdict(response: str, pass_kws: list[str], label: str):
    if not response or response.strip() == "":
        print(red(f"VERDICT: FAIL [{label}]  reason=empty response"))
        return "FAIL"
    hits = [kw for kw in pass_kws if kw.lower() in response.lower()]
    if hits:
        print(green(f"VERDICT: PASS [{label}]  matched={hits[:3]}"))
        return "PASS"
    else:
        print(yellow(f"VERDICT: WARN [{label}]  none of {pass_kws[:4]} found in response"))
        return "WARN"


async def run_test(controller, question: str, label: str, pass_kws: list[str]) -> tuple[str, str]:
    """Run a single question through Jei, return (verdict, response)."""
    print(f"\nQ: {question}")
    print("NOTE: " + label)
    print("=" * 70)

    try:
        response = await asyncio.wait_for(controller.run(question), timeout=240)
    except asyncio.TimeoutError:
        print(red("TIMEOUT after 240s"))
        v = verdict("", pass_kws, label)
        return "FAIL", ""
    response = (response or "").strip()

    print(f"RESPONSE:\n{response}\n")
    v = verdict(response, pass_kws, label)
    return v, response


async def main():
    results = {}

    # ── A1: atomize_scan ───────────────────────────────────────────────────────
    print("\n" + "=" * 70)
    print(f"TEST A1: atomize_scan — 扫描靶文档结构")
    print(f"TARGET: {TARGET_DOC}")
    print("=" * 70)

    loader = EntityLoader(BLUEPRINT_DIR, data_dir=DATA_DIR)
    await loader.start_mcp_servers()
    controller = await loader.get_controller()

    # Force fresh session — no prior context from old Discord sessions
    session_name = f"smoke_atomize_{int(__import__('time').time())}"
    await controller.new_session(session_name)
    print(f"[smoke] fresh session: {session_name}", flush=True)

    q_a1 = (
        f"请用 atomize_scan 工具扫描文档「{TARGET_DOC}」，"
        f"告诉我它有几个 section，每个 section 的标题和 section_id 是什么，"
        f"以及返回的 scan_id 是什么。不要进行任何修改，只扫描。"
    )
    v_a1, r_a1 = await run_test(controller, q_a1, "A1 — atomize_scan", A1_PASS_KEYWORDS)
    results["A1"] = v_a1

    # ── A2: atomize_propose ────────────────────────────────────────────────────
    print("\n" + "=" * 70)
    print(f"TEST A2: atomize_propose — 提出语义 claims")
    print("=" * 70)

    q_a2 = (
        f"请对刚才扫描的文档「{TARGET_DOC}」进行 atomize_propose。"
        f"步骤：\n"
        f"1. 用 alloc_knowledge_id(count=3) 分配 3 个 KNOW ID\n"
        f"2. 为每个 section 构造一个 claim（title、body、ontology_type），"
        f"   ontology_type 从 fact/concept/process/decision 中选择合适的\n"
        f"3. 调用 atomize_propose 提交，告诉我 proposal_id\n"
        f"不要执行 apply，只到 propose 这步。"
    )
    v_a2, r_a2 = await run_test(controller, q_a2, "A2 — atomize_propose", A2_PASS_KEYWORDS)
    results["A2"] = v_a2

    # ── A3: atomize_apply ──────────────────────────────────────────────────────
    print("\n" + "=" * 70)
    print(f"TEST A3: atomize_apply — 执行写入")
    print("⚠️  本步骤会实际修改 vault（创建 KNOW 文件 + patch 源文档）")
    print("=" * 70)

    q_a3 = (
        f"请用 atomize_apply 工具执行刚才的 proposal，"
        f"告诉我创建了哪些 KNOW 文件（路径），以及源文档是否已被 patch。"
    )
    v_a3, r_a3 = await run_test(controller, q_a3, "A3 — atomize_apply", A3_PASS_KEYWORDS)
    results["A3"] = v_a3

    # ── 验证 KNOW 文件实际创建 ─────────────────────────────────────────────────
    print("\n" + "=" * 70)
    print("POST-CHECK: 验证 vault/knowledge/ 下新增 KNOW 文件")
    print("=" * 70)
    know_dir = Path(__file__).parent.parent.parent / "NimbusVault/knowledge"
    know_files = sorted(know_dir.glob("KNOW-*.md"))
    print(f"当前 KNOW 文件数量: {len(know_files)}")
    for f in know_files[-5:]:
        print(f"  {f.name}")

    target_path = Path(__file__).parent.parent.parent / "NimbusVault" / TARGET_DOC
    content = target_path.read_text(encoding="utf-8")
    has_patch = "knowledge_id:" in content or "KNOW-" in content
    if has_patch:
        print(green("SOURCE DOC PATCH: ✅ 源文档已包含 KNOW 引用"))
    else:
        print(yellow("SOURCE DOC PATCH: ⚠️  源文档未检测到 KNOW 引用（可能 patch 格式不同）"))

    # ── 汇总 ──────────────────────────────────────────────────────────────────
    print("\n" + "=" * 70)
    print("ATOMIZE TEST SUMMARY")
    print("=" * 70)
    icons = {"PASS": "✅", "WARN": "⚠️ ", "FAIL": "❌"}
    labels = {
        "A1": "atomize_scan  — 扫描 section 结构",
        "A2": "atomize_propose — 分配 ID + 提交 claims",
        "A3": "atomize_apply   — 写入 KNOW 文件 + patch 源文档",
    }
    all_pass = True
    for k, v in results.items():
        icon = icons.get(v, "?")
        suffix = "" if v == "PASS" else f"  → {v}"
        print(f"  {icon} {k}  {labels[k]}{suffix}")
        if v == "FAIL":
            all_pass = False

    print("=" * 70)
    if all_pass:
        print(green("RESULT: PASS ✅  Jei atomize 能力 A1/A2/A3 验证完成"))
    else:
        print(red("RESULT: 部分测试失败，请查看上方 TRIAGE"))

    print("""
Triage:
  A1 FAIL → atomize_scan 工具调用失败；检查 MCP server 是否正常、文档路径是否正确
  A1 WARN → Jei 未报告 scan_id；检查 Jei prompt 是否理解任务
  A2 FAIL → atomize_propose 失败；可能 scan_id 过期或 alloc_knowledge_id 报错
  A2 WARN → Jei 未返回 KNOW-ID；检查 alloc_knowledge_id 工具描述
  A3 FAIL → atomize_apply 失败；检查 proposal_id 是否正确传递
  A3 WARN → apply 可能成功但关键词未命中；检查 KNOW 文件是否实际创建
""")


if __name__ == "__main__":
    asyncio.run(main())
