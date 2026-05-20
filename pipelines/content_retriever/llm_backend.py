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
    from pipelines.content_retriever.config import PipelineConfig


class SummarizeLlmBackend:
    """Thin configurable LLM wrapper for single-shot summarisation."""

    SUPPORTED = {"ollama", "claude", "none"}

    def __init__(
        self,
        backend: str,
        model: str,
        ollama_url: str = "http://localhost:11434",
    ) -> None:
        self.backend = backend.lower().strip()
        self.model = model
        self.ollama_url = ollama_url.rstrip("/")

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
        raise NotImplementedError(self.backend)

    @property
    def is_enabled(self) -> bool:
        return self.backend != "none"

    # ---------------------------------------------------------------------- #
    # Backends
    # ---------------------------------------------------------------------- #

    def _ollama(self, prompt: str) -> str:
        import requests

        resp = requests.post(
            f"{self.ollama_url}/api/generate",
            json={
                "model": self.model,
                "prompt": prompt,
                "stream": False,
                "options": {"temperature": 0.2},
            },
            timeout=180,
        )
        resp.raise_for_status()
        data = resp.json()
        # /api/generate returns {"response": "..."}
        return data.get("response", "").strip()

    def _claude(self, prompt: str) -> str:
        import anthropic

        client = anthropic.Anthropic()          # reads ANTHROPIC_API_KEY
        msg = client.messages.create(
            model=self.model,
            max_tokens=1024,
            messages=[{"role": "user", "content": prompt}],
        )
        return msg.content[0].text.strip()

    # ---------------------------------------------------------------------- #
    # Factory
    # ---------------------------------------------------------------------- #

    @classmethod
    def from_config(cls, cfg: "PipelineConfig") -> "SummarizeLlmBackend":
        backend = getattr(cfg, "summarize_backend", "none")
        model = getattr(cfg, "summarize_model", "")
        ollama_url = getattr(cfg, "ollama_url", "http://localhost:11434")
        return cls(backend=backend, model=model, ollama_url=ollama_url)
