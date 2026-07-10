"""
Extracts structured traffic events from raw article/post text.
Pattern-matched, no AI call — this keeps signal detection cheap and fast.
"""

import html
import re
from datetime import datetime, timezone

from config import MAX_POST_AGE_HOURS


def _is_recent(posted_at: str, max_age_hours: float = MAX_POST_AGE_HOURS) -> bool:
    """
    True if posted_at is within max_age_hours of now. Missing/unparseable
    timestamps are treated as recent (better to keep a post than silently
    drop real information over an API quirk) — this is only meant to catch
    posts we can positively confirm are stale, like a ~20h old accident
    report still being described as a current condition the next morning.
    """
    if not posted_at:
        return True
    try:
        ts = datetime.fromisoformat(posted_at.replace("Z", "+00:00"))
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        age_hours = (datetime.now(timezone.utc) - ts).total_seconds() / 3600
        return age_hours <= max_age_hours
    except (ValueError, TypeError):
        return True


def _is_reply(text: str) -> bool:
    """
    True if a post is a reply (starts with an @mention), which on X/Twitter
    is almost always conversational/reactive rather than a primary report.
    Found live 2026-07-09: a sarcastic reply ("@bigbank0125 @DOlusegun This
    is such a stupid comment. People had accident on third mainland bridge
    too, so does that mean the bridge shouldn't exist?") matched the accident
    pattern below purely on keyword proximity and got treated as a real
    MEDIUM-severity traffic event. Replies are cheap and reliable to filter
    out before pattern matching even runs.
    """
    return text.strip().startswith("@")


# (pattern, event_type, severity, affected_route_hint)
SIGNAL_PATTERNS: list[tuple[re.Pattern, str, str, str]] = [
    (re.compile(r"convoy|presidential convoy|vip movement|presidential motorcade", re.I),
     "convoy", "HIGH", "multiple routes"),
    (re.compile(r"third mainland bridge.{0,30}(clos|shut|block|damag|crack|repai)", re.I),
     "bridge_closure", "HIGH", "Third Mainland Bridge"),
    (re.compile(r"eko bridge.{0,30}(clos|shut|block|damag|repai)", re.I),
     "bridge_closure", "HIGH", "Eko Bridge"),
    (re.compile(r"(carter|falomo|lekki ikoyi).{0,30}(clos|shut|block)", re.I),
     "bridge_closure", "HIGH", "Lekki-Ikoyi / Carter Bridge"),
    (re.compile(r"(road|highway|expressway).{0,30}(flood|submerge|underwater|inundat)", re.I),
     "flooding_confirmed", "HIGH", "flooded road"),
    # Bare "marathon" collides with common Pidgin/slang idiom ("running a marathon"
    # meaning broke/exhausted, nothing to do with roads) — require actual
    # event-logistics language alongside it, not just the word.
    (re.compile(r"(marathon|road race).{0,40}(route|road clos|diver|shut down)", re.I),
     "major_event", "HIGH", "multiple routes"),
    (re.compile(r"(governor.{0,15}procession|independence day parade)", re.I),
     "major_event", "HIGH", "multiple routes"),
    (re.compile(r"(fatal|multiple|serious).{0,20}accident.{0,30}(expressway|bridge|highway)", re.I),
     "accident", "HIGH", "expressway/bridge"),
    (re.compile(r"accident.{0,40}(third mainland|lekki|oshodi|ikorodu|apapa|vi )", re.I),
     "accident", "MEDIUM", "major corridor"),
    (re.compile(r"(road.{0,10}(repair|maintenance|construction|work).{0,30}(divert|lane))", re.I),
     "maintenance", "MEDIUM", "diverted route"),
    (re.compile(r"(fuel|petrol).{0,20}(scarcity|shortage).{0,30}(queue|traffic|gridlock)", re.I),
     "fuel_scarcity", "MEDIUM", "filling station corridors"),
]


def extract_signals(articles: list[dict], posts: list[dict]) -> list[dict]:
    """
    Combines articles (always empty now — news scraping was dropped 2026-07-06,
    see main.py) and posts (from x_poll.py's TwitterAPI.io poll) into one text
    corpus, runs pattern matching, deduplicates by event type, returns signal
    list. Kept accepting `articles` so the signature doesn't need to change if
    a news source is ever added back.
    """
    signals: list[dict] = []
    seen_types: set[str] = set()

    all_texts: list[tuple[str, str, str]] = []
    for a in articles:
        if not a.get("error"):
            text = f"{a.get('title', '')} {a.get('snippet', '')}"
            all_texts.append((text, a.get("source", "news"), a.get("url", "")))
    for p in posts:
        if p.get("error"):
            continue
        text = p.get("text", "") or p.get("content", "") or str(p)
        if _is_reply(text) or not _is_recent(p.get("posted_at", "")):
            continue
        all_texts.append((text, "twitter", p.get("url", "")))

    for text, source, url in all_texts:
        for pattern, event_type, severity, route_hint in SIGNAL_PATTERNS:
            if pattern.search(text):
                key = f"{event_type}:{route_hint}"
                if key not in seen_types:
                    seen_types.add(key)
                    signals.append({
                        "type":              event_type,
                        "severity":          severity,
                        "description":       _clean_description(text[:200]),
                        "affected_route":    route_hint,
                        "source":            source,
                        "url":               url,
                    })

    # HIGH signals first
    signals.sort(key=lambda s: (0 if s["severity"] == "HIGH" else 1))
    return signals


def _clean_description(text: str) -> str:
    text = html.unescape(text)
    text = re.sub(r"\s+", " ", text).strip()
    return text[:200]


