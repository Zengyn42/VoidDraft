"""
DETERMINISTIC node functions for the content_retriever pipeline.

Each function signature: (state: dict) -> dict
State fields (all JSON strings):
  config     - JSON-serialized PipelineConfig dict
  posts      - JSON-serialized list of post dicts
  downloads  - JSON-serialized list of download record dicts
  analysis   - JSON-serialized analysis results dict
  report     - Final report text
  errors     - JSON-serialized error list
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

# ---------------------------------------------------------------------------
# PipelineConfig dataclass
# ---------------------------------------------------------------------------

from blueprints.business_pipelines.content_retriever.config import PipelineConfig


def _load_list(state: dict, key: str) -> list:
    """Read a list field from state — handles both native list and legacy JSON string."""
    val = state.get(key, [])
    if isinstance(val, str):
        return json.loads(val or "[]")
    return list(val) if val else []


_PIXELDRAIN_FILE_RE = re.compile(r"pixeldrain\.com/u/([A-Za-z0-9]+)")
_PIXELDRAIN_LIST_RE = re.compile(r"pixeldrain\.com/l/([A-Za-z0-9]+)")
_IMAGE_SUFFIXES = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp", ".tiff"}
_VIDEO_SUFFIXES = {".mp4", ".mkv", ".avi", ".mov", ".webm", ".flv", ".m4v"}


def _safe_title(title: str, max_len: int = 40) -> str:
    slug = re.sub(r'[^\w\s-]', '', title, flags=re.UNICODE)
    slug = re.sub(r'[\s]+', '_', slug.strip())
    return slug[:max_len]


def _find_pixeldrain_urls(texts: list[str]) -> list[str]:
    seen: set[str] = set()
    urls: list[str] = []
    for text in texts:
        for m in _PIXELDRAIN_FILE_RE.finditer(text):
            url = f"https://pixeldrain.com/u/{m.group(1)}"
            if url not in seen:
                seen.add(url)
                urls.append(url)
        for m in _PIXELDRAIN_LIST_RE.finditer(text):
            url = f"https://pixeldrain.com/l/{m.group(1)}"
            if url not in seen:
                seen.add(url)
                urls.append(url)
    return urls


def _here() -> Path:
    """Return the directory containing validators.py."""
    return Path(__file__).parent


def _add_sources_to_path():
    """Ensure sources/downloaders/analyzers are importable."""
    p = str(_here())
    if p not in sys.path:
        sys.path.insert(0, p)


# ---------------------------------------------------------------------------
# Node: fetch
# ---------------------------------------------------------------------------

def fetch(state: dict) -> dict:
    """
    DETERMINISTIC node: fetch posts from the configured source.
    Reads state["config"] (JSON string), writes state["posts"].
    """
    errors: list[str] = []

    cfg_raw = state.get("config", "{}")
    try:
        cfg = PipelineConfig.from_dict(json.loads(cfg_raw))
    except Exception as exc:
        errors.append(f"[fetch] config parse error: {exc}")
        return {"posts": [], "errors": errors}

    _add_sources_to_path()

    # Resolve sources package relative to this file
    sources_dir = str(_here())
    if sources_dir not in sys.path:
        sys.path.insert(0, sources_dir)

    try:
        if cfg.source_type == "reddit":
            from sources.reddit import RedditSource
            source = RedditSource(**cfg.source_config)
        elif cfg.source_type in ("rednote", "xiaohongshu"):
            from sources.rednote import RedNoteSource
            source = RedNoteSource(**cfg.source_config)
        else:
            raise ValueError(f"Unknown source type: {cfg.source_type!r}")
    except Exception as exc:
        errors.append(f"[fetch] source init error: {exc}")
        return {"posts": [], "errors": errors}

    download_dir = Path(cfg.download_dir)
    download_dir.mkdir(parents=True, exist_ok=True)

    # Load existing state to skip already-processed posts
    state_file = download_dir / "pipeline_state.json"
    downloaded_keys: set[str] = set()
    if state_file.exists():
        try:
            downloaded_keys = set(json.loads(state_file.read_text(encoding="utf-8")))
        except Exception:
            pass

    posts_data: list[dict] = []
    print(f"[fetch] source={cfg.source_type}, max_posts={cfg.max_posts}")

    try:
        for post in source.get_posts(cfg.max_posts):
            if post.url in downloaded_keys:
                print(f"[fetch] Skipping already-processed post: {post.id}")
                continue
            posts_data.append({
                "id": post.id,
                "title": post.title,
                "url": post.url,
                "text": post.text,
                "source": post.source,
                "image_urls": list(post.image_urls),
                "video_urls": list(post.video_urls),
                "extra": post.extra,
            })
    except Exception as exc:
        errors.append(f"[fetch] error during post collection: {exc}")

    print(f"[fetch] Collected {len(posts_data)} new post(s).")
    return {
        "posts": posts_data,
        "errors": errors,
    }


# ---------------------------------------------------------------------------
# Node: download
# ---------------------------------------------------------------------------

def download(state: dict) -> dict:
    """
    DETERMINISTIC node: download media from fetched posts.
    Reads state["config"] and state["posts"], writes state["downloads"].
    """
    errors: list[str] = []

    try:
        cfg = PipelineConfig.from_dict(json.loads(state.get("config", "{}")))
        posts_data: list[dict] = _load_list(state, "posts")
    except Exception as exc:
        errors.append(f"[download] parse error: {exc}")
        return {"downloads": [], "errors": errors}

    if not posts_data:
        print("[download] No posts to download.")
        return {"downloads": [], "errors": errors}

    _add_sources_to_path()

    try:
        from downloaders.pixeldrain import PixeldrainDownloader
        pixeldrain_dl = PixeldrainDownloader(api_key=cfg.pixeldrain_api_key)
    except Exception:
        pixeldrain_dl = None

    try:
        from downloaders.direct import DirectDownloader
        direct_dl = DirectDownloader()
    except Exception:
        direct_dl = None

    download_dir = Path(cfg.download_dir)
    download_dir.mkdir(parents=True, exist_ok=True)

    # Load / init download state
    state_file = download_dir / "pipeline_state.json"
    downloaded_keys: set[str] = set()
    if state_file.exists():
        try:
            downloaded_keys = set(json.loads(state_file.read_text(encoding="utf-8")))
        except Exception:
            pass

    downloads_data: list[dict] = []

    def _save_state():
        state_file.write_text(
            json.dumps(sorted(downloaded_keys), indent=2, ensure_ascii=False),
            encoding="utf-8",
        )

    for post_dict in posts_data:
        post_id = post_dict["id"]
        title = post_dict.get("title", "")
        post_url = post_dict["url"]
        post_text = post_dict.get("text", "") or ""
        extra = post_dict.get("extra", {})

        slug = _safe_title(title)
        post_dir = download_dir / f"{post_id}_{slug}"
        post_dir.mkdir(parents=True, exist_ok=True)

        (post_dir / "text.txt").write_text(post_text, encoding="utf-8")

        downloaded_files: list[str] = []

        # (a) Pixeldrain links
        all_texts = [post_text] + list(extra.get("comment_texts", []))
        pixeldrain_urls = _find_pixeldrain_urls(all_texts)
        if pixeldrain_urls and pixeldrain_dl is not None:
            for url in pixeldrain_urls:
                if url in downloaded_keys:
                    print(f"  [download] Skipping pixeldrain (already done): {url}")
                    continue
                print(f"  [download] Downloading pixeldrain: {url}")
                try:
                    files = pixeldrain_dl.download(url, post_dir)
                    downloaded_files.extend(str(f) for f in files)
                    downloaded_keys.add(url)
                    _save_state()
                except Exception as exc:
                    errors.append(f"[download] pixeldrain failed {url}: {exc}")

        # (b) XHS-Downloader or direct URLs
        if extra.get("use_xhs_downloader") and post_dict.get("source") in ("rednote", "xiaohongshu"):
            xhs_key = f"xhs:{post_url}"
            if xhs_key not in downloaded_keys:
                files = _xhs_download(post_url, post_dir)
                downloaded_files.extend(str(f) for f in files)
                downloaded_keys.add(xhs_key)
                _save_state()
        else:
            direct_urls = list(post_dict.get("image_urls", [])) + list(post_dict.get("video_urls", []))
            if direct_urls and direct_dl is not None:
                for url in direct_urls:
                    if url in downloaded_keys:
                        continue
                    try:
                        files = direct_dl.download(url, post_dir)
                        downloaded_files.extend(str(f) for f in files)
                        downloaded_keys.add(url)
                        _save_state()
                    except Exception as exc:
                        errors.append(f"[download] direct failed {url}: {exc}")

        downloaded_keys.add(post_url)
        _save_state()

        downloads_data.append({
            "post_id": post_id,
            "post_title": title,
            "post_dir": str(post_dir),
            "files": downloaded_files,
        })
        print(f"  [download] Post {post_id}: {len(downloaded_files)} file(s) downloaded.")

    print(f"[download] Done. {len(downloads_data)} post(s) processed.")
    return {
        "downloads": downloads_data,
        "errors": errors,
    }


def _xhs_download(post_url: str, dest_dir: Path) -> list[Path]:
    """
    Download a single rednote/xiaohongshu post using XHS-Downloader.
    video_preference='size' fetches the smallest available file (sufficient for audio).
    """
    import asyncio
    import sys

    xhs_dir = Path(__file__).parent.parent.parent.parent / "Tools" / "XHS-Downloader"
    if str(xhs_dir) not in sys.path:
        sys.path.insert(0, str(xhs_dir))

    COOKIE = (
        "id_token=VjEAAPP2Aov5ukHHCzIvd5482pmw5L8O7Cf05ZvqHX+VbALQk6kTUaFgW0S5FJKVyhgfMOn7"
        "/JD2y+rjkMW4rSO4j9Lhchh4ZgL2Pm9zVFt5wh8RJ3+7k0EeeOaXRq/LPYdpmGpq;"
        "x-rednote-holderctry=US;"
        "web_session=040069b1c2fc7e562c63669b39384b2b545fa6;"
        "x-rednote-datactry=SG"
    )

    print(f"  [xhs] Downloading: {post_url}")

    async def _run():
        from source import XHS
        async with XHS(
            cookie=COOKIE,
            work_path=str(dest_dir),
            folder_name=".",
            download_record=False,
            video_preference="size",
        ) as xhs:
            return await xhs.extract(post_url, download=True)

    try:
        result = asyncio.run(_run())
        if not result or not result[0]:
            print(f"  [xhs] Download failed for {post_url}")
    except Exception as exc:
        print(f"  [xhs] Error: {exc}")

    # XHS-Downloader writes files into a "Download/" subfolder inside work_path
    scan_dir = dest_dir / "Download"
    if not scan_dir.exists():
        scan_dir = dest_dir  # fallback
    return [
        p for p in scan_dir.rglob("*")
        if p.is_file() and p.suffix.lower() in _VIDEO_SUFFIXES
    ]


# ---------------------------------------------------------------------------
# Node: transcribe
# ---------------------------------------------------------------------------

def transcribe(state: dict) -> dict:
    """
    DETERMINISTIC node: extract audio → whisperX (Whisper + speaker diarization).
    Reads state["downloads"], writes state["transcripts"].

    Output per segment: [SPEAKER_XX 0.0s] text
    is_multi_speaker flag is set when >1 unique speaker detected.

    Requires: whisperx, ffmpeg, HuggingFace token (for pyannote diarization models)
    """
    import subprocess
    errors: list[str] = []

    try:
        cfg = PipelineConfig.from_dict(json.loads(state.get("config", "{}")))
        downloads_data: list[dict] = _load_list(state, "downloads")
    except Exception as exc:
        errors.append(f"[transcribe] parse error: {exc}")
        return {"transcripts": [], "errors": errors}

    if getattr(cfg, "transcribe", True) is False:
        print("[transcribe] Skipping (transcribe=false in config).")
        return {"transcripts": [], "errors": errors}

    try:
        import whisperx
    except ImportError:
        errors.append("[transcribe] whisperx not installed. Run: pip install whisperx")
        return {"transcripts": [], "errors": errors}

    import torch
    device = "cuda" if torch.cuda.is_available() else "cpu"
    compute_type = "float16" if device == "cuda" else "int8"
    whisper_model_name = getattr(cfg, "whisper_model", "medium")
    hf_token = getattr(cfg, "hf_token", None)

    # Try to get HF token from env/cache if not in config
    if not hf_token:
        try:
            from huggingface_hub import get_token
            hf_token = get_token()
        except Exception:
            pass

    print(f"[transcribe] Loading whisperX model: {whisper_model_name} ({device}/{compute_type})")
    model = whisperx.load_model(whisper_model_name, device, compute_type=compute_type, language="zh")

    transcripts_data = []

    for item in downloads_data:
        post_id = item.get("post_id", "")
        post_title = item.get("post_title", post_id)
        files = item.get("files", [])

        video_files = [Path(f) for f in files if Path(f).suffix.lower() in _VIDEO_SUFFIXES and Path(f).exists()]
        if not video_files:
            post_dir = Path(item.get("post_dir", ""))
            dl_dir = post_dir / "Download" if (post_dir / "Download").exists() else post_dir
            video_files = [p for p in dl_dir.rglob("*") if p.suffix.lower() in _VIDEO_SUFFIXES]

        if not video_files:
            print(f"  [transcribe] No video for {post_id}, skipping.")
            continue

        for video_path in video_files:
            audio_path = video_path.with_suffix(".wav")
            transcript_path = video_path.with_suffix(".txt")

            # Skip if transcript already exists
            if transcript_path.exists() and transcript_path.stat().st_size > 0:
                print(f"  [transcribe] Already done, skipping: {transcript_path.name}")
                transcripts_data.append({
                    "post_id": post_id,
                    "post_title": post_title,
                    "video": str(video_path),
                    "transcript_file": str(transcript_path),
                    "transcript": transcript_path.read_text(encoding="utf-8"),
                    "is_multi_speaker": False,
                })
                continue

            # Extract audio (16kHz mono WAV)
            if not audio_path.exists():
                try:
                    subprocess.run(
                        ["ffmpeg", "-y", "-i", str(video_path),
                         "-vn", "-ar", "16000", "-ac", "1", str(audio_path)],
                        check=True, capture_output=True,
                    )
                    print(f"  [transcribe] Audio extracted: {audio_path.name}")
                except subprocess.CalledProcessError as exc:
                    errors.append(f"[transcribe] ffmpeg failed for {video_path.name}: {exc.stderr.decode()[:200]}")
                    continue

            print(f"  [transcribe] Transcribing: {video_path.name}")
            try:
                # Step 1: Whisper transcription
                audio = whisperx.load_audio(str(audio_path))
                result = model.transcribe(audio, batch_size=8, language="zh")
                seg_count = len(result.get("segments", []))
                print(f"  [transcribe] Whisper: {seg_count} segments detected")

                # Fallback: whisperX VAD may miss low-energy speech; use faster-whisper directly
                if seg_count == 0:
                    print("  [transcribe] 0 segments — falling back to faster-whisper")
                    from faster_whisper import WhisperModel as _FWModel
                    fw_model = _FWModel(whisper_model_name, device="cpu", compute_type="int8")
                    fw_segs, fw_info = fw_model.transcribe(str(audio_path), language="zh", beam_size=5)
                    result = {"segments": [{"start": s.start, "end": s.end, "text": s.text} for s in fw_segs]}
                    print(f"  [transcribe] Fallback: {len(result['segments'])} segments")

                # Step 2: Word-level alignment
                try:
                    align_model, metadata = whisperx.load_align_model(language_code="zh", device=device)
                    result = whisperx.align(result["segments"], align_model, metadata, audio, device)
                except Exception as e:
                    print(f"  [transcribe] Alignment skipped: {e}")

                # Step 3: Speaker diarization via pyannote directly (whisperX 3.8+ removed DiarizationPipeline)
                is_multi_speaker = False
                if hf_token:
                    try:
                        from pyannote.audio import Pipeline as PyannotePipeline
                        import torch as _torch
                        diarize_pipeline = PyannotePipeline.from_pretrained(
                            "pyannote/speaker-diarization-3.1",
                            token=hf_token,
                        )
                        diarize_pipeline.to(_torch.device(device))
                        # Load audio as waveform dict to avoid torchcodec dependency
                        import torchaudio as _ta
                        _waveform, _sr = _ta.load(str(audio_path))
                        _audio_input = {"waveform": _waveform, "sample_rate": _sr}
                        diarize_result = diarize_pipeline(_audio_input)
                        # Handle both old Annotation and new DiarizeOutput (pyannote >= 3.x)
                        _annotation = (
                            diarize_result.speaker_diarization
                            if hasattr(diarize_result, "speaker_diarization")
                            else diarize_result
                        )
                        # Convert pyannote output to whisperX-compatible diarize_segments format
                        import pandas as _pd
                        rows = []
                        for turn, _, speaker in _annotation.itertracks(yield_label=True):
                            rows.append({"start": turn.start, "end": turn.end, "speaker": speaker})
                        diarize_segments = _pd.DataFrame(rows)
                        result = whisperx.assign_word_speakers(diarize_segments, result)
                        speakers = set(
                            seg.get("speaker", "SPEAKER_00")
                            for seg in result.get("segments", [])
                        )
                        is_multi_speaker = len(speakers) > 1
                        print(f"  [transcribe] Speakers: {sorted(speakers)} ({'multi' if is_multi_speaker else 'single'})")
                    except Exception as e:
                        errors.append(f"[transcribe] diarization failed for {video_path.name}: {e}")
                else:
                    print("  [transcribe] No HF token — skipping diarization")

                # Build transcript text
                text_lines = []
                for seg in result.get("segments", []):
                    speaker = seg.get("speaker", "SPEAKER_00")
                    start = seg.get("start", 0)
                    text = seg.get("text", "").strip()
                    if text:
                        line = f"[{speaker} {start:.1f}s] {text}"
                        text_lines.append(line)

                full_text = "\n".join(text_lines)
                transcript_path.write_text(full_text, encoding="utf-8")
                print(f"  [transcribe] Saved: {transcript_path.name} ({len(text_lines)} segments, multi_speaker={is_multi_speaker})")

            except Exception as exc:
                errors.append(f"[transcribe] failed for {video_path.name}: {exc}")
                continue

            transcripts_data.append({
                "post_id": post_id,
                "post_title": post_title,
                "video": str(video_path),
                "transcript_file": str(transcript_path),
                "transcript": full_text,
                "is_multi_speaker": is_multi_speaker,
            })

    print(f"[transcribe] Done. {len(transcripts_data)} transcript(s) generated.")
    return {
        "transcripts": transcripts_data,
        "errors": errors,
    }


# ---------------------------------------------------------------------------
# Node: summarize
# ---------------------------------------------------------------------------

_SUMMARIZE_PROMPT = """\
你是一位内容分析助手。以下是一段小红书视频的语音转录文字，请生成结构化摘要。

