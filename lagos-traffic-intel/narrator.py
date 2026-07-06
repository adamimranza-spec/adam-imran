"""
Generates the traffic narrative via Claude API.
Claude receives the deterministic score as an input — it only writes, never decides.
Falls back to level-based templates if the API call fails.
"""

import re

import anthropic
from config import ANTHROPIC_API_KEY, DAY_CONTEXT
from signals import signals_to_text

# {period} is filled in with "morning" or "evening" at render time, so the
# same templates work for both daily runs.
FALLBACK_NARRATIVES: dict[str, str] = {
    "Light":
        "Conditions are light across Lagos this {period}. Most major routes are moving "
        "freely. Allow standard travel time.",
    "Moderate":
        "Moderate traffic on Lagos roads this {period}. Allow extra time on Third Mainland "
        "Bridge, Lekki-Epe Expressway, and Oshodi-Apapa. Standard rush hour density.",
    "Heavy":
        "Heavy traffic across Lagos this {period}. Expect significant delays on Third "
        "Mainland Bridge, Lekki-Epe Expressway, and Oshodi-Apapa Expressway. Leave "
        "earlier than usual.",
    "Severe":
        "Severe conditions across Lagos this {period}. All major corridors (Third Mainland "
        "Bridge, Lekki-Epe, Ikorodu Road, Oshodi-Apapa) are heavily congested. Plan for "
        "45-90 minute delays on the Island routes.",
    "Gridlock":
        "Gridlock across Lagos this {period}. Third Mainland Bridge, Lekki-Epe Expressway, "
        "Oshodi-Apapa, and Ikorodu Road are all critically affected. Delay travel until "
        "conditions ease if possible.",
}

SYSTEM_PROMPT = (
    "You are the Lagos Traffic Intel narrator. The traffic score and level have been "
    "determined by a deterministic algorithm; you do not re-analyze or second-guess them. "
    "But every specific, named claim you make about a road, bridge, or area (congested, "
    "blocked, clear, flooded) must come from the real-time corridor reports or signals "
    "given to you. You have no other source of truth about current conditions and no "
    "knowledge of today's actual traffic beyond what is in this prompt. When no real report "
    "covers a given road, do not invent one; either omit it or speak only in terms of what "
    "is typical for this day and time, clearly hedged as such ('this is usually a heavy "
    "period for...', not 'X is congested'). Fabricating a specific, confident-sounding claim "
    "that isn't grounded in the data you were given is the single worst failure mode for "
    "this job: commuters make real decisions based on what you write. Never use em dashes or "
    "en dashes anywhere in your writing, as a separator or otherwise; use commas, periods, or "
    "plain hyphens instead."
)


def _sanitize(text: str) -> str:
    """Strip em/en dashes regardless of what the model or a template produced."""
    text = text.replace("—", "-").replace("–", "-")
    return re.sub(r" {2,}", " ", text)


def get_fallback_narrative(level: str, period: str = "morning") -> str:
    template = FALLBACK_NARRATIVES.get(level, FALLBACK_NARRATIVES["Moderate"])
    return _sanitize(template.format(period=period))


def _build_user_prompt(ctx: dict) -> str:
    period = ctx.get("period", "morning")
    flood_lines = "\n".join(
        f"  - {z['name']} ({z['confidence']} confidence)"
        for z in ctx.get("flood_zones", [])
    ) or "  None - rainfall below flood thresholds"
    corridor_text = ctx.get("corridor_reports_text", "No real-time corridor reports available this run.")
    has_corridor_reports = "No real-time corridor reports" not in corridor_text

    return f"""=== TODAY'S TRAFFIC BRIEF ===
Date: {ctx['day_of_week'].title()}, {ctx['date_label']} - {ctx.get('run_time_label', '')} ({period} update)
Traffic Level: {ctx['level']} ({ctx['score']}/10)

Scoring inputs (these set the level, they are not things to quote directly):
- Base score ({ctx['day_of_week']} {period} rush): {ctx['base_score']}/10
  ({DAY_CONTEXT.get(ctx['day_of_week'], '')})
- Weather modifier: +{ctx['weather_modifier']} ({ctx['precip_sum_mm']}mm rainfall over {ctx['precip_hours']} hours since midnight)
- Signal modifier: +{ctx['signal_modifier']} ({ctx['signal_count']} event(s) found in news/radio)
- Corridor modifier: {ctx.get('corridor_modifier', 0):+d} (from real corridor report sentiment below; positive
  means several confirmed incidents, negative means most named corridors are reported clear)

Rainfall context: {ctx['condition_label']}, {ctx['season_label']}

Flood zones at risk (from rainfall model, not a live report):
{flood_lines}

REAL-TIME CORRIDOR REPORTS (ground truth for named roads/areas this run, from
Lagos Traffic Radio 96.1FM and keyword monitoring; CLEAR means reported
free-flowing right now, BUSY means reported slow/building, INCIDENT means a
specific confirmed problem):
{corridor_text}

Other events from news/radio pattern matching:
{ctx['signal_lines']}

=== WRITE THE ADVISORY ===

Rules you must follow:
1. Open with one sentence stating the level and its main driver (weather, rush-hour timing,
   or a specific confirmed incident above; pick whichever is actually true).
2. Every named road/area you mention must be grounded in the corridor reports or signals
   above. If a corridor report says an area is CLEAR, do not describe it as congested; if
   it says BUSY or INCIDENT, you can lead with that. Do not name a specific road as blocked,
   congested, or flooded unless something above actually says so.
3. If corridor reports are available, base the 2-3 bullet points on the INCIDENT and BUSY
   ones first (real, specific, useful). You may add one line noting which corridors are
   reported clear, if that's useful context.
4. If no corridor reports or signals are available at all (check below), do not invent
   specific incidents or specific road conditions. Instead, describe the general expected
   conditions for this day and time using hedged language ("typically", "expect", "this is
   usually") based only on the score and weather, and say plainly that this is a general
   expectation, not a confirmed report.
5. If flood zones are listed above, add one flood warning sentence at the end, framed as
   modeled risk, not a confirmed flood, unless a corridor report confirms actual flooding.
6. 150 words maximum. Plain text only - no markdown, no asterisks, no emojis, no hashtags.
7. Second person: "Expect...", "Avoid...", "Plan for..."
8. This is the {period} update - write for a commuter checking this in the {period}, not always "this morning".
9. Do not mention the score number. Do not say "based on the data" or "according to signals."
10. Do not use em dashes (—) or en dashes (–) anywhere, including as a separator. Use commas,
    periods, or plain hyphens (-) only.
11. Begin directly - no preamble, no "Here is the advisory:" opener.

Corridor reports available this run: {"yes" if has_corridor_reports else "no"}.
"""


async def generate_narrative(ctx: dict) -> tuple[str, bool]:
    """
    Returns (narrative_text, used_fallback).
    ctx keys: day_of_week, date_label, level, score, base_score, weather_modifier,
              signal_modifier, signal_count, precip_sum_mm, precip_hours,
              condition_label, season_label, flood_zones, signal_lines, period,
              run_time_label
    """
    level  = ctx.get("level", "Moderate")
    period = ctx.get("period", "morning")

    if not ANTHROPIC_API_KEY:
        return get_fallback_narrative(level, period), True

    try:
        client = anthropic.AsyncAnthropic(api_key=ANTHROPIC_API_KEY)
        msg = await client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=400,
            system=SYSTEM_PROMPT,
            messages=[{"role": "user", "content": _build_user_prompt(ctx)}],
        )
        text = _sanitize(msg.content[0].text.strip())
        return text, False
    except Exception:
        return get_fallback_narrative(level, period), True