def signals_to_text(signals: list[dict]) -> str:
    """Formats signal list for injection into narrator prompt."""
    if not signals:
        return "No specific events found in today's news or radio monitoring."
    lines = []
    for s in signals[:5]:  # cap at 5 for prompt length
        lines.append(f"- [{s['severity']}] {s['type'].replace('_', ' ').title()}: {s['description']} (via {s['source']})")
    return "\n".join(lines)


# ── Structured corridor reports (@lagostraffic961) ──────────────────────────
#
# Lagos Traffic Radio 96.1FM posts a consistent, structured format several
# times a day: "TRAFFIC UPDATE FROM {AREA}" / "TRAFFIC REPORT FROM {AREA}" /
# "INCIDENT REPORT FROM {AREA}" followed by a plain-English body. This is
# actual ground truth for named corridors, as opposed to SIGNAL_PATTERNS
# above (which only catches discrete named events like bridge closures).
# Parsing this directly is what lets the narrator say what's *actually*
# being reported this run instead of falling back to generic day/hour
# expectations.
_CORRIDOR_HEADER_RE = re.compile(
    r"^(TRAFFIC UPDATE|TRAFFIC REPORT|INCIDENT REPORT)\s*(?:FROM\s+([A-Z0-9,&/\s-]+?))?\s*\n+(.*)",
    re.S,
)
_CLEAR_RE    = re.compile(r"\b(smooth|free[- ]flowing|calm|clear|encouraging|steady|orderly|seamless)\b", re.I)
_BUSY_RE     = re.compile(r"\b(busy|slow|building|congest\w*|heavy|delay|gridlock|bumper)\b", re.I)
_INCIDENT_RE = re.compile(r"\b(accident|broke?n? ?down|breakdown|trapped|blocked|collision|fault|fire|flood\w*)\b", re.I)

# @followlastma (LASTMA's own account, added 2026-07-10 via TwitterAPI.io —
# see x_poll.py) posts a different but equally consistent structure: an
# optional "[HH:MMam/pm]" time prefix, then one or more area hashtags, then a
# hashtag containing "Report" or "Management" (typos observed live, e.g.
# "#IncientReport" — matched on the substring so spelling doesn't matter),
# a blank line, then the plain-English body. Every real example seen is a
# LASTMA incident/breakdown response, so kind/status are always "incident",
# mirroring how the "INCIDENT REPORT" header above is treated regardless of
# body wording.
_FOLLOWLASTMA_TYPE_TAG_RE = re.compile(r"report|management", re.I)


def _camel_split(tag: str) -> str:
    """"IkoroduRd" -> "Ikorodu Rd" so hashtag-derived area names read naturally."""
    return re.sub(r"(?<!^)(?=[A-Z])", " ", tag)


def _parse_followlastma(text: str) -> dict | None:
    parts = re.split(r"\n\s*\n", text, maxsplit=1)
    if len(parts) != 2:
        return None
    header, body = parts
    header = re.sub(r"^\[[^\]\n]{3,12}\]\s*", "", header.strip())
    tags = re.findall(r"#(\w+)", header)
    if not tags or not any(_FOLLOWLASTMA_TYPE_TAG_RE.search(t) for t in tags):
        return None
    # Header must be nothing but hashtags (plus the bracket time already
    # stripped above) — guards against misfiring on an unrelated tweet that
    # merely starts with a hashtag.
    if re.sub(r"#\w+", "", header).strip():
        return None
    area_tags = [t for t in tags if not _FOLLOWLASTMA_TYPE_TAG_RE.search(t)]
    body = html.unescape(body)
    body = re.sub(r"\s+", " ", body).strip()
    if not body:
        return None
    return {
        "area":   " / ".join(_camel_split(t) for t in area_tags) if area_tags else "Lagos",
        "kind":   "incident",
        "status": "incident",
        "text":   body[:280],
    }


def parse_corridor_reports(posts: list[dict]) -> list[dict]:
    """
    Parses @lagostraffic961- and @followlastma-style structured posts into
    per-corridor status. Returns one entry per real, dated report:
    {area, kind, status, text, posted_at}.
    """
    reports: list[dict] = []
    for p in posts:
        if p.get("error"):
            continue
        if not _is_recent(p.get("posted_at", "")):
            continue
        text = (p.get("text") or "").strip()

        m = _CORRIDOR_HEADER_RE.match(text)
        if m:
            header, area, body = m.groups()
            body = html.unescape(body)
            body = re.sub(r"\s+", " ", body).strip()
            if not body:
                continue
            kind = "incident" if header == "INCIDENT REPORT" else "update"
            if kind == "incident" or _INCIDENT_RE.search(body):
                status = "incident"
            elif _BUSY_RE.search(body):
                status = "busy"
            elif _CLEAR_RE.search(body):
                status = "clear"
            else:
                status = "unclear"
            reports.append({
                "area":      (area or "Lagos").strip(" -/,"),
                "kind":      kind,
                "status":    status,
                "text":      body[:280],
                "posted_at": p.get("posted_at", ""),
            })
            continue

        fl = _parse_followlastma(text)
        if fl:
            fl["posted_at"] = p.get("posted_at", "")
            reports.append(fl)

    return reports


def corridor_reports_to_text(reports: list[dict]) -> str:
    """Formats corridor reports for injection into the narrator prompt."""
    if not reports:
        return "No real-time corridor reports available this run — none were fetched or none parsed."
    lines = []
    for r in reports[:14]:  # cap for prompt length
        lines.append(f"- [{r['status'].upper()}] {r['area']}: {r['text']}")
    return "\n".join(lines)
