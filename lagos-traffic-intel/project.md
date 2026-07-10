# Lagos Traffic Intel

Live Lagos traffic intelligence, three times daily (6:30 AM, 4:00 PM, 8:00 PM WAT), on a public website and a Telegram channel.

- **Site:** https://lagos-traffic-intel-production.up.railway.app
- **Telegram:** channel ID `-1004333163645` ("friends subscribe here")
- **Repo:** `adamimranza-spec/adam-imran` on GitHub, this project lives in the `lagos-traffic-intel/` subdirectory of that personal monorepo

This file describes the system as it actually runs today. `spec.md` (the original Clay/WhatsApp concept doc from before anything was built) has been removed — everything in it was superseded.

---

## What It Does

Scores Lagos traffic 1–10 for right now, using a deterministic model (day of week, hour, rainfall, real incident reports), then has Claude write a short, grounded, honest advisory around that score — never inventing a specific road claim that isn't backed by real data. Narrows in on named corridors (Third Mainland Bridge, Lekki-Epe, Oshodi-Apapa, etc.) rather than claiming the whole city is affected when it isn't.

Also carries a flood-risk model from rainfall data, and shows subscriber counts / a "share to Telegram" flow on the site.

---

## Architecture

Python, FastAPI, deployed on Railway. No Clay involved in the live system (Clay was the original prototype approach in `project.md`'s first version; abandoned).

| File | Role |
|---|---|
| `server.py` | FastAPI app, entry point. APScheduler runs the pipeline at 6:30/16:00/20:00 WAT. Routes: `/health`, `/run`, `/api/today`, `/api/history`, `/api/subscribers`, `/api/embed`, `/` (serves the frontend). |
| `main.py` | Pipeline orchestrator: fetch → dedupe → score → generate narrative → persist → send to Telegram. Runnable standalone (`python main.py [--dry-run]`). |
| `config.py` | All API keys, the day/hour → base-score matrix, flood zone definitions, file paths (volume-aware), tunable constants. |
| `x_poll.py` | Direct poll of `@lagostraffic961` and `@followlastma` via TwitterAPI.io — see "Data Sources" below. |
| `signals.py` | Turns raw posts into structured signals (regex pattern match) and corridor reports (parses `@lagostraffic961`'s "TRAFFIC UPDATE FROM X" format and `@followlastma`'s hashtag-structured incident format). Filters: drops replies (`@user...`, usually reactive/sarcastic, not reports), drops anything older than `MAX_POST_AGE_HOURS` (20h), decodes HTML entities. |
| `scorer.py` | Pure deterministic scoring: base (day+hour) + weather modifier + signal modifier + corridor modifier (can push the score down as well as up when real reports are mostly clear), capped 1–10, mapped to a level/color. |
| `weather.py` | Open-Meteo fetch (no key needed), past 6h rainfall. |
| `flood.py` | Flood zone risk assessment from the rainfall data, tiered by historical flood-prone area. |
| `narrator.py` | Calls Claude (`claude-sonnet-4-6`) to write the advisory from the score + real corridor reports/signals. Deterministic, grounded fallback template if the API call fails. |
| `storage.py` | Reads/writes `today.json`, `history.json`, `seen_posts.json` (dedup registry). All under `DATA_DIR`, which lives on the persistent volume in production. |
| `telegram.py` | Formats and sends the Telegram message, tracks subscriber count. |
| `static/index.html` | Frontend. Polls `/api/today` every 5 minutes. Has an honest fallback state (not fabricated-looking) for when the API can't be reached. |

---

## Data Sources

**`@lagostraffic961` and `@followlastma`, polled directly via TwitterAPI.io** (`x_poll.py`), one combined query (`(from:lagostraffic961 OR from:followlastma)`) every pipeline run. Fresh within minutes of a real post — no daily crawl cap, no webhook workaround needed. Cost is negligible ($0.15/1000 tweets; a few cents a day at this volume).

**Trigify was the original approach, retired 2026-07-10.** Two saved searches (a Keywords search and a `@lagostraffic961` profile search) plus two real-time forwarding workflows and a `/webhook/trigify-post` endpoint existed to work around Trigify's saved searches being capped at `frequency: DAILY` re-crawl — the pipeline runs 3x/day, so polling alone meant data up to ~24h stale most runs. In practice: the Keywords search never produced a single usable corridor report (zero AND/NOT filtering, mostly noise), and the real-time workflow built to keep `@lagostraffic961` fresh never actually fired in the time it was live. Adam spotted a real accident post on the live Twitter page that the whole pipeline had missed for hours, which is what prompted replacing it outright. All four Trigify resources (both searches, both workflows) were deleted (soft delete, reversible via Trigify's dashboard/API if ever needed) and all of the supporting code (`trigify.py`, the webhook route, `live_posts.json` handling) was removed same-day.

---

## Schedule

Runs at **6:30 AM, 4:00 PM, and 8:00 PM WAT** (APScheduler in `server.py`, `lifespan`). Longest gap between runs is ~10h30m (20:00 → 6:30), which is what `STALE_THRESHOLD_HOURS` (12h) and `SEEN_POSTS_RETENTION_HOURS` (30h) are calibrated against.

The scoring matrix (`config.py` `BASE_SCORE_MATRIX`) already covers all 24 hours for every day — adding the third run required no scoring changes, just the new `add_job` call plus recalibrating the stale threshold.

---

## Infrastructure

- **Railway project:** `harmonious-kindness` (workspace `adamimranza-spec`), service `lagos-traffic-intel`
- **GitHub connection:** `adamimranza-spec/adam-imran`, Root Directory `lagos-traffic-intel`, branch `main` — auto-deploys on push. (This was broken for most of 2026-07-09: the service had no GitHub source connected at all, running a stale manual deploy; reconnecting required transferring the repo from a different GitHub account since Railway's session was authenticated as `adamimranza-spec`.)
- **Persistent volume:** `lagos-traffic-intel-volume`, 500MB, mounted at `/data`. Added 2026-07-09 — before this, Railway's container filesystem was wiped on every deploy/restart, resetting `today.json`/`seen_posts.json` to empty each time. `config.py`'s `DATA_DIR` uses `RAILWAY_VOLUME_MOUNT_PATH` when present, falls back to a local relative path otherwise.
- **`/health`** returns a `version` field (short commit SHA, via `RAILWAY_GIT_COMMIT_SHA` or local `git rev-parse`) specifically so a deploy can be confirmed read-only, without ever needing to hit `/run`.
- **Env vars required** (Railway dashboard + local `.env`, never committed): `ANTHROPIC_API_KEY`, `TWITTERAPI_IO_KEY`, `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHANNEL`.

---

## Testing

`python main.py --dry-run` (local) or `POST /run?dry_run=true` (deployed) runs the full pipeline and writes `today.json` as normal, but skips the Telegram send. Always use this for testing against the live server — a 2026-07-08 incident sent ~17 real messages to real subscribers when a polling loop hit `/run` repeatedly on code that predated `dry_run` support. Never poll `/run` (even with `dry_run=true`) in a retry loop to check deploy status — verify via `/health` (read-only) first, then do one deliberate `/run` call.

---

## Positioning

Kept from the original spec, still true regardless of implementation: the hook when talking about this project is not "I built a traffic app" — it's "Google Maps tells you about traffic. It does not tell you the president is coming to Lagos tomorrow, or that today's corridor reports say a specific bridge is confirmed clear when the model alone would've scored it Gridlock." The differentiator is grounded, honest reporting, not another live-traffic layer.
