"""
Reddit source — fetches posts (and comments) from a subreddit using the JSON API.

Key behaviours:
- Uses Reddit's public JSON API: https://www.reddit.com/r/{subreddit}/.json
- http2=False (Reddit blocks HTTP/2)
- Pagination via the 'after' token
- Recursively collects comments to find pixeldrain links
- 429 retry with backoff
- Skip already-downloaded posts (caller checks output dir)
"""

import time
from pathlib import Path
from typing import Iterator

import httpx

from .base import Post, PlatformSource

_USER_AGENT = "python:fancam_harvester:v1.0 (by /u/fancam_bot)"


def _collect_comments(children: list) -> list[str]:
    """Recursively collect all comment body strings from a Reddit listing's children."""
    bodies: list[str] = []
    for child in children:
        if not isinstance(child, dict):
            continue
        kind = child.get("kind")
        data = child.get("data", {})
        if kind == "t1":
            body = data.get("body", "").strip()
            if body and body != "[deleted]" and body != "[removed]":
                bodies.append(body)
            # Recurse into replies
            replies = data.get("replies")
            if isinstance(replies, dict):
                reply_children = replies.get("data", {}).get("children", [])
                bodies.extend(_collect_comments(reply_children))
        elif kind == "Listing":
            nested = data.get("children", [])
            bodies.extend(_collect_comments(nested))
    return bodies


class RedditSource(PlatformSource):
    """Fetch posts (and their comments) from a subreddit using the JSON API."""

    def __init__(
        self,
        subreddit: str,
        sort: str = "new",
        request_delay: float = 1.5,
    ) -> None:
        self.subreddit = subreddit
        self.sort = sort
        self.request_delay = request_delay
        self._client = httpx.Client(
            timeout=30,
            follow_redirects=True,
            http2=False,               # Reddit blocks HTTP/2
            headers={"User-Agent": _USER_AGENT},
            cookies={"over18": "1"},   # Required for NSFW subreddits
        )

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _get_json(self, url: str) -> dict | list:
        """GET *url* and return parsed JSON; handles 429 with backoff retry."""
        while True:
            resp = self._client.get(url)
            if resp.status_code == 429:
                print(f"[reddit] 429 rate-limited on {url} — sleeping 10s")
                time.sleep(10)
                continue
            resp.raise_for_status()
            return resp.json()

    def _fetch_comments(self, subreddit: str, post_id: str) -> list[str]:
        """Return a flat list of comment body strings for *post_id*."""
        url = (
            f"https://www.reddit.com/r/{subreddit}/comments/{post_id}.json?limit=500"
        )
        data = self._get_json(url)
        # Reddit returns [post_listing, comment_listing]
        if not isinstance(data, list) or len(data) < 2:
            return []
        comment_listing = data[1]
        children = comment_listing.get("data", {}).get("children", [])
        return _collect_comments(children)

    def _fetch_page(self, after: str | None) -> tuple[list[dict], str | None]:
        """Fetch one page of posts. Returns (post_data_list, next_after_token)."""
        url = (
            f"https://www.reddit.com/r/{self.subreddit}/{self.sort}.json?limit=25"
        )
        if after:
            url += f"&after={after}"
        data = self._get_json(url)
        listing_data = data.get("data", {})
        children = listing_data.get("children", [])
        posts = [c["data"] for c in children if c.get("kind") == "t3"]
        next_after: str | None = listing_data.get("after")
        return posts, next_after

    # ------------------------------------------------------------------
    # Public interface
    # ------------------------------------------------------------------

    def get_posts(self, max_posts: int) -> Iterator[Post]:
        collected = 0
        after: str | None = None

        while collected < max_posts:
            raw_posts, after = self._fetch_page(after)
            if not raw_posts:
                break

            for d in raw_posts:
                if collected >= max_posts:
                    return

                post_id: str = d["id"]
                title: str = d.get("title", "")
                permalink: str = d.get("permalink", "")
                url = f"https://www.reddit.com{permalink}"
                selftext: str = d.get("selftext", "") or ""

                # Fetch comments for pixeldrain link discovery
                try:
                    comment_bodies = self._fetch_comments(self.subreddit, post_id)
                except Exception as exc:
                    print(f"[reddit] Could not fetch comments for {post_id}: {exc}")
                    comment_bodies = []

                if comment_bodies:
                    full_text = selftext + "\n---\n" + "\n---\n".join(comment_bodies)
                else:
                    full_text = selftext

                yield Post(
                    id=post_id,
                    title=title,
                    url=url,
                    text=full_text,
                    source="reddit",
                    extra={
                        "comment_texts": comment_bodies,
                        "score": d.get("score"),
                        "num_comments": d.get("num_comments"),
                        "author": d.get("author"),
                        "created_utc": d.get("created_utc"),
                        "link_flair_text": d.get("link_flair_text"),
                        "subreddit": d.get("subreddit"),
                        "is_self": d.get("is_self"),
                        "url_original": d.get("url"),
                    },
                )

                collected += 1
                time.sleep(self.request_delay)

            if not after:
                break