视频标题：{title}
是否多人对话：{multi_speaker}

转录内容：
{transcript}

请以 JSON 格式输出，结构如下（直接输出 JSON，不要加 markdown 代码块）：
{{
  "summary": "2-4句话的核心内容概括",
  "key_points": ["要点1", "要点2", "要点3"],
  "topic": "视频主题标签（如：美食教程、健康养生、美妆发型、理财投资等）",
  "target_audience": "适合哪类人观看",
  "actionable": true/false（视频是否包含可操作的方法/步骤）
}}"""


def summarize(state: dict) -> dict:
    """
    LLM node: generate structured summaries from transcripts.

    Reads  state["transcripts"]  (list[dict] via add reducer)
    Writes state["summaries"]    (list[dict] via add reducer)
           state["errors"]       (list[str]  via add reducer)

    Backend is selected by config.summarize_backend:
        "ollama"  — local Ollama server (model = config.summarize_model)
        "claude"  — Anthropic SDK      (model = config.summarize_model)
        "none"    — disabled (graph routing skips this node, but safe to call)

    Skips videos with empty transcripts (no speech detected).
    """
    errors: list[str] = []

    try:
        cfg = PipelineConfig.from_dict(json.loads(state.get("config", "{}")))
        # Support both new list state and legacy JSON-string state
        raw_transcripts = state.get("transcripts", [])
        if isinstance(raw_transcripts, str):
            transcripts_data: list[dict] = json.loads(raw_transcripts or "[]")
        else:
            transcripts_data = list(raw_transcripts)
    except Exception as exc:
        return {"summaries": [], "errors": [f"[summarize] parse error: {exc}"]}

    if not getattr(cfg, "summarize", True):
        print("[summarize] Skipping (summarize=false in config).")
        return {"summaries": [], "errors": []}

    if not transcripts_data:
        print("[summarize] No transcripts to summarize.")
        return {"summaries": [], "errors": []}

    from blueprints.business_pipelines.content_retriever.llm_backend import SummarizeLlmBackend

    # Validate primary backend config (fail fast before looping)
    try:
        _probe = SummarizeLlmBackend.from_config(cfg)
    except ValueError as exc:
        return {"summaries": [], "errors": [f"[summarize] bad backend config: {exc}"]}

    if not _probe.is_enabled:
        print("[summarize] Backend is 'none' — skipping.")
        return {"summaries": [], "errors": []}

    threshold = getattr(cfg, "summarize_auto_threshold_chars", 60000)
    max_retries = getattr(cfg, "summarize_max_retries", 3)
    retry_delay = getattr(cfg, "summarize_retry_delay", 5.0)
    fallback_backend = getattr(cfg, "summarize_fallback_backend", "") or ""

    summaries_data: list[dict] = []
    print(
        f"[summarize] backend={_probe.backend}, model={_probe.model!r}, "
        f"{len(transcripts_data)} transcript(s), "
        f"threshold={threshold:,} chars"
        + (f", fallback={fallback_backend}" if fallback_backend else "")
    )

    for item in transcripts_data:
        post_id = item.get("post_id", "")
        post_title = item.get("post_title", post_id)
        transcript = item.get("transcript", "").strip()
        is_multi = item.get("is_multi_speaker", False)

        if not transcript:
            print(f"  [summarize] {post_id}: empty transcript, skipping.")
            continue

        # Skip if summary already exists on disk
        transcript_path = item.get("transcript_file", "")
        if transcript_path:
            summary_path = Path(transcript_path).with_suffix(".summary.json")
            if summary_path.exists() and summary_path.stat().st_size > 0:
                print(f"  [summarize] Already done, skipping: {summary_path.name}")
                try:
                    existing = json.loads(summary_path.read_text(encoding="utf-8"))
                    summaries_data.append(existing)
                except Exception:
                    pass
                continue

        # ── Auto backend selection based on transcript length ──────────────
        transcript_chars = len(transcript)
        llm, switched = SummarizeLlmBackend.for_transcript(cfg, transcript_chars)
        if switched:
            print(
                f"  [summarize] {post_id}: transcript {transcript_chars:,} chars > "
                f"threshold {threshold:,} → auto-switch to {llm.backend} ({llm.model})"
            )
        elif transcript_chars > threshold and not fallback_backend:
            # No fallback configured — truncate to threshold with a warning
            print(
                f"  [summarize] {post_id}: transcript {transcript_chars:,} chars > "
                f"threshold {threshold:,} but no fallback configured — truncating."
            )
            transcript = transcript[:threshold] + "\n…[截断]"

        prompt = _SUMMARIZE_PROMPT.format(
            title=post_title,
            multi_speaker="是" if is_multi else "否（单人）",
            transcript=transcript,
        )

        print(f"  [summarize] {post_id}: {post_title[:40]}… ({transcript_chars:,} chars)")
        try:
            raw = llm.complete(prompt, max_retries=max_retries, retry_delay=retry_delay)

            # Parse JSON — be lenient with markdown fences
            try:
                parsed = json.loads(raw)
            except json.JSONDecodeError:
                import re as _re
                m = _re.search(r'\{.*\}', raw, _re.DOTALL)
                parsed = json.loads(m.group()) if m else {"summary": raw}

            summary_record = {
                "post_id": post_id,
                "post_title": post_title,
                "is_multi_speaker": is_multi,
                "video": item.get("video", ""),
                "transcript_file": item.get("transcript_file", ""),
                **parsed,
            }
            summaries_data.append(summary_record)

            # Persist alongside transcript file
            transcript_path = item.get("transcript_file", "")
            if transcript_path:
                summary_path = Path(transcript_path).with_suffix(".summary.json")
                summary_path.write_text(
                    json.dumps(summary_record, ensure_ascii=False, indent=2),
                    encoding="utf-8",
                )
                print(f"  [summarize] Saved: {summary_path.name}")

        except Exception as exc:
            errors.append(f"[summarize] failed for {post_id} after {max_retries} retries: {exc}")

    print(f"[summarize] Done. {len(summaries_data)} summary(ies) generated.")
    return {
        "summaries": summaries_data,
        "errors": errors,
    }


# ---------------------------------------------------------------------------
# Node: analyze
# ---------------------------------------------------------------------------

def analyze(state: dict) -> dict:
    """
    DETERMINISTIC node: run LLM analysis on downloaded files.
    Reads state["config"] and state["downloads"], writes state["analysis"].
    If analyzer == "none", skips and returns empty analysis.
    """
    errors: list[str] = []

    try:
        cfg = PipelineConfig.from_dict(json.loads(state.get("config", "{}")))
        downloads_data: list[dict] = _load_list(state, "downloads")
    except Exception as exc:
        errors.append(f"[analyze] parse error: {exc}")
        return {"analysis": [], "errors": errors}

    if cfg.analyzer.lower() == "none" or not downloads_data:
        print(f"[analyze] Skipping (analyzer={cfg.analyzer!r}).")
        return {"analysis": [], "errors": errors}

    _add_sources_to_path()

    # Build analyzer
    try:
        analyzer_obj = _build_analyzer(cfg)
    except Exception as exc:
        errors.append(f"[analyze] analyzer init error: {exc}")
        return {"analysis": [], "errors": errors}

    analysis_results: list[dict] = []

    for dl in downloads_data:
        post_id = dl.get("post_id", "")
        post_title = dl.get("post_title", "")
        post_dir = Path(dl.get("post_dir", "."))

        result: dict = {
            "post_id": post_id,
            "post_title": post_title,
            "text_summary": "",
            "files": [],
        }

        # Analyze post text
        text_file = post_dir / "text.txt"
        if text_file.exists():
            text = text_file.read_text(encoding="utf-8", errors="replace").strip()
            if text:
                print(f"    [analyze] Summarizing post text for {post_id}...")
                try:
                    result["text_summary"] = analyzer_obj.analyze_text(text)
                except Exception as exc:
                    result["text_summary"] = f"[error: {exc}]"

        # Analyze each downloaded file
        for file_str in dl.get("files", []):
            file_path = Path(file_str)
            suffix = file_path.suffix.lower()
            entry: dict = {"file": file_path.name, "analysis": "", "type": "unknown"}

            if suffix in _IMAGE_SUFFIXES:
                print(f"    [analyze] Image: {file_path.name}")
                entry["type"] = "image"
                try:
                    entry["analysis"] = analyzer_obj.analyze_image(file_path)
                except Exception as exc:
                    entry["analysis"] = f"[error: {exc}]"
            elif suffix in _VIDEO_SUFFIXES:
                print(f"    [analyze] Video: {file_path.name}")
                entry["type"] = "video"
                try:
                    entry["analysis"] = analyzer_obj.analyze_video(file_path)
                except Exception as exc:
                    entry["analysis"] = f"[error: {exc}]"
            else:
                entry["analysis"] = "(unsupported file type)"

            result["files"].append(entry)

        # Save per-post analysis.json
        analysis_file = post_dir / "analysis.json"
        try:
            analysis_file.write_text(
                json.dumps(result, indent=2, ensure_ascii=False),
                encoding="utf-8",
            )
        except Exception as exc:
            errors.append(f"[analyze] could not write analysis.json for {post_id}: {exc}")

        analysis_results.append(result)

    print(f"[analyze] Done. {len(analysis_results)} post(s) analyzed.")
    return {
        "analysis": analysis_results,
        "errors": errors,
    }


def _build_analyzer(cfg: PipelineConfig):
    kind = cfg.analyzer.lower()
    fi = cfg.frame_interval
    mf = cfg.max_frames

    if kind == "claude":
        from analyzers.claude import ClaudeAnalyzer
        model = cfg.analyze_model or "claude-haiku-4-5"
        print(f"[analyze] Analyzer: Claude ({model})")
        return ClaudeAnalyzer(model=model, frame_interval=fi, max_frames=mf)
    elif kind == "gemini":
        from analyzers.gemini import GeminiAnalyzer
        model = cfg.analyze_model or "gemini-2.0-flash"
        print(f"[analyze] Analyzer: Gemini ({model})")
        return GeminiAnalyzer(model=model, frame_interval=fi, max_frames=mf)
    else:  # ollama (default)
        from analyzers.ollama import OllamaAnalyzer
        model = cfg.analyze_model or "gemma4:e4b"
        print(f"[analyze] Analyzer: Ollama ({model})")
        return OllamaAnalyzer(
            vision_model=model,
            text_model=model,
            frame_interval=fi,
            max_frames=mf,
        )


# ---------------------------------------------------------------------------
# Node: report
# ---------------------------------------------------------------------------

def report(state: dict) -> dict:
    """
    DETERMINISTIC node: generate a markdown summary report.
    Reads state["config"], state["downloads"], state["analysis"], state["summaries"],
    writes state["report"].
    """
    errors: list[str] = []

    try:
        cfg = PipelineConfig.from_dict(json.loads(state.get("config", "{}")))
        downloads_data: list[dict] = _load_list(state, "downloads")
        analysis_data: list[dict] = _load_list(state, "analysis")
        summaries_data: list[dict] = _load_list(state, "summaries")
    except Exception as exc:
        errors.append(f"[report] parse error: {exc}")
        return {"report": "", "errors": errors}

    download_dir = Path(cfg.download_dir)

    # Build lookup dicts by post_id
    analysis_by_id: dict[str, dict] = {
        a["post_id"]: a for a in analysis_data if isinstance(a, dict)
    }
    summaries_by_id: dict[str, dict] = {
        s["post_id"]: s for s in summaries_data if isinstance(s, dict)
    }

    from datetime import datetime
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    date_tag = datetime.now().strftime("%Y%m%d_%H%M%S")

    def _human_size(n: int) -> str:
        for unit in ("B", "KB", "MB", "GB", "TB"):
            if n < 1024:
                return f"{n:.1f} {unit}"
            n /= 1024
        return f"{n:.1f} PB"

    total_files = 0
    total_bytes = 0
    sections: list[str] = []

    for dl in downloads_data:
        post_id = dl.get("post_id", "")
        post_title = dl.get("post_title", post_id)
        post_dir = Path(dl.get("post_dir", "."))
        files = [Path(f) for f in dl.get("files", [])]

        post_file_count = len(files)
        post_bytes = sum(f.stat().st_size for f in files if f.exists())
        total_files += post_file_count
        total_bytes += post_bytes

        lines: list[str] = []
        lines.append(f"### {post_title}")
        lines.append(f"**Post ID:** `{post_id}`  ")
        lines.append(f"**Files:** {post_file_count}  |  **Size:** {_human_size(post_bytes)}")
        lines.append("")

        # Text excerpt
        text_file = post_dir / "text.txt"
        if text_file.exists():
            try:
                full_text = text_file.read_text(encoding="utf-8", errors="replace")
                excerpt = full_text[:300].strip()
                if len(full_text) > 300:
                    excerpt += "..."
                if excerpt:
                    lines.append("**Text excerpt:**")
                    lines.append(f"> {excerpt.replace(chr(10), chr(10) + '> ')}")
                    lines.append("")
            except OSError:
                pass

        # File list
        if files:
            lines.append("**Downloaded files:**")
            for f in files:
                size_str = _human_size(f.stat().st_size) if f.exists() else "?"
                lines.append(f"- `{f.name}` ({size_str})")
            lines.append("")

        # LLM summary
        summary = summaries_by_id.get(post_id)
        if summary:
            lines.append("**摘要（LLM）：**")
            if summary.get("summary"):
                lines.append(f"> {summary['summary']}")
                lines.append("")
            topic = summary.get("topic", "")
            audience = summary.get("target_audience", "")
            actionable = summary.get("actionable")
            meta_parts = []
            if topic:
                meta_parts.append(f"主题：{topic}")
            if audience:
                meta_parts.append(f"受众：{audience}")
            if actionable is not None:
                meta_parts.append(f"可操作：{'是' if actionable else '否'}")
            if summary.get("is_multi_speaker"):
                meta_parts.append("多人对话")
            if meta_parts:
                lines.append("  ".join(meta_parts))
                lines.append("")
            key_points = summary.get("key_points", [])
            if key_points:
                lines.append("**要点：**")
                for kp in key_points:
                    lines.append(f"- {kp}")
                lines.append("")

        # VLM analysis
        analysis = analysis_by_id.get(post_id)
        if analysis:
            text_summary = analysis.get("text_summary", "").strip()
            if text_summary:
                lines.append("**VLM text summary:**")
                lines.append(f"> {text_summary.replace(chr(10), chr(10) + '> ')}")
                lines.append("")

            file_analyses = analysis.get("files", [])
            if file_analyses:
                lines.append("**VLM file analysis:**")
                for entry in file_analyses:
                    fname = entry.get("file", "unknown")
                    ftype = entry.get("type", "")
                    fanalysis = entry.get("analysis", "").strip()
                    type_label = f" [{ftype}]" if ftype else ""
                    lines.append(f"- **`{fname}`**{type_label}")
                    if fanalysis:
                        indented = fanalysis.replace("\n", "\n  ")
                        lines.append(f"  {indented}")
                lines.append("")

        sections.append("\n".join(lines))

    header_lines = [
        "# Content Retriever Report",
        "",
        "| Field | Value |",
        "|-------|-------|",
        f"| Generated | {timestamp} |",
        f"| Download directory | `{download_dir}` |",
        f"| Total posts | {len(downloads_data)} |",
        f"| Total media files | {total_files} |",
        f"| Total size | {_human_size(total_bytes)} |",
        "",
        "---",
        "",
    ]

    if not sections:
        header_lines.append("*No posts processed.*")
        report_text = "\n".join(header_lines)
    else:
        report_text = "\n".join(header_lines) + "\n\n".join(sections)

    # Save report file
    try:
        download_dir.mkdir(parents=True, exist_ok=True)
        report_path = download_dir / f"report_{date_tag}.md"
        report_path.write_text(report_text, encoding="utf-8")
        print(f"[report] Saved to {report_path}")
    except Exception as exc:
        errors.append(f"[report] could not save report: {exc}")

    return {
        "report": report_text,
        "errors": errors,
    }
