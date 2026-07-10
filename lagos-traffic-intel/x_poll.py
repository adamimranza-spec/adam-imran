"""
Direct poll of @lagostraffic961 and @followlastma via TwitterAPI.io, bypassing
Trigify entirely for these two accounts.

Added 2026-07-10: Trigify's saved searches are capped at DAILY re-crawl
frequency (see trigify.py), so the poll there can be up to ~24h stale. This
endpoint returns tweets within minutes of being posted, at $0.15/1000 tweets —
confirmed live by pulling a real accident report from @followlastma (Marine
Bridge/Apapa truck breakdown) posted about 4 hours prior, versus Trigify still
showing the same crawl from ~24h earlier for @lagostraffic961.

Endpoint verified directly (docs.twitterapi.io):
  GET https://api.twitterapi.io/twitter/tweet/advanced_search
  Header: X-API-Key: <key>
  Params: query (advanced-search syntax, e.g. "from:user1 OR from:user2"),
          queryType ("Latest" | "Top")
  -> {"tweets": [{"id", "url", "text", "createdAt", "author": {"userName", ...}, ...}],
      "has_next_page": bool, "next_cursor": str}

createdAt is Twitter's classic format ("Fri Jul 10 09:31:18 +0000 2026"), not
ISO 8601 — converted here so it works with signals.py's _is_recent() and the
same post-shape convention trigify.py already produces (text/author/
posted_at/url), so it merges into main.py's pipeline without any changes
downstream.
"""

from datetime import datetime

import httpx

from config import TWITTERAPI_IO_KEY

TWITTERAPI_BASE = "https://api.twitterapi.io/twitter/tweet/advanced_search"
QUERY = "(from:lagostraffic961 OR from:followlastma)"
MAX_POSTS = 60  # combined across both handles, per poll


def _parse_created_at(raw: str) -> str:
    try:
        dt = datetime.strptime(raw, "%a %b %d %H:%M:%S %z %Y")
        return dt.isoformat()
    except (ValueError, TypeError):
        return ""


def _extract_posts(tweets: list[dict]) -> list[dict]:
    posts: list[dict] = []
    for t in tweets[:MAX_POSTS]:
        author = t.get("author") or {}
        posts.append({
            "text":       str(t.get("text", ""))[:600],
            "author":     author.get("userName") or author.get("name") or "",
            "posted_at":  _parse_created_at(t.get("createdAt", "")),
            "url":        t.get("url", ""),
            "raw":        t,
        })
    return posts


async def fetch_x_posts() -> list[dict]:
    """
    Fetches latest tweets from @lagostraffic961 and @followlastma directly.
    Any failure returns a single error-flagged post, matching trigify.py's
    convention so main.py's _safe() wrapper and downstream filtering both
    already handle it correctly.
    """
    try:
        async with httpx.AsyncClient(timeout=20) as client:
            resp = await client.get(
                TWITTERAPI_BASE,
                params={"query": QUERY, "queryType": "Latest"},
                headers={"X-API-Key": TWITTERAPI_IO_KEY},
            )
            resp.raise_for_status()
            data = resp.json()
            return _extract_posts(data.get("tweets", []))
    except Exception as e:
        return [{
            "text": f"[twitterapi.io fetch failed: {e}]",
            "author": "", "posted_at": "", "url": "",
            "error": True,
        }]
