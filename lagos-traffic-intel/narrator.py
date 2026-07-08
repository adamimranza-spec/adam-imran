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
# same templates work for both daily runs. These no longer name specific
# roads unconditionally — the level can hit Gridlock purely from time-of-day
# scoring (e.g. Friday evening) even when most named corridors are actually
# reported clear, so claiming "all major corridors are critically affected"
# regardless of real reports would be fabricating specifics. get_fallback_narrative()
# below grounds the "most affected" claim in real corridor_reports when they're
# available, and only falls back to this fully generic, hedged wording (no
# road names) when there's no real corridor data at all for this run.
LEVEL_OPENERS: dict[str, str] = {
    "Light":    "Conditions are light across Lagos this {period}.",
    "Moderate": "Moderate traffic across Lagos this {period}.",
    "Heavy":    "Heavy traffic across Lagos this {period}.",
    "Severe":   "Severe traffic conditions across Lagos this {period}.",
    "Gridlock": "Gridlock-level traffic across Lagos this {period}.",
}

LEVEL_HEDGES: dict[str, str] = {
    "Light":    "Most major routes are typically moving freely at this level. Allow standard travel time.",
    "Moderate": "Expect standard rush hour density on the busiest corridors. Allow extra time.",
    "Heavy":    "Expect significant delays on the busiest corridors. Leave earlier than usual.",
    "Severe":   "Expect heavy congestion on the busiest corridors. Plan for long delays on the Island routes.",
    "Gridlock": "Expect critical congestion on the busiest corridors. Delay travel until conditions ease if possible.",
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
    "plain hyphens instead. Write like a Lagosian giving a friend the heads up before they "
    "leave the house, not a corporate report or a weather bulletin: warm, brief, a little "
    "personality and humor is welcome, especially on a calm day. Tone is flexible, the "
    "grounding rules above are not."
)


def _sanitize(text: str) -> str:
    """Strip em/en dashes regardless of what the model or a template produced."""
    text = text.replace("—", "-").replace("–", "-")
    return re.sub(r" {2,}", " ", text)


def _join_areas(areas: list[str]) -> str:
    if len(areas) == 1:
        return areas[0]
    return ", ".join(areas[:-1]) + f" and {areas[-1]}"


def get_fallback_narrative(level: str, period: str = "morning",
                           corridor_reports: list[dict] | None = None) -> str:
    """
    Deterministic narrative used when the Claude call fails or the API key is
    missing. Applies the same grounding rule as the live narrator: never name
    a specific road as affected unless a real corridor report says so this
    run, and never imply the whole city is blocked when some corridors are
    reported clear. Falls back to fully generic hedged language (no road
    names) only when there's no real corridor data at all to ground on.
    """
    opener = LEVEL_OPENERS.get(level, LEVEL_OPENERS["Moderate"]).format(period=period)
    corridor_reports = corridor_reports or []

    incidents = [r["area"] for r in corridor_reports if r.get("status") == "incident"]
    busy      = [r["area"] for r in corridor_reports if r.get("status") == "busy"]
    clear     = [r["area"] for r in corridor_reports if r.get("status") == "clear"]

    affected = list(dict.fromkeys(incidents + busy))[:3]  # incidents first, de-duped
    if affected:
        text = f"{opener} The heaviest disruption is on {_join_areas(affected)}."
        clear_areas = list(dict.fromkeys(clear))[:2]
        if clear_areas:
            verb = "is" if len(clear_areas) == 1 else "are"
            text += (
                f" {_join_areas(clear_areas)} {verb} reported moving normally, "
                "so it isn't blocked everywhere."
            )
        return _sanitize(text)

    hedge = LEVEL_HEDGES.get(level, LEVEL_HEDGES["Moderate"])
    return _sanitize(
        f"{opener} No specific corridor reports came in this run, so treat this as a "
        f"general expectation, not a confirmed report. {hedge}"
    )


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
1. Open with one short, conversational sentence stating the level. Only name a driver
   (weather, rush hour, a confirmed incident) if it's the actual real reason, and say it the
   way a person would, not by reciting the scoring inputs above back in prose.
