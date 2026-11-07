"""Read-only public Bilibili video discovery adapter.

Searches public video pages and fetches their page text through the host's
SSRF-guarded web.search/web.fetch tools. It does not log in, use cookies,
download/play video streams, post comments, or claim a video was watched.
"inspected" means the public page/description was actually fetched.
"""
from __future__ import annotations

import re
from urllib.parse import urlsplit

from whale_core.life.discovery import DiscoveredContent

_BVID = re.compile(r"^/video/(BV[0-9A-Za-z]+)(?:/|$)")
_VIDEO_URL = re.compile(
    r"https?://(?:www\.)?bilibili\.com/video/BV[0-9A-Za-z]+/?(?:\?[^\s)）]*)?",
    re.IGNORECASE,
)
_ALLOWED_HOSTS = frozenset({"www.bilibili.com", "bilibili.com"})


class BilibiliContentAdapter:
    """Adapt public Bilibili search results into evidence-backed discoveries."""

    def __init__(self, search, fetch, *, max_per_topic: int = 2):
        if not callable(search) or not callable(fetch):
            raise ValueError("search and fetch callbacks are required")
        self.search = search
        self.fetch = fetch
        self.max_per_topic = max(0, min(int(max_per_topic), 5))

    @staticmethod
    def _parse_line(line: str):
        match = _VIDEO_URL.search(line or "")
        if not match:
            return None
        raw_url = match.group(0).rstrip(".,;:!?。，；：！？")
        parsed = urlsplit(raw_url)
        host = (parsed.hostname or "").lower()
        path_match = _BVID.match(parsed.path or "")
        if parsed.scheme != "https" or host not in _ALLOWED_HOSTS or not path_match:
            return None
        bvid = path_match.group(1)
        before = (line or "")[:match.start()].strip(" \t-*•")
        title = before.split(" — ", 1)[0].split(" – ", 1)[0].strip()
        title = re.sub(r"^\s*\d+[.)]\s*", "", title).strip(" []()（）")
        snippet = ""
        if " — " in before:
            snippet = before.split(" — ", 1)[1].strip()
        elif " – " in before:
            snippet = before.split(" – ", 1)[1].strip()
        return bvid, f"https://www.bilibili.com/video/{bvid}/", title[:200], snippet[:500]

    def discover(self, topic: str, *, should_stop=None) -> list[DiscoveredContent]:
        query = f"site:bilibili.com/video {str(topic or '').strip()}"[:400]
        if not query.strip() or self.max_per_topic <= 0:
            return []
        raw = self.search(query, limit=max(3, self.max_per_topic + 1))
        lines = raw.splitlines() if isinstance(raw, str) else []
        seen: set[str] = set()
        out: list[DiscoveredContent] = []
        for line in lines:
            if should_stop is not None and should_stop():
                break
            parsed = self._parse_line(line)
            if parsed is None:
                continue
            bvid, url, title, snippet = parsed
            if bvid in seen:
                continue
            seen.add(bvid)
            page = self.fetch(url)
            page_text = str(page or "").strip()
            if len(page_text) < 24:
                continue
            summary = "\n".join(part for part in (snippet, page_text[:700]) if part)
            out.append(DiscoveredContent(
                item_id=bvid,
                title=title or f"B站视频 {bvid}",
                url=url,
                source_name="哔哩哔哩",
                summary=summary[:1000],
                inspected=True,
                content_type="video",
                inspection_method="public_page_description",
            ))
            if len(out) >= self.max_per_topic:
                break
        return out


__all__ = ["BilibiliContentAdapter"]
