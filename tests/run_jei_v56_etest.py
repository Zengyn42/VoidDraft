"""
v5.6 smoke test runner for Jei — validates Graph Visualization via live Jei session.

Flow:

  S1 — Generate:   ask Jei to call generate_graph(namespace="nimbus")
                   → response should confirm graph.html path
  S2 — Filesystem: verify graph.html exists and contains Obsidian JS
                   (_prismNodeData, obsidian://, stabilizationIterationsDone)
  S3 — Portal:     verify portal nodes present (hexagon shape / portal_href)
                   if cross-namespace refs exist in the graph

Setup/teardown:
  - Removes generated graph.html after run (restores original if existed)

Usage:
    python3 run_jei_v56_etest.py
"""
import asyncio
import sys
import os
import logging
from pathlib import Path

framework_dir = Path(__file__).parent.parent.parent / "ZenithLoom"
prismrag_dir  = Path(__file__).parent.parent.parent / "PrismRag"
os.chdir(framework_dir)
sys.path.insert(0, str(framework_dir))
sys.path.insert(0, str(prismrag_dir))

logging.basicConfig(level=logging.WARNING)
logging.getLogger("httpx").setLevel(logging.ERROR)
logging.getLogger("httpcore").setLevel(logging.ERROR)

from framework.loader import EntityLoader

BLUEPRINT_DIR = Path("/home/kingy/Foundation/VoidDraft/role_agents/knowledge_curator")
DATA_DIR      = Path("/home/kingy/Foundation/EdenGateway/agents/jei")

# ── Stream capture ─────────────────────────────────────────────────────────────

_stream_buf: list[str] = []

def _stream_cb(text: str, is_thinking: bool = False) -> None:
    _stream_buf.append(text)
    print(text, end="", flush=True)

# ── Setup / Teardown ───────────────────────────────────────────────────────────

def _setup() -> dict:
    from prism_rag.config import PrismRagSettings
    settings   = PrismRagSettings()
    nimbus_src = next(s for s in settings.resolved_graphs if s.namespace == "nimbus")
    graph_html = nimbus_src.data_dir / "graph.html"
    original_html = graph_html.read_text(encoding="utf-8") if graph_html.exists() else None
    print(f"[setup] graph.html pre-exists: {graph_html.exists()}", flush=True)
    return {
        "graph_html": graph_html,
        "original_html": original_html,
    }


def _teardown(state: dict) -> None:
    graph_html = state["graph_html"]
    if state["original_html"] is None:
        if graph_html.exists():
            graph_html.unlink()
            print(f"[teardown] removed {graph_html}", flush=True)
    else:
        graph_html.write_text(state["original_html"], encoding="utf-8")
        print(f"[teardown] restored original {graph_html}", flush=True)


# ── Assertion ──────────────────────────────────────────────────────────────────

def _assess(tag: str, response: str, stream: str,
            pass_keywords: list[str], fail_keywords: list[str]) -> tuple[str, list[str]]:
    combined = (response + "\n" + stream).lower()
    if not combined.strip():
        return "FAIL", ["[empty response]"]
    triggered = [kw for kw in fail_keywords if kw.lower() in combined]
    if triggered:
        return "FAIL", [f"fail_kw={kw!r}" for kw in triggered]
    missing = [kw for kw in pass_keywords if kw.lower() not in combined]
    if missing:
        return "WARN", [f"missing={kw!r}" for kw in missing]
    return "PASS", []


# ── Tests ──────────────────────────────────────────────────────────────────────

TESTS = [
    {
        "tag": "S1",
        "label": "Generate — Jei calls generate_graph(namespace='nimbus')",
        "question": (
            "请调用 generate_graph 工具，namespace 设为 'nimbus'。"
            "告诉我生成的 HTML 文件路径是什么。"
        ),
        "pass_keywords": ["graph.html", "nimbus"],
        "fail_keywords": ["error", "失败", "not found", "unavailable"],
        "note": "Jei should call generate_graph MCP tool and return the output path.",
    },
    {
        "tag": "S4",
        "label": "Vault URI — generate_graph returns valid obsidian:// URI info",
        "question": (
            "请再次调用 generate_graph，namespace='nimbus'，vault='NimbusVault'。"
            "然后读取生成的 graph.html 文件，告诉我：\n"
            "1. 文件里是否包含 'obsidian://open?vault=NimbusVault' 这个字符串？\n"
            "2. 找到其中一个 obsidian URI 的完整内容并展示给我。"
        ),
        "pass_keywords": ["nimbusVault".lower(), "obsidian://"],
        "fail_keywords": ["error", "失败", "not found"],
        "note": "Verifies vault_name is correctly embedded in generated obsidian:// URIs.",
    },
    {
        "tag": "S5",
        "label": "CLI — prism visualize --help shows correct options",
        "question": (
            "请在终端运行命令 `prism-rag visualize --help`，"
            "把输出原文告诉我，特别是所有 --option 选项的名称。"
        ),
        "pass_keywords": ["--namespace", "--vault", "--output"],
        "fail_keywords": ["--federation", "error", "command not found"],
        "note": "Verifies CLI options: --namespace/--vault/--output present, --federation absent.",
    },
]


