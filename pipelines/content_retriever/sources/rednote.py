"""
小红书 (RedNote) source — keyword search + user profile scraping.

Media download is handled by the pipeline via yt-dlp (bypasses CDN hotlink protection).
This module is responsible for:
  1. Building search / listing URLs
  2. Using Scrapling StealthyFetcher with scroll for lazy-loaded content
  3. Fetching post links, title, and body text per post
  4. Exposing image/video URLs to the pipeline for yt-dlp download

Cookie persistence is achieved via user_data_dir (Chrome profile directory).
"""

import re
import time
from typing import Iterator
from urllib.parse import quote, urlparse

from scrapling.fetchers import StealthyFetcher

from .base import Post, PlatformSource

_XHS_BASE = "https://www.xiaohongshu.com"
_SEARCH_URL = "https://www.xiaohongshu.com/search_result?keyword={kw}&type=51"


# ─── Helpers ─────────────────────────────────────────────────────────────────

def _extract_post_id(url: str) -> str:
    match = re.search(r"/explore/([a-zA-Z0-9]+)", url)
    return match.group(1) if match else urlparse(url).path.rstrip("/").split("/")[-1]


def _absolute_url(href: str) -> str:
    if href.startswith("http"):
        return href
    if href.startswith("//"):
        return "https:" + href
    return _XHS_BASE + href


def _make_scroll_action(scroll_count: int, wait_ms: int = 2500):
    """
    Returns a page_action function that scrolls scroll_count times
    on a Playwright page, waiting wait_ms ms per scroll to trigger lazy-loading.
    """
    def _action(page):
        for _ in range(scroll_count):
            page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
            page.wait_for_timeout(wait_ms)
    return _action


# ─── Source class ─────────────────────────────────────────────────────────────

class RedNoteSource(PlatformSource):
    """
    小红书 content source. Supports two modes:
      - search_keyword: keyword search, fetches top results
      - target_url: directly specified user profile or listing URL

    Both can be set simultaneously; search_keyword takes priority.
    """

    def __init__(
        self,
        user_data_dir: str,
        search_keyword: str | None = None,
        target_url: str | None = None,
        request_delay: float = 2.0,
        max_scroll: int = 5,
    ) -> None:
        if not search_keyword and not target_url:
            raise ValueError("RedNoteSource: must specify search_keyword or target_url")

        self.user_data_dir = user_data_dir
        self.search_keyword = search_keyword
        self.target_url = target_url
        self.request_delay = request_delay
        self.max_scroll = max_scroll

    # ── Build entry URL ───────────────────────────────────────────────────────

    def _build_index_url(self) -> str:
        if self.search_keyword:
            encoded = quote(self.search_keyword)
            url = _SEARCH_URL.format(kw=encoded)
            print(f"[rednote] Search keyword: {self.search_keyword!r}")
            print(f"[rednote] URL: {url}")
            return url
        return self.target_url  # type: ignore[return-value]

    # ── Scrapling fetch ───────────────────────────────────────────────────────

    def _fetch_with_scroll(self, url: str) -> object:
        """Load page and scroll max_scroll times to trigger lazy-loading."""
        print(f"[rednote] Loading page (scrolling {self.max_scroll} times)...")
        page = StealthyFetcher.fetch(
            url,
            user_data_dir=self.user_data_dir,
            headless=True,
            network_idle=True,
            timeout=90000,
            page_action=_make_scroll_action(self.max_scroll),
        )
        return page

    def _fetch_post(self, url: str) -> object:
        """Load a single post page (no scrolling needed)."""
        return StealthyFetcher.fetch(
            url,
            user_data_dir=self.user_data_dir,
            headless=True,
            network_idle=True,
            timeout=60000,
        )

    # ── Link extraction ───────────────────────────────────────────────────────

    def _collect_post_links(self, page, max_posts: int) -> list[str]:
        """Extract post URLs from listing/search results in ranking order."""
        seen: set[str] = set()
        links: list[str] = []

        # 小红书 search results and user profiles use /explore/{id} format
        anchors = page.css("a[href*='/explore/']")
        for anchor in anchors:
            href = anchor.attrib.get("href", "")
            if not href:
                continue
            abs_url = _absolute_url(href).split("?")[0].split("#")[0]
            if abs_url not in seen:
                seen.add(abs_url)
                links.append(abs_url)
                if len(links) >= max_posts:
                    break

        print(f"[rednote] Found {len(links)} post link(s)")
        return links

    # ── Single post content extraction ───────────────────────────────────────

    def _scrape_post(self, post_url: str) -> "Post | None":
        post_id = _extract_post_id(post_url)
        try:
            page = self._fetch_post(post_url)
        except Exception as exc:
            print(f"[rednote] Post load failed {post_url}: {exc}")
            return None

        # Title
        title = ""
        for sel in ("h1", ".title", "[class*='title']", "meta[property='og:title']"):
            el = page.css_first(sel)
            if el:
                title = (el.attrib.get("content") or el.inner_text() or "").strip()
                if title:
                    break

        # Body text
        text = ""
        for sel in (".note-text", ".desc", "[class*='desc']", "#detail-desc", ".content"):
            el = page.css_first(sel)
            if el:
                text = (el.inner_text() or "").strip()
                if text:
                    break

        # Image URLs (for reference; actual download uses yt-dlp)
        image_urls: list[str] = []
        for img in page.css(
            "img[src*='xhscdn'], img[src*='ci.xiaohongshu'], img[src*='sns-webpic']"
        ):
            src = img.attrib.get("src", "")
            if src and src not in image_urls:
                image_urls.append(src)

        # Video URLs
        video_urls: list[str] = []
        for sel in ("video source[src]", "video[src]"):
            for el in page.css(sel):
                src = el.attrib.get("src", "")
                if src and src not in video_urls:
                    video_urls.append(src)

        print(
            f"[rednote] Post {post_id}: title={title[:30]!r}, "
            f"imgs={len(image_urls)}, vids={len(video_urls)}"
        )

        return Post(
            id=post_id,
            title=title or post_id,
            url=post_url,
            text=text,
            source="rednote",
            image_urls=image_urls,
            video_urls=video_urls,
            extra={
                "use_ytdlp": True,       # signal pipeline to use yt-dlp for download
                "chrome_profile": self.user_data_dir,
                "search_keyword": self.search_keyword,
            },
        )

    # ── Public interface ──────────────────────────────────────────────────────

    def get_posts(self, max_posts: int) -> Iterator[Post]:
        index_url = self._build_index_url()

        try:
            index_page = self._fetch_with_scroll(index_url)
        except Exception as exc:
            print(f"[rednote] Index page load failed: {exc}")
            return

        post_links = self._collect_post_links(index_page, max_posts)

        collected = 0
        for post_url in post_links:
            if collected >= max_posts:
                break
            post = self._scrape_post(post_url)
            if post is not None:
                yield post
                collected += 1
            time.sleep(self.request_delay)