2. Every named road/area you mention must be grounded in the corridor reports or signals
   above. If a corridor report says an area is CLEAR, do not describe it as congested; if
   it says BUSY or INCIDENT, you can lead with that. Do not name a specific road as blocked,
   congested, or flooded unless something above actually says so. Even at Heavy, Severe, or
   Gridlock level, do not imply the whole city is affected: name the specific areas that are
   actually busy or have an incident, not "Lagos" or "the city" as a whole.
3. If corridor reports are available, base the 2-3 bullet points on the INCIDENT and BUSY
   ones first (real, specific, useful) and name the specific areas most affected. If any of
   the corridor reports in the same batch say CLEAR, you must add one line naming at least
   one of them and stating it's moving normally, so the advisory doesn't read as if the
   entire city is blocked when only specific corridors are. Only skip this line if every
   corridor report you have is BUSY or INCIDENT with no CLEAR ones at all.
4. If no corridor reports or signals are available at all (check below), this is a quiet
   run: say plainly, in one short sentence, that nothing specific came in this run, then at
   most one more sentence of general hedged expectation for this day and time ("typically",
   "usually"). Do not pad a quiet run out with multiple paragraphs of caveats just because
   there's nothing to report, it should read as quiet, not exhaustive.
5. If flood zones are listed above, mention them in one short sentence and always name at
   least one specific real place from the list, e.g. "Oshodi-Apapa and Mile 2 could get
   soggy given today's rain." Never write a vague placeholder line like "near the water" or
   "low-lying areas" with no actual place attached, Lagos is a lagoon city, that tells a
   reader nothing. Name at most two zones, don't enumerate the full list. Match your
   confidence wording to the data: HIGH confidence zones can be stated more directly ("expect
   pooling on..."), MEDIUM/LOW should read as a chance, not a certainty ("there's a chance
   of...") but still name the actual place. On a quiet run (rule 4), only add this if it's
   genuinely worth knowing, one hedge paragraph total is enough.
6. 150 words maximum, and far shorter when there's nothing concrete to report: a quiet run
   with no corridor reports or signals should be 2-4 sentences total, not a full-length
   advisory padded with hedges.
7. Second person: "Expect...", "Avoid...", "Plan for..."
8. This is the {period} update - write for a commuter checking this in the {period}, not always "this morning".
9. Do not mention the score number. Do not say "based on the data" or "according to signals."
10. Do not use em dashes (—) or en dashes (–) anywhere, including as a separator. Use commas,
    periods, or plain hyphens (-) only.
11. Begin directly - no preamble, no "Here is the advisory:" opener.
12. Never close with a vague, content-free line like "good luck" or "stay safe out there" in
    place of real information. If you have nothing more specific to add, end on the last
    concrete, grounded sentence instead.
13. Sound like a person, not software. A little wit or warmth is good, especially when
    conditions are calm and there's nothing dramatic to relay, but never let tone push you
    into inventing a specific claim that isn't grounded in the data above.

Corridor reports available this run: {"yes" if has_corridor_reports else "no"}.
"""


async def generate_narrative(ctx: dict) -> tuple[str, bool]:
    """
    Returns (narrative_text, used_fallback).
    ctx keys: day_of_week, date_label, level, score, base_score, weather_modifier,
              signal_modifier, signal_count, precip_sum_mm, precip_hours,
              condition_label, season_label, flood_zones, signal_lines, period,
              run_time_label, corridor_reports
    """
    level  = ctx.get("level", "Moderate")
    period = ctx.get("period", "morning")
    corridor_reports = ctx.get("corridor_reports", [])

    if not ANTHROPIC_API_KEY:
        return get_fallback_narrative(level, period, corridor_reports), True

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
        return get_fallback_narrative(level, period, corridor_reports), True
