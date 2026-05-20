"""
小红书 (RedNote) source — profile & favorites scraping via Playwright.

Modes:
  - favorites (default): user's saved/collected notes via ?tab=fav&subTab=note
    Uses API interception of note/collect/page — returns xsec_token directly.
  - posted: user's own published notes (DOM-based, SSR-rendered)
  - target_url: directly specified listing URL (DOM-based)
  - search_keyword: keyword search on xiaohongshu.com (DOM-based)

Cookies are injected at runtime — no persistent Chrome profile needed.
"""

from __future__ import annotations

import asyncio
import re
import time
from typing import Iterator

from .base import Post, PlatformSource

_XHS_BASE = "https://www.xiaohongshu.com"
_REDNOTE_BASE = "https://www.rednote.com"
_CHROME_BIN = "/usr/bin/google-chrome"

# Default credentials directory (under EdenGateway, not in source code)
_DEFAULT_CREDS_DIR = "/home/kingy/Foundation/EdenGateway/rednote/accounts"


def _load_cookies(credentials_file: str | None) -> list[dict]:
    """
    Load cookies from a JSON credentials file.

    The file lives under EdenGateway/rednote/accounts/<name>.json and contains:
      {
        "account_id": "...",
        "cookies": [ {name, value, domain, path, secure, httpOnly}, ... ]
      }

    If credentials_file is None or missing, returns [] and Playwright will
    run without auth (useful for public profiles).
    """
    import json, pathlib

    if not credentials_file:
        return []

    path = pathlib.Path(credentials_file)
    if not path.is_absolute():
        path = pathlib.Path(_DEFAULT_CREDS_DIR) / path

    if not path.exists():
        print(f"[rednote] WARNING: credentials file not found: {path}")
        return []

    data = json.loads(path.read_text(encoding="utf-8"))
    cookies = data.get("cookies", [])
    print(f"[rednote] Loaded {len(cookies)} cookies from {path.name}")
    return cookies


def _rednote_to_xhs(url: str) -> str:
    """Convert rednote.com URL to xiaohongshu.com (shared backend)."""
    return url.replace("www.rednote.com", "www.xiaohongshu.com")


def _extract_note_id(url: str) -> str | None:
    m = re.search(r"(?:explore|discovery/item)/([0-9a-f]+)", url)
    return m.group(1) if m else None


