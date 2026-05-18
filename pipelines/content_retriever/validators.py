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

from pipelines.content_retriever.config import PipelineConfig
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
    errors: list[str] = json.loads(state.get("errors") or "[]")

    cfg_raw = state.get("config", "{}")
    try:
        cfg = PipelineConfig.from_dict(json.loads(cfg_raw))
    except Exception as exc:
        errors.append(f"[fetch] config parse error: {exc}")
        return {"posts": "[]", "errors": json.dumps(errors)}

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
        return {"posts": "[]", "errors": json.dumps(errors)}

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
        "posts": json.dumps(posts_data, ensure_ascii=False),
        "errors": json.dumps(errors),
    }


# ---------------------------------------------------------------------------
# Node: download
# ---------------------------------------------------------------------------

def download(state: dict) -> dict:
    """
    DETERMINISTIC node: download media from fetched posts.
    Reads state["config"] and state["posts"], writes state["downloads"].
    """
    errors: list[str] = json.loads(state.get("errors") or "[]")

    try:
        cfg = PipelineConfig.from_dict(json.loads(state.get("config", "{}")))
        posts_data: list[dict] = json.loads(state.get("posts", "[]"))
    except Exception as exc:
        errors.append(f"[download] parse error: {exc}")
        return {"downloads": "[]", "errors": json.dumps(errors)}

    if not posts_data:
        print("[download] No posts to download.")
        return {"downloads": "[]", "errors": json.dumps(errors)}

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

    def _save_state():
        state_file.write_text(
            json.dumps(sorted(downloaded_keys), indent=2, ensure_ascii=False),
            encoding="utf-8",
        )

    downloads_data: list[dict] = []

    for post_dict in posts_data:
        post_id = post_dict["id"]
        title = post_dict.get("title", "")
        post_url = post_dict["url"]
        post_text = post_dict.get("text", "") or ""
        extra = post_dict.get("extra", {})

        slug = _safe_title(title)
        post_dir = download_dir / f"{post_id}_{slug}"
        post_dir.mkdir(parents=True, exist_ok=True)

        # Save text
        text_file = post_dir / "text.txt"
        text_file.write_text(post_text, encoding="utf-8")

        downloaded_files: list[str] = []

        # (a) Pixeldrain links from post text + comment texts
        all_texts = [post_text]
        all_texts.extend(extra.get("comment_texts", []))
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

        # (b) rednote: use yt-dlp; other sources: direct download image/video URLs
        if extra.get("use_ytdlp") and post_dict.get("source") in ("rednote", "xiaohongshu"):
            ytdlp_key = f"ytdlp:{post_url}"
            if ytdlp_key not in downloaded_keys:
                files = _ytdlp_download(post_url, post_dir, extra.get("chrome_profile", ""))
                downloaded_files.extend(str(f) for f in files)
                downloaded_keys.add(ytdlp_key)
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
        "downloads": json.dumps(downloads_data, ensure_ascii=False),
        "errors": json.dumps(errors),
    }


def _ytdlp_download(post_url: str, dest_dir: Path, chrome_profile: str) -> list[Path]:
    import subprocess
    import shutil
    ytdlp = shutil.which("yt-dlp") or "yt-dlp"
    cmd = [
        ytdlp,
        "--no-playlist",
        "--write-info-json",
        "-o", str(dest_dir / "%(title)s.%(ext)s"),
    ]
    if chrome_profile:
        cmd += ["--cookies-from-browser", f"chrome:{chrome_profile}"]
    cmd.append(post_url)

    print(f"  [yt-dlp] Downloading: {post_url}")
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
        if result.returncode != 0:
            print(f"  [yt-dlp] Warning: {result.stderr[-300:] if result.stderr else 'unknown error'}")
    except subprocess.TimeoutExpired:
        print(f"  [yt-dlp] Timeout for {post_url}")
    except Exception as exc:
        print(f"  [yt-dlp] Error: {exc}")

    return [
        p for p in dest_dir.iterdir()
        if p.suffix.lower() in _IMAGE_SUFFIXES | _VIDEO_SUFFIXES
    ]


# ---------------------------------------------------------------------------
# Node: analyze
# ---------------------------------------------------------------------------

def analyze(state: dict) -> dict:
    """
    DETERMINISTIC node: run LLM analysis on downloaded files.
    Reads state["config"] and state["downloads"], writes state["analysis"].
    If analyzer == "none", skips and returns empty analysis.
    """
    errors: list[str] = json.loads(state.get("errors") or "[]")

    try:
        cfg = PipelineConfig.from_dict(json.loads(state.get("config", "{}")))
        downloads_data: list[dict] = json.loads(state.get("downloads", "[]"))
    except Exception as exc:
        errors.append(f"[analyze] parse error: {exc}")
        return {"analysis": "[]", "errors": json.dumps(errors)}

    if cfg.analyzer.lower() == "none" or not downloads_data:
        print(f"[analyze] Skipping (analyzer={cfg.analyzer!r}).")
        return {"analysis": "[]", "errors": json.dumps(errors)}

    _add_sources_to_path()

    # Build analyzer
    try:
        analyzer_obj = _build_analyzer(cfg)
    except Exception as exc:
        errors.append(f"[analyze] analyzer init error: {exc}")
        return {"analysis": "[]", "errors": json.dumps(errors)}

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
        "analysis": json.dumps(analysis_results, ensure_ascii=False),
        "errors": json.dumps(errors),
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
    Reads state["config"], state["downloads"], state["analysis"],
    writes state["report"].
    """
    errors: list[str] = json.loads(state.get("errors") or "[]")

    try:
        cfg = PipelineConfig.from_dict(json.loads(state.get("config", "{}")))
        downloads_data: list[dict] = json.loads(state.get("downloads", "[]"))
        analysis_data: list[dict] = json.loads(state.get("analysis", "[]"))
    except Exception as exc:
        errors.append(f"[report] parse error: {exc}")
        return {"report": "", "errors": json.dumps(errors)}

    download_dir = Path(cfg.download_dir)

    # Build analysis lookup by post_id
    analysis_by_id: dict[str, dict] = {
        a["post_id"]: a for a in analysis_data if isinstance(a, dict)
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
        "errors": json.dumps(errors),
    }
