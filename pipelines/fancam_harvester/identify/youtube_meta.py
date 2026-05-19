"""
YouTube metadata fetcher.

Given a YouTube video ID, returns title, channel, upload_date, and
description using the YouTube Data API v3.

Falls back to yt-dlp if API key is not configured (slower but auth-free).
"""

from __future__ import annotations

import logging
import re
import subprocess
import json
from dataclasses import dataclass
from typing import Optional

logger = logging.getLogger(__name__)

_YT_ID_PATTERN = re.compile(r"(?:youtube@|youtu\.be/|v=)([A-Za-z0-9_-]{11})")


@dataclass
class YouTubeMeta:
    video_id: str
    title: str
    channel: str
    upload_date: str    # YYYYMMDD
    description: str
    duration_sec: float


def extract_yt_id(text: str) -> Optional[str]:
    """
    Extract a YouTube video ID from a filename or URL.

    Handles:
        【youtube@s6JQrtlSuC0】
        https://youtu.be/s6JQrtlSuC0
        https://www.youtube.com/watch?v=s6JQrtlSuC0
    """
    m = _YT_ID_PATTERN.search(text)
    return m.group(1) if m else None


def fetch_via_api(video_id: str, api_key: str) -> Optional[YouTubeMeta]:
    """Fetch metadata using YouTube Data API v3."""
    import urllib.request
    import urllib.parse

    url = (
        "https://www.googleapis.com/youtube/v3/videos"
        f"?part=snippet,contentDetails&id={video_id}&key={api_key}"
    )
    try:
        with urllib.request.urlopen(url, timeout=10) as resp:
            data = json.loads(resp.read())
        items = data.get("items", [])
        if not items:
            return None
        snippet = items[0]["snippet"]
        content = items[0]["contentDetails"]

        # Parse ISO 8601 duration → seconds
        dur_str = content.get("duration", "PT0S")
        dur_sec = _parse_iso_duration(dur_str)

        upload_raw = snippet.get("publishedAt", "")[:10].replace("-", "")

        return YouTubeMeta(
            video_id=video_id,
            title=snippet.get("title", ""),
            channel=snippet.get("channelTitle", ""),
            upload_date=upload_raw,
            description=snippet.get("description", "")[:500],
            duration_sec=dur_sec,
        )
    except Exception as e:
        logger.warning(f"YouTube API fetch failed for {video_id}: {e}")
        return None


def fetch_via_ytdlp(video_id: str) -> Optional[YouTubeMeta]:
    """Fallback: use yt-dlp --dump-json (no API key needed)."""
    url = f"https://www.youtube.com/watch?v={video_id}"
    try:
        result = subprocess.run(
            ["yt-dlp", "--dump-json", "--no-playlist", url],
            capture_output=True, text=True, timeout=30
        )
        if result.returncode != 0:
            logger.warning(f"yt-dlp dump-json failed: {result.stderr[:200]}")
            return None
        data = json.loads(result.stdout)
        return YouTubeMeta(
            video_id=video_id,
            title=data.get("title", ""),
            channel=data.get("uploader", ""),
            upload_date=data.get("upload_date", ""),
            description=(data.get("description") or "")[:500],
            duration_sec=float(data.get("duration") or 0),
        )
    except Exception as e:
        logger.warning(f"yt-dlp fallback failed for {video_id}: {e}")
        return None


def fetch(video_id: str, api_key: str = "") -> Optional[YouTubeMeta]:
    """Fetch YouTube metadata, preferring API if key is set."""
    if api_key:
        meta = fetch_via_api(video_id, api_key)
        if meta:
            return meta
    return fetch_via_ytdlp(video_id)


def _parse_iso_duration(s: str) -> float:
    """Parse PT1H2M3S → seconds."""
    m = re.match(r"PT(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?", s)
    if not m:
        return 0.0
    h, mn, sec = (int(x or 0) for x in m.groups())
    return h * 3600 + mn * 60 + sec