class RedNoteSource(PlatformSource):
    """
    小红书 content source using Playwright (no persistent Chrome profile needed).

    Args:
        user_data_dir: Unused (kept for config compat). Cookies are injected directly.
        account_user_id: User ID to scrape.
        mode: "favorites" (saved notes) | "posted" (published notes) | "target_url" | "search"
        target_url: Direct listing URL (used when mode="target_url").
        search_keyword: Keyword (used when mode="search").
        max_scroll: Scroll rounds for pagination.
        request_delay: Seconds between yielded posts.
        video_only: If True, skip image-only posts.
    """

    def __init__(
        self,
        user_data_dir: str = "",
        account_user_id: str | None = None,
        mode: str = "favorites",
        target_url: str | None = None,
        search_keyword: str | None = None,
        max_scroll: int = 20,
        request_delay: float = 1.5,
        video_only: bool = True,
        credentials_file: str | None = None,
    ) -> None:
        self.user_data_dir = user_data_dir
        self.account_user_id = account_user_id
        self.mode = mode
        self.target_url = target_url
        self.search_keyword = search_keyword
        self.max_scroll = max_scroll
        self.request_delay = request_delay
        self.video_only = video_only
        # Load cookies from EdenGateway credentials file (not hardcoded)
        self._cookies = _load_cookies(credentials_file)

    def _build_index_url(self) -> str:
        uid = self.account_user_id or ""
        if self.mode == "favorites":
            return f"{_REDNOTE_BASE}/user/profile/{uid}?tab=fav&subTab=note"
        if self.mode == "posted":
            return f"{_REDNOTE_BASE}/user/profile/{uid}"
        if self.mode == "target_url" and self.target_url:
            return self.target_url
        if self.mode == "search" and self.search_keyword:
            from urllib.parse import quote
            return f"{_XHS_BASE}/search_result?keyword={quote(self.search_keyword)}&type=51"
        raise ValueError(f"RedNoteSource: invalid mode={self.mode!r} or missing params")

    # ------------------------------------------------------------------
    # Favorites mode: intercept note/collect/page API responses
    # ------------------------------------------------------------------
    async def _collect_favorites_async(self, index_url: str, max_posts: int) -> list[dict]:
        """
        Navigate to ?tab=fav&subTab=note, intercept note/collect/page API responses.
        Scroll to trigger pagination. Returns list of post info dicts with xsec_token.
        """
        from playwright.async_api import async_playwright

        collected: dict[str, dict] = {}
        has_more = True

        async with async_playwright() as p:
            browser = await p.chromium.launch(
                executable_path=_CHROME_BIN,
                headless=True,
                args=["--no-sandbox", "--disable-dev-shm-usage"],
            )
            ctx = await browser.new_context(
                viewport={"width": 1280, "height": 900},
                service_workers="block",
            )
            page = await ctx.new_page()
            await ctx.add_cookies(self._cookies)

            # Capture note/collect/page API responses
            async def on_response(resp):
                nonlocal has_more
                if "note/collect/page" not in resp.url:
                    return
                try:
                    body = await resp.json()
                    data = body.get("data", {})
                    notes = data.get("notes", [])
                    has_more = data.get("has_more", False)
                    print(f"[rednote][api] collect/page: {len(notes)} notes, has_more={has_more}")
                    for note in notes:
                        note_id = note.get("note_id", "")
                        if not note_id or note_id in collected:
                            continue
                        xsec = note.get("xsec_token", "")
                        title = note.get("display_title", "") or note_id
                        note_type = str(note.get("type", "")).lower()
                        xhs_url = (
                            f"{_XHS_BASE}/explore/{note_id}"
                            + (f"?xsec_token={xsec}" if xsec else "")
                        )
                        collected[note_id] = {
                            "id": note_id,
                            "title": title[:80],
                            "url": xhs_url,
                            "type": note_type,
                        }
                except Exception as e:
                    print(f"[rednote][api] parse error: {e}")

            page.on("response", on_response)

            print(f"[rednote] Loading favorites: {index_url}")
            try:
                await page.goto(index_url, wait_until="domcontentloaded", timeout=60000)
            except Exception as e:
                print(f"[rednote] Page load warning: {e}")
            await asyncio.sleep(8)  # Wait for initial API call

            print(f"[rednote] Page title: {await page.title()}")

            # Scroll to trigger pagination
            no_change = 0
            for i in range(self.max_scroll):
                if len(collected) >= max_posts or not has_more:
                    break
                prev = len(collected)
                await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
                await asyncio.sleep(3)
                print(f"[rednote] Scroll {i+1}/{self.max_scroll} — {len(collected)} posts")
                no_change = 0 if len(collected) > prev else no_change + 1
                if no_change >= 3:
                    print("[rednote] No new posts after 3 scrolls, stopping")
                    break

            await ctx.close()
            await browser.close()

        return list(collected.values())[:max_posts]

    # ------------------------------------------------------------------
    # Posted / DOM mode: extract links from profile page HTML (SSR)
    # ------------------------------------------------------------------
    async def _collect_dom_async(self, index_url: str, max_posts: int) -> list[dict]:
        """
        Scroll listing page, extract note links with xsec_token from DOM.
        Works for posted mode and target_url mode.
        """
        from playwright.async_api import async_playwright

        collected: dict[str, dict] = {}

        async with async_playwright() as p:
            browser = await p.chromium.launch(
                executable_path=_CHROME_BIN,
                headless=True,
                args=["--no-sandbox", "--disable-dev-shm-usage"],
            )
            ctx = await browser.new_context(
                viewport={"width": 1280, "height": 900},
                service_workers="block",
            )
            page = await ctx.new_page()
            await ctx.add_cookies(self._cookies)

            print(f"[rednote] Loading: {index_url}")
            try:
                await page.goto(index_url, wait_until="domcontentloaded", timeout=60000)
            except Exception as e:
                print(f"[rednote] Page load warning: {e}")
            await asyncio.sleep(5)
            print(f"[rednote] Page title: {await page.title()}")

            no_change = 0
            for i in range(self.max_scroll):
                if len(collected) >= max_posts:
                    break
                prev = len(collected)

                # Profile page: cover anchors have xsec_token in href
                dom_links = await page.evaluate("""
                    () => Array.from(document.querySelectorAll('a[href*="xsec_token"]'))
                        .map(a => a.href)
                        .filter(h => h.includes('/explore/') || h.includes('/profile/'))
                """)
                if i == 0:
                    print(f"[rednote] Sample DOM links: {dom_links[:2]}")
                for link in dom_links:
                    m = re.search(r"(?:explore/|profile/[^/]+/)([0-9a-f]{24})", link)
                    if not m:
                        continue
                    note_id = m.group(1)
                    if note_id in collected:
                        continue
                    m_tok = re.search(r"xsec_token=([^&]+)", link)
                    xsec = m_tok.group(1) if m_tok else ""
                    xhs_url = (
                        f"{_XHS_BASE}/explore/{note_id}"
                        + (f"?xsec_token={xsec}" if xsec else "")
                    )
                    collected[note_id] = {
                        "id": note_id, "title": note_id,
                        "url": xhs_url, "type": "unknown",
                    }

                print(f"[rednote] Scroll {i+1}/{self.max_scroll} — {len(collected)} posts")
                no_change = 0 if len(collected) > prev else no_change + 1
                if no_change >= 3:
                    print("[rednote] No new posts after 3 rounds, stopping")
                    break
                await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
                await asyncio.sleep(3)

            await ctx.close()
            await browser.close()

        return list(collected.values())[:max_posts]

    # ------------------------------------------------------------------
    # Public interface
    # ------------------------------------------------------------------
    async def _collect_async(self, index_url: str, max_posts: int) -> list[dict]:
        if self.mode == "favorites":
            return await self._collect_favorites_async(index_url, max_posts)
        return await self._collect_dom_async(index_url, max_posts)

    def get_posts(self, max_posts: int) -> Iterator[Post]:
        index_url = self._build_index_url()
        try:
            loop = asyncio.get_event_loop()
            if loop.is_running():
                import concurrent.futures
                with concurrent.futures.ThreadPoolExecutor(1) as pool:
                    post_infos = pool.submit(
                        asyncio.run, self._collect_async(index_url, max_posts)
                    ).result()
            else:
                post_infos = loop.run_until_complete(
                    self._collect_async(index_url, max_posts)
                )
        except Exception as exc:
            print(f"[rednote] Collection failed: {exc}")
            return

        print(f"[rednote] Collected {len(post_infos)} post links")
        yielded = 0
        for info in post_infos:
            if yielded >= max_posts:
                break
            if self.video_only and info["type"] not in ("video", "unknown"):
                continue
            yield Post(
                id=info["id"],
                title=info["title"],
                url=info["url"],
                text="",
                source="rednote",
                image_urls=[],
                video_urls=[],
                extra={
                    "use_xhs_downloader": True,
                    "chrome_profile": self.user_data_dir,
                    "note_type": info["type"],
                },
            )
            yielded += 1
            time.sleep(self.request_delay)
