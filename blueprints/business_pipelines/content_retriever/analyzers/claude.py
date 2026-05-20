"""
Claude analyzer — multimodal analysis using Anthropic API.

Default model: claude-haiku-4-5 (fast, low cost).
For video: extracts frames with ffmpeg, sends all frames in a single message
(Claude supports multiple images in one request).
"""

import base64
import io
import os
import subprocess
import tempfile
from pathlib import Path

import anthropic

from .base import BaseAnalyzer

_MAX_IMAGE_WIDTH = 1024  # Claude supports higher resolution
_DEFAULT_MODEL = "claude-haiku-4-5"


def _find_ffmpeg() -> str:
    """Find ffmpeg binary."""
    local = Path(__file__).parent.parent.parent.parent.parent / ".local" / "bin" / "ffmpeg"
    if local.exists():
        return str(local)
    return "ffmpeg"


def _resize_image_bytes(raw: bytes, max_width: int = _MAX_IMAGE_WIDTH) -> bytes:
    try:
        from PIL import Image
        img = Image.open(io.BytesIO(raw)).convert("RGB")
        if img.width > max_width:
            ratio = max_width / img.width
            img = img.resize((max_width, int(img.height * ratio)), Image.LANCZOS)
        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=90)
        return buf.getvalue()
    except Exception:
        return raw


class ClaudeAnalyzer(BaseAnalyzer):
    """Anthropic Claude multimodal analyzer."""

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
        self._client = anthropic.Anthropic(
            api_key=api_key or os.environ.get("ANTHROPIC_API_KEY")
        )

    # ── Image analysis ────────────────────────────────────────────────────────

    def analyze_image(
        self,
        image_path: Path,
        prompt: str = "Describe this image in detail. Focus on people, actions, and setting.",
    ) -> str:
        try:
            raw = _resize_image_bytes(image_path.read_bytes())
        except OSError as exc:
            return f"[error reading image: {exc}]"

        b64 = base64.standard_b64encode(raw).decode("ascii")
        try:
            msg = self._client.messages.create(
                model=self.model,
                max_tokens=512,
                messages=[{
                    "role": "user",
                    "content": [
                        {
                            "type": "image",
                            "source": {
                                "type": "base64",
                                "media_type": "image/jpeg",
                                "data": b64,
                            },
                        },
                        {"type": "text", "text": prompt},
                    ],
                }],
            )
            return msg.content[0].text
        except Exception as exc:
            return f"[error: {exc}]"

    # ── Text analysis ─────────────────────────────────────────────────────────

    def analyze_text(self, text: str) -> str:
        try:
            msg = self._client.messages.create(
                model=self.model,
                max_tokens=512,
                messages=[{
                    "role": "user",
                    "content": f"请用中文简洁总结以下内容（100字以内）：\n\n{text[:4000]}",
                }],
            )
            return msg.content[0].text
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

        with tempfile.TemporaryDirectory(prefix="claude_frames_") as tmp:
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

            # Uniform sampling
            if len(frames) > self.max_frames:
                step = len(frames) / self.max_frames
                frames = [frames[int(i * step)] for i in range(self.max_frames)]

            print(f"    [claude] Analyzing {len(frames)} frame(s)...", flush=True)

            # Build multi-frame single message (Claude supports multiple images per request)
            content: list[dict] = []
            for i, f in enumerate(frames):
                ts = i * interval
                content.append({
                    "type": "text",
                    "text": f"Frame {i + 1} at {ts // 60:02d}:{ts % 60:02d}:",
                })
                raw = _resize_image_bytes(f.read_bytes())
                b64 = base64.standard_b64encode(raw).decode("ascii")
                content.append({
                    "type": "image",
                    "source": {
                        "type": "base64",
                        "media_type": "image/jpeg",
                        "data": b64,
                    },
                })

            content.append({
                "type": "text",
                "text": "请用中文描述这段视频的内容，包括人物、动作、场景和整体氛围，100字以内。",
            })

            try:
                msg = self._client.messages.create(
                    model=self.model,
                    max_tokens=512,
                    messages=[{"role": "user", "content": content}],
                )
                return msg.content[0].text
            except Exception as exc:
                return f"[error: {exc}]"
