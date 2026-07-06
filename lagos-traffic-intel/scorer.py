"""
Deterministic scoring engine. No I/O — pure functions only.
The score is computed before any AI call so Claude never decides traffic severity.
"""

from config import BASE_SCORE_MATRIX, DEFAULT_BASE_SCORE, SCORE_LEVELS


def get_base_score(day: str, hour: int = 7) -> int:
    key_day = day.lower()
    for (d, h0, h1), score in BASE_SCORE_MATRIX.items():
        if d == key_day and h0 <= hour < h1:
            return score
    return DEFAULT_BASE_SCORE


def weather_modifier(precip_hours: int, precip_sum_mm: float, month: int) -> int:
    """
    Primary signal: precip_hours (drain saturation).
    Secondary: precip_sum_mm (raw intensity).
    Peak season (Jul–Sep): infrastructure already stressed, lower effective threshold.
    """
    mod = 0
    if precip_hours >= 6:
        mod += 3
    elif precip_hours >= 4:
        mod += 2
    elif precip_hours >= 2:
        mod += 1

    if precip_sum_mm >= 25:
        mod += 2
    elif precip_sum_mm >= 10:
        mod += 1

    # Peak flood season bonus — drainage already compromised from accumulated rain
    if month in (7, 8, 9) and mod > 0:
        mod += 1

    return min(mod, 4)


def signal_modifier(signals: list[dict]) -> int:
    """
    +2 for any HIGH-severity event (convoy, bridge closure, confirmed flooding).
    +1 for any MEDIUM-severity event (accident, maintenance).
    Capped at 2.
    """
    mod = 0
    for sig in signals:
        sev = sig.get("severity", "LOW")
        if sev == "HIGH":
            mod = max(mod, 2)
        elif sev == "MEDIUM":
            mod = max(mod, 1)
    return mod


def corridor_modifier(corridor_reports: list[dict]) -> int:
    """
    Real corridor reports (from @lagostraffic961 and keyword monitoring) are
    the only ground truth this pipeline has for current conditions. Unlike
    signal_modifier above, this can pull the score DOWN as well as up: the
    base/weather formula alone can put a rainy weekday evening near Gridlock
    even when most named corridors are actually reported clear, and the
    badge should reflect that, not just the time of day.

    Bounded to +2/-2 so it nudges the deterministic base rather than
    replacing it — a handful of confirmed incidents can escalate, a broad,
    uncontradicted set of clear reports can de-escalate, but neither alone
    can swing the score by more than 2.
    """
    if not corridor_reports:
        return 0

    incidents = sum(1 for r in corridor_reports if r.get("status") == "incident")
    busy      = sum(1 for r in corridor_reports if r.get("status") == "busy")
    clear     = sum(1 for r in corridor_reports if r.get("status") == "clear")
    total     = len(corridor_reports)

    if incidents >= 3:
        return 2
    if incidents >= 1 or busy >= 3:
        return 1
    # No incidents and not broadly busy — look for a real "roads are clear" signal.
    if total >= 3 and clear / total >= 0.7:
        return -2
    if total >= 2 and clear / total >= 0.5:
        return -1
    return 0


def compute_score(base: int, w_mod: int, s_mod: int, c_mod: int = 0) -> int:
    return max(1, min(base + w_mod + s_mod + c_mod, 10))


def score_to_level(score: int) -> tuple[str, str]:
    """Returns (level_name, hex_color)."""
    for lo, hi, name, color in SCORE_LEVELS:
        if lo <= score <= hi:
            return name, color
    return "Light", "#16A34A"


def full_score(day: str, hour: int, precip_hours: int, precip_sum_mm: float,
               month: int, signals: list[dict], corridor_reports: list[dict] | None = None) -> dict:
    base   = get_base_score(day, hour)
    w_mod  = weather_modifier(precip_hours, precip_sum_mm, month)
    s_mod  = signal_modifier(signals)
    c_mod  = corridor_modifier(corridor_reports or [])
    score  = compute_score(base, w_mod, s_mod, c_mod)
    level, color = score_to_level(score)
    uncapped = base + w_mod + s_mod + c_mod
    return {
        "base_score":        base,
        "weather_modifier":  w_mod,
        "signal_modifier":   s_mod,
        "corridor_modifier": c_mod,
        "final_score":       score,
        "level":             level,
        "hex_color":         color,
        "capped_at_10":      uncapped > 10,
        "capped_at_1":       uncapped < 1,
    }
