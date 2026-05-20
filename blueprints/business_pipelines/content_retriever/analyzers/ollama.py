"""
Ollama analyzer — supports images, video frames, and text via local Ollama API.

Settings:
  - think: False (disable thinking mode to avoid timeouts)
  - num_predict: 512 (limit output length)
  - Images resized to MAX_IMAGE_WIDTH=640px before sending (reduce inference time)
  - Short videos get adaptive frame interval (at most 3 frames)
"""

import base64
import io
import subprocess
import tempfile
from pathlib import Path

import httpx

from .base import BaseAnalyzer

_OLLAMA_BASE = "http://localhost:11434"
_MAX_IMAGE_WIDTH = 640  # resize images before sending to VLM


def _find_ffmpeg() -> str | None:
    """Find ffmpeg binary: check common local path, fall back to system PATH."""
    local = Path(__file__).parent.parent.parent.parent.parent / ".local" / "bin" / "ffmpeg"
    if local.exists():
        return str(local)
    result = subprocess.run(["which", "ffmpeg"], capture_output=True, text=True)
    if result.returncode == 0:
        return result.stdout.strip()
    return None


def _resize_image_bytes(raw: bytes, max_width: int = _MAX_IMAGE_WIDTH) -> bytes:
    """Resize image to max_width preserving aspect ratio, return JPEG bytes. Requires Pillow."""
    try:
        from PIL import Image
        img = Image.open(io.BytesIO(raw)).convert("RGB")
        if img.width > max_width:
            ratio = max_width / img.width
            img = img.resize((max_width, int(img.height * ratio)), Image.LANCZOS)
        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=85)
        return buf.getvalue()
    except ImportError:
        return raw  # Pillow not installed, return as-is
    except Exception:
        return raw


class OllamaAnalyzer(BaseAnalyzer):
    """Unified Ollama analyzer supporting images, video frames, and text."""

    def __init__(
        self,
        vision_model: str = "gemma4:e4b",
        text_model: str = "gemma4:e4b",
        frame_interval: int = 10,
        max_frames: int = 8,
    ):
        self.vision_model = vision_model
        self.text_model = text_model
        self.frame_interval = frame_interval
        self.max_frames = max_frames

    # ------------------------------------------------------------------
    # Public methods
    # ------------------------------------------------------------------

    def analyze_image(
        self,
        image_path: Path,
        prompt: str = "Describe this image in detail.",
    ) -> str:
        """Send image to Ollama VLM, return description string."""
        try:
            raw = image_path.read_bytes()
        except OSError as exc:
            return f"[error reading image: {exc}]"

        raw = _resize_image_bytes(raw)
        b64 = base64.b64encode(raw).decode("ascii")
        return self._ollama_generate(self.vision_model, prompt, images=[b64])

    def analyze_video(self, video_path: Path) -> str:
        """Extract frames with ffmpeg every frame_interval seconds, analyze each, return summary."""
        ffmpeg_bin = _find_ffmpeg()
        if not ffmpeg_bin:
            return "[error: ffmpeg not found; cannot analyze video]"

        with tempfile.TemporaryDirectory(prefix="ollama_frames_") as tmp_dir:
            tmp_path = Path(tmp_dir)
            frame_pattern = str(tmp_path / "frame_%04d.jpg")

            # Probe video duration to adapt interval for short videos
            probe = subprocess.run(
                [ffmpeg_bin, "-i", str(video_path)],
                capture_output=True,
                text=True,
            )
            duration = 0.0
            for line in probe.stderr.splitlines():
                if "Duration:" in line:
                    parts = line.split("Duration:")[1].split(",")[0].strip()
                    try:
                        h, m, s = parts.split(":")
                        duration = float(h) * 3600 + float(m) * 60 + float(s)
                    except Exception:
                        pass
                    break

            interval = self.frame_interval
            if duration > 0 and duration < interval * 2:
                # Short video: extract at most 3 frames
                interval = max(1, int(duration / 3))

            cmd = [
                ffmpeg_bin,
                "-i", str(video_path),
                "-vf", f"fps=1/{interval}",
                "-q:v", "2",
                frame_pattern,
                "-y",
                "-loglevel", "error",
            ]
            try:
                proc = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
            except subprocess.TimeoutExpired:
                return "[error: ffmpeg timed out while extracting frames]"
            except FileNotFoundError:
                return f"[error: ffmpeg binary not found at {ffmpeg_bin}]"

            if proc.returncode != 0:
                return f"[error: ffmpeg failed: {proc.stderr.strip()}]"

            frame_files = sorted(tmp_path.glob("frame_*.jpg"))
            if not frame_files:
                return "[no frames extracted from video]"

            # Uniform sampling to respect max_frames limit
            if len(frame_files) > self.max_frames:
                step = len(frame_files) / self.max_frames
                frame_files = [frame_files[int(i * step)] for i in range(self.max_frames)]

            print(f"    [ollama] Analyzing {len(frame_files)} frame(s)...", flush=True)
            frame_descriptions: list[str] = []
            for i, frame_path in enumerate(frame_files):
                timestamp_sec = i * interval
                minutes, seconds = divmod(timestamp_sec, 60)
                ts_label = f"{minutes:02d}:{seconds:02d}"
                prompt = (
                    f"This is frame {i + 1} at timestamp {ts_label} from a video. "
                    "Describe what you see in detail."
                )
                description = self.analyze_image(frame_path, prompt=prompt)
                frame_descriptions.append(f"[{ts_label}] {description}")

            # Synthesize overall video description
            combined = "\n\n".join(frame_descriptions)
            synthesis_prompt = (
                "The following are descriptions of frames extracted from a video, "
                "one frame every few seconds. Please synthesize these into a coherent, "
                "concise description of what the video shows overall:\n\n"
                f"{combined}"
            )
            return self._ollama_generate(self.text_model, synthesis_prompt)

    def analyze_text(self, text: str) -> str:
        """Summarize text using Ollama."""
        prompt = f"Please summarize the following text concisely:\n\n{text}"
        return self._ollama_generate(self.text_model, prompt)

    # ------------------------------------------------------------------
    # Core API call
    # ------------------------------------------------------------------

    def _ollama_generate(
        self,
        model: str,
        prompt: str,
        images: list[str] | None = None,
    ) -> str:
        """Core Ollama /api/generate call. images = list of base64-encoded strings."""
        payload: dict = {
            "model": model,
            "prompt": prompt,
            "stream": False,
            "think": False,          # disable thinking mode
            "options": {
                "num_predict": 512,  # limit output length
            },
        }
        if images:
            payload["images"] = images
        try:
            resp = httpx.post(
                f"{_OLLAMA_BASE}/api/generate",
                json=payload,
                timeout=300,
            )
            resp.raise_for_status()
            return resp.json().get("response", "")
        except httpx.ConnectError:
            return f"[error: cannot connect to Ollama at {_OLLAMA_BASE}]"
        except httpx.HTTPStatusError as exc:
            return f"[error: Ollama returned HTTP {exc.response.status_code}]"
        except Exception as exc:
            return f"[error: {exc}]"
