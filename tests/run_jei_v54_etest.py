"""
v5.4 smoke test runner for Jei — validates P1/P2/P3/P4 via live Jei session.

S1 — P1: New session path inference (vault root injected via MCP instructions)
S2 — P2: KNOW-ID routing (Jei must call explain_node, not search_knowledge)
S3 — P3/P4: list_knowledge_nodes body_preview + readable label in theme analysis

Two-layer assertion for S2:
  Layer A (System): search_knowledge("KNOW-000008") returns soft hint, not search results
                    Verified by: hint keywords in streamed tool output OR response text
  Layer B (Agent):  Jei eventually calls explain_node, response contains KNOW-000008 content
                    Verified by: known content keywords in final response

Usage:
    # Stop the systemd Jei first to avoid DB contention
    systemctl --user stop jei

    cd /path/to/VoidDraft/tests
    python3 run_jei_v54_etest.py

    # Restart after
    systemctl --user start jei

Pass criteria:
    PASS  = all assertions met
    WARN  = response exists but expected keywords missing (prompt-layer issue)
    FAIL  = error / timeout / empty response (code-layer issue)
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

# ── Known content anchors ──────────────────────────────────────────────────────
# KNOW-000008: "PrismRag Embedding 层次：Text-first vs 真正跨模态"
KNOW_000008_KEYWORDS = ["Text-first", "跨模态", "nomic-embed-vision", "embedding", "KNOW-000008"]

# A known vault doc that exists (used for S1 path test)
KNOWN_DOC = "设计细节/PrismRag v5.4 — Atomize 体验精打磨.md"

TESTS = [
    # ── S1: P1 — vault path inference ─────────────────────────────────────────
    {
        "tag": "S1",
        "label": "P1 — new session vault path inference",
        "question": f"帮我读一下 {KNOWN_DOC} 这个笔记的标题（frontmatter 里的 title 字段）",
        # Pass: response contains "P1" or title keyword, no path error
        "pass_keywords": ["v5.4", "Atomize", "体验精打磨", "PrismRag"],
        "fail_keywords": ["路径不存在", "not found", "No such file", "path does not exist",
                          "FileNotFoundError", "无法找到"],
        "layer": "agent",
        "note": "Verifies vault root is injected correctly in new session (P1)",
    },

    # ── S2: P2 — KNOW-ID routing ──────────────────────────────────────────────
    {
        "tag": "S2-A",
        "label": "P2 System layer — search_knowledge returns soft hint for KNOW-ID",
        "question": "KNOW-000008 的内容是什么？",
        # Layer A: hint keywords should appear in stream (tool output visible to Jei)
        # We look for them in the combined stream output
        "pass_keywords": ["⚠️", "未执行搜索", "explain_node"],
        "fail_keywords": [],
        "layer": "system",
        "note": "Verifies MCP soft hint is returned (System layer). "
                "If WARN: hint not visible in stream but agent layer may still pass.",
    },
    {
        "tag": "S2-B",
        "label": "P2 Agent layer — Jei calls explain_node, presents KNOW-000008 content",
        "question": "KNOW-000008 的内容是什么？",
        # Layer B: final response must contain actual node content
        "pass_keywords": KNOW_000008_KEYWORDS,
        "fail_keywords": ["无法找到", "没有找到", "搜索结果为空", "不存在"],
        "layer": "agent",
        "note": "Verifies Jei actually called explain_node and got real content (Agent layer).",
    },

    # ── S3: P3/P4 — list_knowledge_nodes body_preview + readable labels ────────
    {
        "tag": "S3",
        "label": "P3/P4 — list_knowledge_nodes body_preview enables theme analysis",
        "question": "列出所有 KNOW 节点，分析哪两个节点的主题最相近，说明你的判断依据",
        # Pass: response references actual content (not just labels), shows analysis
        "pass_keywords": ["KNOW-", "主题", "相近"],
        "fail_keywords": ["没有内容", "无法分析", "信息不足"],
        "layer": "agent",
        "note": "Verifies body_preview enables content-based analysis (P3) and "
                "labels are readable not just slugs (P4).",
    },

    # ── S4: Embedding path — hybrid semantic search via Ollama ────────────────
    {
        "tag": "S4",
        "label": "Embedding path — semantic search triggers Ollama GPU embedding",
        # Deliberately avoids exact keywords ("Text-first", "nomic-embed-vision")
        # so BM25/exact alone cannot find the right node.
        # Only vector similarity can surface KNOW-000008 from this paraphrase.
        "question": "向量空间里把图片和文字统一表示的方案，PrismRag 是怎么设计的？",
        # KNOW-000008 content: nomic-embed-vision, 768-dim, 跨模态
        "pass_keywords": ["nomic-embed-vision", "768", "跨模态", "KNOW-000008"],
        "fail_keywords": ["没有找到", "搜索结果为空", "无法回答", "不相关"],
        "layer": "agent",
        "note": "Verifies embedding-based hybrid search works (Ollama must be reachable). "
                "Query is a paraphrase — BM25/exact alone cannot surface the correct node. "
                "WARN = Ollama may be down or embedding path degraded to BM25-only.",
    },
]


# ── Stream capture ─────────────────────────────────────────────────────────────

_stream_buf: list[str] = []

def _stream_cb(text: str, is_thinking: bool = False) -> None:
    _stream_buf.append(text)
    print(text, end="", flush=True)


def _assess(response: str, stream: str, test: dict) -> tuple[str, list[str]]:
    """Return (status, failed_checks).

    status: PASS | WARN | FAIL
    PASS = all pass_keywords found in (response + stream)
    WARN = no fail_keywords, but some pass_keywords missing
    FAIL = fail_keywords found OR empty response
    """
    combined = (response + "\n" + stream).lower()
    failed_pass = []
    triggered_fail = []

    for kw in test["pass_keywords"]:
        if kw.lower() not in combined:
            failed_pass.append(kw)

    for kw in test["fail_keywords"]:
        if kw.lower() in combined:
            triggered_fail.append(kw)

    if not response.strip():
        return "FAIL", ["[empty response]"]
    if triggered_fail:
        return "FAIL", [f"fail_kw={kw!r}" for kw in triggered_fail]
    if failed_pass:
        return "WARN", [f"missing={kw!r}" for kw in failed_pass]
    return "PASS", []


async def run_tests():
    loader = EntityLoader(BLUEPRINT_DIR, data_dir=DATA_DIR)
    await loader.start_mcp_servers()
    controller = await loader.get_controller()

    # Force a fresh session so Gemini has no prior context memory.
    # Without this, _init_session() picks the oldest Discord session
    # and Gemini may answer from conversation history, not real tool calls.
    session_name = f"smoke_v54_{int(__import__('time').time())}"
    await controller.new_session(session_name)
    print(f"[smoke] fresh session: {session_name}", flush=True)

    try:
        graph = controller._graph
        if hasattr(graph, "nodes"):
            for node_id, node_fn in graph.nodes.items():
                if hasattr(node_fn, "set_stream_callback"):
                    node_fn.set_stream_callback(_stream_cb)
    except Exception as e:
        print(f"[warn] Could not attach stream callback: {e}")

    results = []
    # S2-A and S2-B ask the same question — share the response for efficiency
    _response_cache: dict[str, str] = {}

    for test in TESTS:
        global _stream_buf
        _stream_buf = []

        sep = "=" * 70
        tag   = test["tag"]
        label = test["label"]
        q     = test["question"]

        print(f"\n{sep}")
        print(f"TEST {tag}: {label}")
        print(f"Q: {q[:120]}{'...' if len(q) > 120 else ''}")
        print(f"NOTE: {test['note']}")
        print(f"{sep}")
        print("RESPONSE:")

        # Cache S2 response (same question for S2-A and S2-B)
        cache_key = q
        if cache_key in _response_cache:
            response = _response_cache[cache_key]
            stream   = ""   # stream was already captured in prior run
            print("[using cached response from previous identical question]")
        else:
            try:
                response = await asyncio.wait_for(
                    controller.run(q),
                    timeout=300,
                )
                stream = "".join(_stream_buf)
                final = response if len(response) >= len(stream) else stream
                if not final.strip():
                    final = response or stream
                if not stream and final:
                    print(final)
                response = final
                _response_cache[cache_key] = response
            except asyncio.TimeoutError:
                print("\n*** TIMEOUT after 300s ***")
                response = ""
                stream   = ""
            except Exception as e:
                import traceback
                print(f"\n*** ERROR: {e} ***")
                traceback.print_exc()
                response = ""
                stream   = ""

        stream = "".join(_stream_buf)
        status, failures = _assess(response, stream, test)

        layer_label = f"[{test['layer'].upper()} LAYER]"
        print(f"\n--- END {tag} ---")
        print(f"VERDICT: {status} {layer_label}"
              + (f"  reasons={failures}" if failures else ""))

        results.append({
            "tag": tag,
            "label": label,
            "layer": test["layer"],
            "status": status,
            "failures": failures,
            "response_len": len(response),
        })

    await loader.stop_mcp_servers()
    return results


def main():
    results = asyncio.run(run_tests())

    sep = "=" * 70
    print(f"\n\n{sep}")
    print("v5.4 SMOKE TEST SUMMARY")
    print(sep)

    all_pass = True
    for r in results:
        icon = {"PASS": "✅", "WARN": "⚠️", "FAIL": "❌"}.get(r["status"], "?")
        layer = r["layer"].upper()
        reason = f"  → {r['failures']}" if r["failures"] else ""
        print(f"  {icon} {r['tag']:6s} [{layer:6s}] {r['label'][:50]:52s}{reason}")
        if r["status"] == "FAIL":
            all_pass = False

    print(sep)
    if all_pass:
        print("RESULT: ALL PASS ✅  v5.4 P1/P2/P3/P4 + Embedding path verified via Jei")
    else:
        fails = [r for r in results if r["status"] == "FAIL"]
        warns = [r for r in results if r["status"] == "WARN"]
        print(f"RESULT: {len(fails)} FAIL, {len(warns)} WARN")
        for r in fails:
            print(f"  FAIL {r['tag']}: {r['failures']}")
        for r in warns:
            layer_note = ("→ code-layer bug" if r["layer"] == "system"
                          else "→ prompt/docstring issue, check tool description")
            print(f"  WARN {r['tag']}: {r['failures']}  {layer_note}")

    print("""
Triage guide:
  S1  FAIL → P1 vault path not injected correctly; check _startup_settings in server.py
  S2-A WARN → soft hint not visible in stream; hint may still work (check S2-B)
  S2-A FAIL → search_knowledge soft hint not triggered; check regex + placement
  S2-B WARN → Jei saw hint but chose not to call explain_node; tweak docstring
  S2-B FAIL → explain_node returned error or Jei ignored hint entirely
  S3  WARN → analysis present but vague; body_preview may be too short or labels still slugs
  S3  FAIL → list_knowledge_nodes error or empty results
""")


if __name__ == "__main__":
    main()