# ── Runner ─────────────────────────────────────────────────────────────────────

async def run_tests():
    state = _setup()

    loader = EntityLoader(BLUEPRINT_DIR, data_dir=DATA_DIR)
    await loader.start_mcp_servers()
    controller = await loader.get_controller()

    session_name = f"v56_smoke_{int(__import__('time').time())}"
    await controller.new_session(session_name)
    print(f"[smoke] fresh session: {session_name}\n", flush=True)

    try:
        graph = controller._graph
        if hasattr(graph, "nodes"):
            for node_id, node_fn in graph.nodes.items():
                if hasattr(node_fn, "set_stream_callback"):
                    node_fn.set_stream_callback(_stream_cb)
    except Exception as e:
        print(f"[warn] stream callback: {e}")

    results = []
    sep = "=" * 70

    try:
        for test in TESTS:
            global _stream_buf
            _stream_buf = []

            tag   = test["tag"]
            label = test["label"]
            q     = test["question"]

            print(f"\n{sep}")
            print(f"TEST {tag}: {label}")
            print(f"NOTE: {test['note']}")
            print(sep)
            print("RESPONSE:")

            try:
                response = await asyncio.wait_for(controller.run(q), timeout=300)
                stream   = "".join(_stream_buf)
                final    = response if len(response) >= len(stream) else stream
                if not final.strip():
                    final = response or stream
                if not stream and final:
                    print(final)
                response = final
            except asyncio.TimeoutError:
                print("\n*** TIMEOUT ***")
                response = stream = ""
            except Exception as e:
                import traceback
                print(f"\n*** ERROR: {e} ***")
                traceback.print_exc()
                response = stream = ""

            stream = "".join(_stream_buf)
            status, failures = _assess(tag, response, stream,
                                       test["pass_keywords"], test["fail_keywords"])
            print(f"\n--- END {tag} ---")
            if status == "PASS":
                print(f"VERDICT: PASS [AGENT LAYER]")
            elif status == "WARN":
                print(f"VERDICT: WARN [AGENT LAYER]  → {failures}")
            else:
                print(f"VERDICT: FAIL [AGENT LAYER]  → {failures}")
            results.append((tag, status, failures, label))

        # ── S2: filesystem check — graph.html content ────────────────────────
        print(f"\n{sep}")
        print("TEST S2: Filesystem — graph.html exists with Obsidian JS injected")
        print(sep)

        graph_html = state["graph_html"]
        s2_status = "FAIL"
        s2_details = []

        if not graph_html.exists():
            s2_details.append("graph.html does not exist")
        else:
            html = graph_html.read_text(encoding="utf-8")
            checks = [
                ("_prismNodeData",              "_prismNodeData JS map present"),
                ("obsidian://",                 "obsidian:// URI present"),
                ("stabilizationIterationsDone", "hash-focus event present"),
                ("portal_href",                 "portal_href in JS"),
            ]
            passed = []
            failed = []
            for needle, desc in checks:
                if needle in html:
                    passed.append(f"✅ {desc}")
                else:
                    failed.append(f"❌ {desc}")
            s2_details = passed + failed
            s2_status = "PASS" if not failed else "FAIL"
            print(f"graph.html size: {len(html):,} bytes")
            for d in s2_details:
                print(f"  {d}")

        print(f"\n--- END S2 ---")
        print(f"VERDICT: {'PASS' if s2_status == 'PASS' else 'FAIL'} [FILESYSTEM]")
        results.append(("S2", s2_status, [d for d in s2_details if d.startswith("❌")], "graph.html Obsidian JS"))

        # ── S3: portal nodes check ────────────────────────────────────────────
        print(f"\n{sep}")
        print("TEST S3: Portal nodes — hexagon shape in cross-namespace refs")
        print(sep)

        s3_status = "PASS"
        s3_detail = ""

        if not graph_html.exists():
            s3_status = "FAIL"
            s3_detail = "graph.html missing, cannot check portals"
        else:
            html = graph_html.read_text(encoding="utf-8")
            has_hexagon = "hexagon" in html
            # portal_href appears in JS template code, so check _prismNodeData for actual portal entries
            import re, json as _json
            m = re.search(r'var _prismNodeData\s*=\s*(\{.*?\});', html, re.DOTALL)
            portal_count = 0
            if m:
                nd = _json.loads(m.group(1))
                portal_count = sum(1 for v in nd.values() if "portal_href" in v)
            if has_hexagon and portal_count > 0:
                s3_detail = f"portal hexagon nodes present ({portal_count} portals)"
            elif portal_count == 0 and not has_hexagon:
                s3_detail = "no cross-namespace refs in this graph (expected for single-namespace ingest)"
                s3_status = "PASS"
            else:
                s3_detail = f"partial: hexagon={has_hexagon} portal_count={portal_count}"
                s3_status = "WARN"

        print(f"  {s3_detail}")
        print(f"\n--- END S3 ---")
        print(f"VERDICT: {'PASS' if s3_status == 'PASS' else s3_status} [FILESYSTEM]")
        results.append(("S3", s3_status, [s3_detail] if s3_status != "PASS" else [], "Portal hexagon nodes"))

        # ── S6: _prismNodeData entry counts ──────────────────────────────────
        print(f"\n{sep}")
        print("TEST S6: _prismNodeData — obsidian_uri count matches note/knowledge nodes")
        print(sep)

        s6_status = "FAIL"
        s6_detail = ""

        if not graph_html.exists():
            s6_detail = "graph.html missing"
        else:
            html = graph_html.read_text(encoding="utf-8")
            m = re.search(r'var _prismNodeData\s*=\s*(\{.*?\});', html, re.DOTALL)
            if not m:
                s6_detail = "_prismNodeData not found in HTML"
            else:
                nd = _json.loads(m.group(1))
                total     = len(nd)
                obsidian  = sum(1 for v in nd.values() if "obsidian_uri" in v)
                portal    = sum(1 for v in nd.values() if "portal_href" in v)
                print(f"  _prismNodeData total entries : {total}")
                print(f"  obsidian_uri entries         : {obsidian}")
                print(f"  portal_href entries          : {portal}")
                if obsidian > 0:
                    sample = next(v["obsidian_uri"] for v in nd.values() if "obsidian_uri" in v)
                    print(f"  sample URI: {sample[:100]}")
                if total > 0 and obsidian > 0:
                    s6_status = "PASS"
                    s6_detail = f"{obsidian}/{total} entries have obsidian_uri, {portal} portal_href"
                elif total == 0:
                    s6_detail = "_prismNodeData is empty — vault_name may not be set"
                else:
                    s6_detail = f"total={total} but obsidian_uri=0 — vault_name not applied"

        print(f"\n--- END S6 ---")
        print(f"VERDICT: {'PASS' if s6_status == 'PASS' else 'FAIL'} [FILESYSTEM]")
        results.append(("S6", s6_status, [s6_detail] if s6_status != "PASS" else [], "_prismNodeData obsidian_uri count"))

    finally:
        _teardown(state)

    # ── Summary ───────────────────────────────────────────────────────────────
    print(f"\n\n{sep}")
    print("V5.6 SMOKE TEST SUMMARY")
    print(sep)

    pass_count = sum(1 for _, s, _, _ in results if s == "PASS")
    warn_count = sum(1 for _, s, _, _ in results if s == "WARN")
    fail_count = sum(1 for _, s, _, _ in results if s == "FAIL")

    for tag, status, failures, label in results:
        icon = "✅" if status == "PASS" else "⚠️" if status == "WARN" else "❌"
        short = label[:55] + "..." if len(label) > 55 else label
        reason = f"  → {failures}" if failures else ""
        print(f"  {icon} {tag:<4} {short:<58}{reason}")

    print(sep)
    if fail_count == 0 and warn_count == 0:
        print("RESULT: ALL PASS ✅")
    elif fail_count == 0:
        print(f"RESULT: ALL PASS ✅  ({warn_count} WARN)")
    else:
        print(f"RESULT: {fail_count} FAIL, {warn_count} WARN")


def main():
    asyncio.run(run_tests())


if __name__ == "__main__":
    main()
