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

import asyncio
from datetime import datetime

import httpx

from config import TWITTERAPI_IO_KEY

TWITTERAPI_BASE = "https://api.twitterapi.io/twitter/tweet/advanced_search"
# One query per handle, not a combined "from:a OR from:b" — found live
# 2026-07-10: a single combined query only returns one page (~20 tweets
# total across BOTH accounts), and @lagostraffic961 posts far more often
# than @followlastma (a burst of 19 tweets in 5 minutes, apparently a
# livestream artifact, was observed). That burst alone filled the entire
# page and pushed a real @followlastma incident report (Badagry Expressway
# bus breakdown, ~2h15m old at the time) off the result set entirely before
# the pipeline ever saw it. Querying each handle separately guarantees one
# account's volume can never crowd the other out.
QUERIES = ["from:lagostraffic961", "from:followlastma"]
MAX_POSTS = 30  # per handle, per poll

# TwitterAPI.io's free tier enforces 1 request / 5s per key (confirmed live
# 2026-07-10: two back-to-back requests in the same fetch got a 429 with
# "QPS limit is one request every 5 seconds" — no documented QPS figure
# anywhere, this is a from-error-message fact, not a guess). 5.5s for margin.
REQUEST_GAP_SECONDS = 5.5


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
    Fetches latest tweets from @lagostraffic961 and @followlastma, one query
    per handle so a burst of posts from one account can't push the other's
    posts out of the result set. Any individual query failure returns an
    error-flagged post for just that handle rather than failing the whole
    poll, matching trigify.py's old per-search convention.
    """
    all_posts: list[dict] = []
    async with httpx.AsyncClient(timeout=20) as client:
        for i, query in enumerate(QUERIES):
            if i > 0:
                await asyncio.sleep(REQUEST_GAP_SECONDS)
            try:
                resp = await client.get(
                    TWITTERAPI_BASE,
                    params={"query": query, "queryType": "Latest"},
                    headers={"X-API-Key": TWITTERAPI_IO_KEY},
                )
                resp.raise_for_status()
                data = resp.json()
                all_posts.extend(_extract_posts(data.get("tweets", [])))
            except Exception as e:
                all_posts.append({
                    "text": f"[twitterapi.io fetch failed for {query}: {e}]",
                    "author": "", "posted_at": "", "url": "",
                    "error": True,
                })
    return all_posts
