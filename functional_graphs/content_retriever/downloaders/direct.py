"""
Direct URL downloader — streams any URL with httpx as the universal fallback.
"""

import re
from pathlib import Path
from urllib.parse import urlparse

import httpx

from .base import FileDownloader

_CHUNK_SIZE = 512 * 1024  # 512 KB
_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)
_SAFE_NAME_RE = re.compile(r'[\\/:*?"<>|]')


def _fmt_mb(n_bytes: int) -> str:
    return f"{n_bytes / (1024 * 1024):.1f}MB"


def _sanitize(name: str) -> str:
    return _SAFE_NAME_RE.sub("_", name)


def _derive_filename(url: str, content_type: str) -> str:
    """Derive a safe filename from the URL path, fixing missing extensions."""
    path = urlparse(url).path
    segments = [s for s in path.split("/") if s]
    raw_name = segments[-1] if segments else "download"
    name = _sanitize(raw_name)

    suffix = Path(name).suffix.lower()
    ct = content_type.lower()

    if not suffix:
        if ct.startswith("image/"):
            name += ".jpg"
        elif ct.startswith("video/"):
            name += ".mp4"

    return name


class DirectDownloader(FileDownloader):
    """Fallback downloader: streams any URL directly with httpx."""

    def __init__(self) -> None:
        self._client = httpx.Client(
            timeout=60,
            follow_redirects=True,
            headers={"User-Agent": _USER_AGENT},
        )

    # ------------------------------------------------------------------
    # FileDownloader interface
    # ------------------------------------------------------------------

    def can_handle(self, url: str) -> bool:
        # Acts as the universal fallback; always True
        return True

    def download(self, url: str, dest_dir: Path, filename: str | None = None) -> list[Path]:
        dest_dir.mkdir(parents=True, exist_ok=True)

        try:
            with self._client.stream("GET", url) as resp:
                resp.raise_for_status()

                content_type = resp.headers.get("content-type", "")
                name = filename or _derive_filename(url, content_type)
                dest_path = dest_dir / name

                total = int(resp.headers.get("content-length", 0))
                downloaded = 0

                with dest_path.open("wb") as fh:
                    for chunk in resp.iter_bytes(chunk_size=_CHUNK_SIZE):
                        fh.write(chunk)
                        downloaded += len(chunk)
                        if total:
                            print(
                                f"\r[direct] {name} "
                                f"({_fmt_mb(downloaded)}/{_fmt_mb(total)})",
                                end="",
                                flush=True,
                            )
                        else:
                            print(
                                f"\r[direct] {name} ({_fmt_mb(downloaded)})",
                                end="",
                                flush=True,
                            )
                print()  # newline after progress

        except httpx.HTTPStatusError as exc:
            print(f"\n[direct] HTTP {exc.response.status_code} for {url} — skipping")
            return []
        except httpx.RequestError as exc:
            print(f"\n[direct] Request error for {url}: {exc} — skipping")
            return []
        except Exception as exc:
            print(f"\n[direct] Unexpected error downloading {url}: {exc} — skipping")
            return []

        return [dest_path]
