"""
LLM backend abstraction for the summarize node.

Single-shot completion only — no conversation history, no tools.
Supports: Ollama (local), Claude SDK (Anthropic), None (disabled).

Usage:
    backend = SummarizeLlmBackend.from_config(cfg)
    text = backend.complete(prompt)
"""
from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from blueprints.business_pipelines.content_retriever.config import PipelineConfig


class SummarizeLlmBackend:
    """Thin configurable LLM wrapper for single-shot summarisation."""

    SUPPORTED = {"ollama", "claude", "gemini", "none"}

    def __init__(
        self,
        backend: str,
        model: str,
        ollama_url: str = "http://localhost:11434",
        gemini_api_key: str = "",
    ) -> None:
        self.backend = backend.lower().strip()
        self.model = model
        self.ollama_url = ollama_url.rstrip("/")
        self.gemini_api_key = gemini_api_key

        if self.backend not in self.SUPPORTED:
            raise ValueError(
                f"Unknown summarize_backend {self.backend!r}. "
                f"Choose from: {sorted(self.SUPPORTED)}"
            )

    # ---------------------------------------------------------------------- #
    # Public API
    # ---------------------------------------------------------------------- #

    def complete(self, prompt: str) -> str:
        """Send a single prompt, return the raw text response."""
        if self.backend == "none":
            raise RuntimeError(
                "summarize_backend is 'none'. Set it to 'ollama' or 'claude' in config."
            )
        if self.backend == "ollama":
            return self._ollama(prompt)
        if self.backend == "claude":
            return self._claude(prompt)
        if self.backend == "gemini":
            return self._gemini(prompt)
        raise NotImplementedError(self.backend)

    @property
    def is_enabled(self) -> bool:
        return self.backend != "none"

    # ---------------------------------------------------------------------- #
    # Backends
    # ---------------------------------------------------------------------- #

    def _ollama(self, prompt: str) -> str:
        """httpx + /v1/chat/completions — same endpoint & payload as ZenithLoom OllamaNode."""
        import httpx

        payload = {
            "model": self.model,
            "messages": [{"role": "user", "content": prompt}],
            "stream": False,
            "keep_alive": -1,
            "temperature": 0.2,
        }
        with httpx.Client(timeout=httpx.Timeout(600, connect=30)) as client:
            resp = client.post(f"{self.ollama_url}/v1/chat/completions", json=payload)
        resp.raise_for_status()
        return resp.json()["choices"][0]["message"]["content"].strip()

    def _gemini(self, prompt: str) -> str:
        """Call Gemini via ZenithLoom's _CodeAssistClient (OAuth, no API key needed)."""
        import sys
        _ZL = "/home/kingy/Foundation/ZenithLoom"
        if _ZL not in sys.path:
            sys.path.insert(0, _ZL)
        from framework.nodes.llm.gemini import _CodeAssistClient

        model = self.model or "gemini-2.5-pro"
        # jitter_multiplier=1 → ZenithLoom default 1–20 s jitter on 429/quota errors
        client = _CodeAssistClient(model=model, jitter_multiplier=1)
        return client._chat_sync(prompt)

    def _claude(self, prompt: str) -> str:
        """claude_agent_sdk.query() — same dependency as ZenithLoom ClaudeSDKNode.

        Event-loop safe: if a loop is already running (e.g. inside ZenithLoom's
        async executor), we schedule the coroutine onto it via
        concurrent.futures; otherwise we own a fresh loop via asyncio.run().
        """
        import asyncio
        from claude_agent_sdk import query as sdk_query, ClaudeAgentOptions, ResultMessage

        model = self.model or "claude-sonnet-4-5"
        options = ClaudeAgentOptions(model=model, permission_mode="bypassPermissions")

        async def _run() -> str:
            full = ""
            async for event in sdk_query(prompt=prompt, options=options):
                if isinstance(event, ResultMessage):
                    full = event.result or ""
            return full

        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            loop = None

        if loop is not None and loop.is_running():
            # Already inside an async context — run in a thread pool so we
            # don't block the loop and don't nest asyncio.run().
            import concurrent.futures
            with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
                future = pool.submit(asyncio.run, _run())
                return future.result().strip()
        else:
            return asyncio.run(_run()).strip()

    # ---------------------------------------------------------------------- #
    # Factory
    # ---------------------------------------------------------------------- #

    @classmethod
    def from_config(cls, cfg: "PipelineConfig") -> "SummarizeLlmBackend":
        backend = getattr(cfg, "summarize_backend", "none")
        model = getattr(cfg, "summarize_model", "")
        ollama_url = getattr(cfg, "ollama_url", "http://localhost:11434")
        gemini_api_key = getattr(cfg, "gemini_api_key", "")
        return cls(backend=backend, model=model, ollama_url=ollama_url, gemini_api_key=gemini_api_key)
