"""
Gemini analyzer — multimodal analysis using Google Gemini API.

Uses `from google import genai` (google-genai package, NOT google-generativeai).
Default model: gemini-2.0-flash (fast).

For video: extracts frames with ffmpeg, sends all frames together as PIL Images
(Gemini supports multiple images in a single request).
"""

import io
import os
import subprocess
import tempfile
from pathlib import Path

from google import genai
from PIL import Image as PILImage

from .base import BaseAnalyzer

_MAX_IMAGE_WIDTH = 1024
_DEFAULT_MODEL = "gemini-2.0-flash"


def _find_ffmpeg() -> str:
    """Find ffmpeg binary."""
    local = Path(__file__).parent.parent.parent.parent.parent / ".local" / "bin" / "ffmpeg"
    if local.exists():
        return str(local)
    return "ffmpeg"


def _resize_pil(raw: bytes, max_width: int = _MAX_IMAGE_WIDTH) -> PILImage.Image:
    img = PILImage.open(io.BytesIO(raw)).convert("RGB")
    if img.width > max_width:
        ratio = max_width / img.width
        img = img.resize((max_width, int(img.height * ratio)), PILImage.LANCZOS)
    return img


class GeminiAnalyzer(BaseAnalyzer):
    """Google Gemini multimodal analyzer."""

    def __init__(
        self,
        model: str = _DEFAULT_MODEL,
        frame_interval: int = 10,
        max_frames: int = 8,
        api_key: str | None = None,
    ):
        self.model = model
        self.frame_interval = frame_interval
        self.max_frames = max_frames
        self._client = genai.Client(api_key=api_key or os.environ.get("GOOGLE_API_KEY"))

    # ── Image analysis ────────────────────────────────────────────────────────

    def analyze_image(
        self,
        image_path: Path,
        prompt: str = "Describe this image in detail. Focus on people, actions, and setting.",
    ) -> str:
        try:
            img = _resize_pil(image_path.read_bytes())
        except Exception as exc:
            return f"[error reading image: {exc}]"
        try:
            resp = self._client.models.generate_content(
                model=self.model,
                contents=[prompt, img],
            )
            return resp.text
        except Exception as exc:
            return f"[error: {exc}]"

    # ── Text analysis ─────────────────────────────────────────────────────────

    def analyze_text(self, text: str) -> str:
        try:
            resp = self._client.models.generate_content(
                model=self.model,
                contents=f"请用中文简洁总结以下内容（100字以内）：\n\n{text[:4000]}",
            )
            return resp.text
        except Exception as exc:
            return f"[error: {exc}]"

    # ── Video analysis ────────────────────────────────────────────────────────

    def analyze_video(self, video_path: Path) -> str:
        ffmpeg_bin = _find_ffmpeg()

        # Probe duration
        probe = subprocess.run(
            [ffmpeg_bin, "-i", str(video_path)],
            capture_output=True,
            text=True,
        )
        duration = 0.0
        for line in probe.stderr.splitlines():
            if "Duration:" in line:
                try:
                    t = line.split("Duration:")[1].split(",")[0].strip()
                    h, m, s = t.split(":")
                    duration = float(h) * 3600 + float(m) * 60 + float(s)
                except Exception:
                    pass
                break

        interval = self.frame_interval
        if duration > 0 and duration < interval * 2:
            interval = max(1, int(duration / 3))

        with tempfile.TemporaryDirectory(prefix="gemini_frames_") as tmp:
            pattern = str(Path(tmp) / "frame_%04d.jpg")
            subprocess.run(
                [
                    ffmpeg_bin, "-i", str(video_path),
                    "-vf", f"fps=1/{interval}",
                    "-q:v", "2", pattern,
                    "-y", "-loglevel", "error",
                ],
                capture_output=True,
                timeout=120,
            )
            frames = sorted(Path(tmp).glob("frame_*.jpg"))
            if not frames:
                return "[no frames extracted]"

            if len(frames) > self.max_frames:
                step = len(frames) / self.max_frames
                frames = [frames[int(i * step)] for i in range(self.max_frames)]

            print(f"    [gemini] Analyzing {len(frames)} frame(s)...", flush=True)

            # Gemini supports multiple PIL Images in a single request
            parts: list = []
            for i, f in enumerate(frames):
                ts = i * interval
                parts.append(f"Frame {i + 1} at {ts // 60:02d}:{ts % 60:02d}:")
                parts.append(_resize_pil(f.read_bytes()))

            parts.append("请用中文描述这段视频的内容，包括人物、动作、场景和整体氛围，100字以内。")

            try:
                resp = self._client.models.generate_content(
                    model=self.model,
                    contents=parts,
                )
                return resp.text
            except Exception as exc:
                return f"[error: {exc}]"
