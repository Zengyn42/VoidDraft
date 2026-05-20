"""
Pixeldrain downloader — supports single files (/u/{id}) and albums (/l/{id}).

Authentication: Basic Auth with username="" and api_key as password.
API key is loaded from config (pixeldrain_api_key field), never hardcoded here.
"""

import re
import time
from pathlib import Path

import httpx

from .base import FileDownloader

_CHUNK_SIZE = 512 * 1024  # 512 KB


def _fmt_mb(n_bytes: int) -> str:
    return f"{n_bytes / (1024 * 1024):.1f}MB"


class PixeldrainDownloader(FileDownloader):
    """Download single files (/u/{id}) or albums (/l/{id}) from pixeldrain.com."""

    def __init__(self, api_key: str | None = None) -> None:
        # Pixeldrain Basic Auth: username="" password=api_key
        auth = ("", api_key) if api_key else None
        self._client = httpx.Client(timeout=120, follow_redirects=True, auth=auth)

    # ------------------------------------------------------------------
    # FileDownloader interface
    # ------------------------------------------------------------------

    def can_handle(self, url: str) -> bool:
        return "pixeldrain.com" in url

    def download(self, url: str, dest_dir: Path, filename: str | None = None) -> list[Path]:
        dest_dir.mkdir(parents=True, exist_ok=True)

        list_match = re.search(r"pixeldrain\.com/l/([A-Za-z0-9_-]+)", url)
        file_match = re.search(r"pixeldrain\.com/u/([A-Za-z0-9_-]+)", url)

        if list_match:
            return self._download_album(list_match.group(1), dest_dir)
        elif file_match:
            return self._download_single(file_match.group(1), dest_dir, filename)
        else:
            print(f"[pixeldrain] Could not parse URL: {url}")
            return []

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def list_album(self, list_id: str) -> list[dict]:
        """
        Return the file metadata list for a Pixeldrain album (/l/{list_id}).

        Each dict contains: id, name, size (bytes), and other API fields.
        Returns [] on error.
        """
        album_url = f"https://pixeldrain.com/api/list/{list_id}"
        try:
            resp = self._client.get(album_url)
            resp.raise_for_status()
            return resp.json().get("files", [])
        except Exception as exc:
            print(f"[pixeldrain] list_album({list_id}) failed: {exc}")
            return []

    def _get_file_info(self, file_id: str) -> dict | None:
        """Return file metadata dict from the pixeldrain API, or None on error."""
        info_url = f"https://pixeldrain.com/api/file/{file_id}/info"
        try:
            resp = self._client.get(info_url)
        except httpx.RequestError as exc:
            print(f"[pixeldrain] Request error fetching info for {file_id}: {exc}")
            return None

        if resp.status_code in (403, 404):
            print(
                f"[pixeldrain] Warning: {resp.status_code} for file info {file_id} — skipping"
            )
            return None
        try:
            resp.raise_for_status()
        except httpx.HTTPStatusError as exc:
            print(f"[pixeldrain] HTTP error for file info {file_id}: {exc}")
            return None

        return resp.json()

    def _stream_file(self, file_id: str, dest_path: Path, display_name: str) -> bool:
        """Stream-download *file_id* to *dest_path*. Returns True on success."""
        dl_url = f"https://pixeldrain.com/api/file/{file_id}?download"
        try:
            with self._client.stream("GET", dl_url) as resp:
                if resp.status_code in (403, 404):
                    print(
                        f"[pixeldrain] Warning: {resp.status_code} for {display_name} — skipping"
                    )
                    return False
                resp.raise_for_status()

                total = int(resp.headers.get("content-length", 0))
                downloaded = 0

                with dest_path.open("wb") as fh:
                    for chunk in resp.iter_bytes(chunk_size=_CHUNK_SIZE):
                        fh.write(chunk)
                        downloaded += len(chunk)
                        if total:
                            print(
                                f"\r[pixeldrain] {display_name} "
                                f"({_fmt_mb(downloaded)}/{_fmt_mb(total)})",
                                end="",
                                flush=True,
                            )
                        else:
                            print(
                                f"\r[pixeldrain] {display_name} ({_fmt_mb(downloaded)})",
                                end="",
                                flush=True,
                            )
                print()  # newline after progress
        except httpx.RequestError as exc:
            print(f"\n[pixeldrain] Request error downloading {display_name}: {exc}")
            if dest_path.exists():
                dest_path.unlink(missing_ok=True)
            return False
        except httpx.HTTPStatusError as exc:
            print(f"\n[pixeldrain] HTTP error downloading {display_name}: {exc}")
            if dest_path.exists():
                dest_path.unlink(missing_ok=True)
            return False

        return True

    def _download_single(
        self,
        file_id: str,
        dest_dir: Path,
        override_filename: str | None = None,
    ) -> list[Path]:
        info = self._get_file_info(file_id)
        if info is None:
            return []

        name = override_filename or info.get("name") or file_id
        dest_path = dest_dir / name

        success = self._stream_file(file_id, dest_path, name)
        return [dest_path] if success else []

    def _download_album(self, list_id: str, dest_dir: Path) -> list[Path]:
        """Fetch album metadata and download every file in it."""
        album_url = f"https://pixeldrain.com/api/list/{list_id}"
        try:
            resp = self._client.get(album_url)
        except httpx.RequestError as exc:
            print(f"[pixeldrain] Request error fetching album {list_id}: {exc}")
            return []

        if resp.status_code in (403, 404):
            print(
                f"[pixeldrain] Warning: {resp.status_code} for album {list_id} — skipping"
            )
            return []
        try:
            resp.raise_for_status()
        except httpx.HTTPStatusError as exc:
            print(f"[pixeldrain] HTTP error for album {list_id}: {exc}")
            return []

        album_data = resp.json()
        files = album_data.get("files", [])
        if not files:
            print(f"[pixeldrain] Album {list_id} has no files")
            return []

        print(f"[pixeldrain] Album {list_id}: {len(files)} file(s)")
        downloaded_paths: list[Path] = []

        for entry in files:
            fid = entry.get("id", "")
            fname = entry.get("name") or fid
            if not fid:
                continue
            dest_path = dest_dir / fname
            success = self._stream_file(fid, dest_path, fname)
            if success:
                downloaded_paths.append(dest_path)
            time.sleep(0.5)

        return downloaded_paths
