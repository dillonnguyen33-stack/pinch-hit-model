"""
MLB Pregame Pull-Risk Board — v1

Companion to the live pinch-hit alert bot. This is the PREGAME side: hours before
first pitch, once lineups are posted, it ranks the day's starting hitters by how
likely each is to be LIFTED early / get fewer at-bats than the market assumes —
i.e. the best UNDER candidates for H+R+RBI / hits props.

WHY "pull-risk" and not literally "pinch-hit after 1-2 ABs":
  Your edge is unders. If a starter is likely to be lifted AT ALL, his expected
  ABs drop from ~4-5 to ~2-3, quietly dropping his projected hits/TB below the
  line. Pinpointing the exact inning is near-impossible; ranking who gets fewer
  ABs than expected is very doable. So the model targets pull risk.

WHAT DRIVES A PULL (post-2022 universal DH — pitchers don't bat, so this is all
position-player platoon/matchup strategy):
  1. Platoon disadvantage — a hitter facing a same-handed starter (L vs L / R vs
     R) with a weak split vs that hand. Biggest single signal.
  2. Bench upgrade — an opposite-handed bat on the bench who's a better matchup.
  3. Manager tendency — how often this manager has pinch-hit/platooned recently
     (reuses the live bot's play-by-play substitution parser).
  4. Lineup spot — bottom-third hitters get pulled more.

DATA: 100% free MLB StatsAPI (schedule+lineups, player splits vs LHP/RHP,
handedness, play-by-play substitutions). No scraping, no paid tier.

USAGE:
  python pinch_hit_pregame_v1.py --print            # compute + print board to console (no Discord)
  python pinch_hit_pregame_v1.py                     # compute + post board to PREGAME_WEBHOOK_URL
  python pinch_hit_pregame_v1.py --date 2026-08-02   # a specific slate
  python pinch_hit_pregame_v1.py --games 3           # limit to first N games (quick test)

ENV:
  PREGAME_WEBHOOK_URL   Discord webhook for the board (required to post)
  ANTHROPIC_API_KEY     optional — if set, Claude writes a one-line summary per pick
  MANAGER_LOOKBACK_DAYS default 14; set 0 to SKIP the recent-manager scan (fast test)
  TOP_N                 default 10 board size
  MIN_SCORE             default 35; hide picks below this
  SEASON                default current year
"""

import os
import re
import sys
import json
import time
import io
import csv
import argparse
import sqlite3
import requests
from datetime import datetime, timezone, timedelta
from zoneinfo import ZoneInfo

ET_TZ   = ZoneInfo("America/New_York")
API     = "https://statsapi.mlb.com/api/v1"
API11   = "https://statsapi.mlb.com/api/v1.1"

# All persistent state lives under DATA_DIR. Point this at a Railway VOLUME mount
# (e.g. DATA_DIR=/data) so predictions, accuracy, caches, and the results DB
# survive redeploys — Railway's default filesystem is ephemeral and wipes them.
DATA_DIR = os.environ.get("DATA_DIR", "")
def _p(name):
    return os.path.join(DATA_DIR, name) if DATA_DIR else name

PREGAME_WEBHOOK_URL   = os.environ.get("PREGAME_WEBHOOK_URL")
ANTHROPIC_API_KEY     = os.environ.get("ANTHROPIC_API_KEY")
MANAGER_LOOKBACK_DAYS = int(os.environ.get("MANAGER_LOOKBACK_DAYS", "14"))
RECENCY_DAYS          = int(os.environ.get("RECENCY_DAYS", "14"))   # window for recent-form signal
PINCH_HIST_DAYS       = int(os.environ.get("PINCH_HIST_DAYS", "14"))  # window for player pinch-hit-for history + --trend (14 trending, 21 more stable)
TREND_TOP_N           = int(os.environ.get("TREND_TOP_N", "15"))      # rows in the --trend leaderboard
LEAD_MINUTES          = int(os.environ.get("LEAD_MINUTES", "60"))    # serve mode: fire this many min before each game's first pitch
POLL_MINUTES          = int(os.environ.get("POLL_MINUTES", "10"))    # serve mode: how often the scheduler checks
GAME_TOP_N            = int(os.environ.get("GAME_TOP_N", "5"))       # max picks per per-game embed
POST_MIN_SCORE        = int(os.environ.get("POST_MIN_SCORE", "60"))  # serve mode: only ping a game if its top pick >= this
POSTED_STATE_PATH     = os.environ.get("POSTED_STATE_PATH", _p("pregame_posted.json"))
RESULTS_HOUR_ET       = int(os.environ.get("RESULTS_HOUR_ET", "3"))   # serve mode: grade the prior day at ~this hour
PREDICTIONS_PATH      = os.environ.get("PREDICTIONS_PATH", _p("pregame_predictions.json"))
ACCURACY_PATH         = os.environ.get("ACCURACY_PATH", _p("pregame_accuracy.json"))
RESULTS_DB_PATH       = os.environ.get("RESULTS_DB_PATH", _p("pregame_results.db"))
TOP_N                 = int(os.environ.get("TOP_N", "10"))
MIN_SCORE             = int(os.environ.get("MIN_SCORE", "55"))       # hide picks below this confidence
SEASON                = int(os.environ.get("SEASON", str(datetime.now(ET_TZ).year)))

# Disk caches so re-runs (and bench lookups) don't re-hit the API.
PLAYER_CACHE_PATH  = os.environ.get("PLAYER_CACHE_PATH",  _p(f"pregame_players_{SEASON}.json"))
MANAGER_CACHE_PATH = os.environ.get("MANAGER_CACHE_PATH", _p("pregame_manager_tendency.json"))
SUBS_CACHE_PATH    = os.environ.get("SUBS_CACHE_PATH",    _p("pregame_subs_scan.json"))
STATCAST_CACHE_PATH = os.environ.get("STATCAST_CACHE_PATH", _p("pregame_statcast.json"))
PITCH_CACHE_PATH    = os.environ.get("PITCH_CACHE_PATH",    _p("pregame_pitchdata.json"))

_session = requests.Session()

# ── small cached GET helpers ──────────────────────────────────────────────────
def _get(url, params=None, timeout=15):
    r = _session.get(url, params=params, timeout=timeout)
    r.raise_for_status()
    return r.json()

def _load(path):
    try:
        with open(path) as f:
            return json.load(f)
    except Exception:
        return {}

def _save(path, data):
    try:
        with open(path, "w") as f:
            json.dump(data, f)
    except Exception as e:
        print(f"[cache save error] {path}: {e}")

_player_cache = _load(PLAYER_CACHE_PATH)

def _fmt(x):
    """Format an OPS/AVG-ish float like .580, or 'n/a'."""
    if x is None:
        return "n/a"
    try:
        return f"{float(x):.3f}".lstrip("0") or "0"
    except Exception:
        return str(x)

# ── Statcast expected stats (#1) — overall xwOBA per hitter (quality + luck) ──────
# Source: Baseball Savant expected-stats leaderboard (free, separate from StatsAPI).
# It's OVERALL (not platoon-split) — used as a stable talent read: weak bats are more
# pull-prone, strong bats get kept. Loaded once/day, cached to disk.
_statcast = None
def _load_statcast():
    global _statcast
    if _statcast is not None:
        return _statcast
    today = datetime.now(ET_TZ).strftime("%Y-%m-%d")
    disk = _load(STATCAST_CACHE_PATH)
    if disk.get("date") == today and disk.get("data"):
        _statcast = {int(k): v for k, v in disk["data"].items()}
        return _statcast
    data = {}
    try:
        r = _session.get("https://baseballsavant.mlb.com/leaderboard/expected_statistics",
                         params={"type": "batter", "year": SEASON, "min": "1", "csv": "true"},
                         headers={"User-Agent": "Mozilla/5.0 (pinch-hit-model)"}, timeout=30)
        r.raise_for_status()
        rd = csv.DictReader(io.StringIO(r.content.decode("utf-8-sig")))
        for row in rd:
            try:
                pid = int(row.get("player_id"))
                xw = _to_float(row.get("est_woba"))
                if pid and xw is not None:
                    data[pid] = {"xwoba": xw, "woba": _to_float(row.get("woba"))}
            except Exception:
                continue
        print(f"[statcast] loaded xwOBA for {len(data)} hitters")
    except Exception as e:
        print(f"[statcast] load error: {e}")
    _statcast = data
    _save(STATCAST_CACHE_PATH, {"date": today, "data": {str(k): v for k, v in data.items()}})
    return _statcast

def statcast_xwoba(pid):
    return _load_statcast().get(int(pid), {})

# ── Starter length (#1) — avg innings per start = how early the bullpen enters ────
def starter_length(pid):
    """Avg IP per start this season (opener/short-leash vs workhorse). Cached daily."""
    if not pid:
        return None
    key = "len_" + str(pid)
    today = datetime.now(ET_TZ).strftime("%Y-%m-%d")
    c = _player_cache.get(key)
    if isinstance(c, dict) and c.get("_date") == today:
        return c.get("ip_gs")
    ip_gs = None
    try:
        d = _get(f"{API}/people/{pid}/stats", params={"stats": "season", "group": "pitching", "season": SEASON})
        for s in d.get("stats", []):
            for spl in s.get("splits", []):
                st = spl.get("stat", {})
                gs = int(st.get("gamesStarted") or 0)
                ip = _to_float(st.get("inningsPitched"))
                if gs > 0 and ip:
                    ip_gs = round(ip / gs, 2)
    except Exception as e:
        print(f"[len] {pid} error: {e}")
    _player_cache[key] = {"ip_gs": ip_gs, "_date": today}
    return ip_gs

# ── Pitch-arsenal matchup (#2) — batter performance vs THIS pitcher's pitch mix ───
# Pitcher arsenal (usage by pitch type) + batter run-value by pitch type, both from
# Baseball Savant. Weighting the batter's performance by the pitcher's usage gives a
# real "does this hitter handle this arsenal" read. This is a PRODUCTION signal (does
# the under cash if he bats), complementing the pull-risk signals.
_pitch_data = None
_ARS_SUFFIXES = ["ff", "si", "fc", "sl", "ch", "cu", "fs", "kn", "st", "sv"]

def _load_pitch_data():
    global _pitch_data
    if _pitch_data is not None:
        return _pitch_data
    today = datetime.now(ET_TZ).strftime("%Y-%m-%d")
    disk = _load(PITCH_CACHE_PATH)
    if disk.get("date") == today and disk.get("arsenal"):
        _pitch_data = {"arsenal": {int(k): v for k, v in disk["arsenal"].items()},
                       "batter": {int(k): v for k, v in disk["batter"].items()}}
        return _pitch_data
    UA = {"User-Agent": "Mozilla/5.0 (pinch-hit-model)"}
    arsenal, batter = {}, {}
    try:
        r = _session.get("https://baseballsavant.mlb.com/leaderboard/pitch-arsenals",
                         params={"year": SEASON, "min": "50", "type": "n_", "csv": "true"},
                         headers=UA, timeout=30)
        r.raise_for_status()
        for row in csv.DictReader(io.StringIO(r.content.decode("utf-8-sig"))):
            try:
                pid = int(row["pitcher"])
                counts = {s.upper(): float(row.get("n_" + s) or 0) for s in _ARS_SUFFIXES}
                tot = sum(counts.values())
                if tot > 0:
                    arsenal[pid] = {pt: round(c / tot, 3) for pt, c in counts.items() if c > 0}
            except Exception:
                continue
    except Exception as e:
        print(f"[pitch] arsenal load error: {e}")
    try:
        r = _session.get("https://baseballsavant.mlb.com/leaderboard/pitch-arsenal-stats",
                         params={"type": "batter", "year": SEASON, "min": "20", "csv": "true"},
                         headers=UA, timeout=30)
        r.raise_for_status()
        for row in csv.DictReader(io.StringIO(r.content.decode("utf-8-sig"))):
            try:
                bid = int(row["player_id"]); pt = row.get("pitch_type")
                if not pt:
                    continue
                batter.setdefault(bid, {})[pt] = {
                    "rv": _to_float(row.get("run_value_per_100")),
                    "woba": _to_float(row.get("woba")),
                    "whiff": _to_float(row.get("whiff_percent"))}
            except Exception:
                continue
    except Exception as e:
        print(f"[pitch] batter load error: {e}")
    _pitch_data = {"arsenal": arsenal, "batter": batter}
    _save(PITCH_CACHE_PATH, {"date": today,
                             "arsenal": {str(k): v for k, v in arsenal.items()},
                             "batter": {str(k): v for k, v in batter.items()}})
    print(f"[pitch] arsenals {len(arsenal)} pitchers, run-values {len(batter)} batters")
    return _pitch_data

_PT_NAMES = {"FF": "4-seam", "SI": "sinker", "FC": "cutter", "SL": "slider", "CH": "change",
             "CU": "curve", "FS": "splitter", "KN": "knuckle", "ST": "sweeper", "SV": "slurve"}

def arsenal_matchup(bid, pid):
    """Batter's run value weighted by the pitcher's pitch usage. Negative wrv = the
    hitter struggles vs this arsenal (tough). Returns {wrv, top_pt, top_frac}."""
    d = _load_pitch_data()
    ars = d["arsenal"].get(int(pid)); bat = d["batter"].get(int(bid))
    if not ars or not bat:
        return {"wrv": None}
    wrv, wsum = 0.0, 0.0
    for pt, frac in ars.items():
        b = bat.get(pt)
        if b and b.get("rv") is not None:
            wrv += frac * b["rv"]; wsum += frac
    top_pt, top_frac = max(ars.items(), key=lambda kv: kv[1])
    return {"wrv": round(wrv, 2) if wsum >= 0.4 else None, "top_pt": top_pt, "top_frac": top_frac,
            "bat_top": bat.get(top_pt)}

# ── player profile: handedness + platoon splits (cached) ──────────────────────
PRIOR_WEIGHT = float(os.environ.get("PRIOR_WEIGHT", "0.6"))   # weight on prior season when blending

def _fetch_splits(pid, season):
    """Return {'vl':{ops,avg,pa}, 'vr':{ops,avg,pa}} for one season (empty if none)."""
    out = {"vl": {"ops": None, "avg": None, "pa": 0}, "vr": {"ops": None, "avg": None, "pa": 0}}
    try:
        data = _get(f"{API}/people/{pid}/stats",
                    params={"stats": "statSplits", "sitCodes": "vl,vr", "group": "hitting", "season": season})
        for s in data.get("stats", []):
            for spl in s.get("splits", []):
                code = spl.get("split", {}).get("code")
                st = spl.get("stat", {})
                if code in ("vl", "vr"):
                    out[code] = {"ops": _to_float(st.get("ops")), "avg": _to_float(st.get("avg")),
                                 "pa": int(st.get("plateAppearances") or 0)}
    except Exception as e:
        print(f"[splits] {pid} {season} error: {e}")
    return out

def _blend(c, p):
    """PA-weighted blend of a current-season split (c) with prior season (p), prior
    discounted by PRIOR_WEIGHT. Stabilizes small current-season samples. `pa` = current
    PA (for display), `pa_eff` = effective blended sample (for the noise guard)."""
    cpa, ppa = c.get("pa") or 0, p.get("pa") or 0
    co, po = c.get("ops"), p.get("ops")
    if co is None and po is None:
        return {"ops": None, "avg": c.get("avg"), "pa": cpa, "pa_eff": cpa, "blended": False}
    if co is None:
        return {"ops": po, "avg": p.get("avg"), "pa": cpa, "pa_eff": ppa, "blended": True}
    if po is None or ppa == 0:
        return {"ops": co, "avg": c.get("avg"), "pa": cpa, "pa_eff": cpa, "blended": False}
    wprev = ppa * PRIOR_WEIGHT
    denom = cpa + wprev
    bo = (co * cpa + po * wprev) / denom
    ca, pa2 = c.get("avg"), p.get("avg")
    ba = (ca * cpa + pa2 * wprev) / denom if (ca is not None and pa2 is not None) else ca
    return {"ops": round(bo, 3), "avg": round(ba, 3) if ba is not None else None,
            "pa": cpa, "pa_eff": cpa + ppa, "blended": True}

def get_hitter_profile(pid, name=None):
    """Returns {name, bats: L/R/S, vl:{ops,avg,pa}, vr:{ops,avg,pa}}. Cached to disk,
    but REFRESHED DAILY — splits change as the season goes, so a cache entry is only
    trusted if it was fetched today (ET). Prevents stale platoon numbers."""
    key = str(pid)
    today = datetime.now(ET_TZ).strftime("%Y-%m-%d")
    cached = _player_cache.get(key)
    if isinstance(cached, dict) and cached.get("_date") == today:
        return cached

    prof = {"name": name, "bats": None, "_date": today,
            "vl": {"ops": None, "avg": None, "pa": 0},
            "vr": {"ops": None, "avg": None, "pa": 0}}
    try:
        person = _get(f"{API}/people/{pid}").get("people", [{}])[0]
        prof["name"] = person.get("fullName", name)
        prof["bats"] = person.get("batSide", {}).get("code")  # L / R / S
    except Exception as e:
        print(f"[profile] handedness error {pid}: {e}")

    # platoon splits — current season BLENDED with prior season to stabilize small
    # samples (#3). When current PA is thin, prior-year data fills in the read.
    cur  = _fetch_splits(pid, SEASON)
    prev = _fetch_splits(pid, SEASON - 1)
    for code in ("vl", "vr"):
        prof[code] = _blend(cur[code], prev[code])

    # Statcast overall xwOBA (#1) — stable talent read (quality/luck).
    sc = statcast_xwoba(pid)
    prof["xwoba"] = sc.get("xwoba")
    prof["woba"]  = sc.get("woba")

    # recency: last RECENCY_DAYS of form (batter-only, refreshed daily with profile).
    prof["recent_ops"] = None
    prof["recent_avg"] = None
    prof["recent_pa"]  = 0
    try:
        end   = datetime.now(ET_TZ).date()
        start = end - timedelta(days=RECENCY_DAYS)
        d = _get(f"{API}/people/{pid}/stats",
                 params={"stats": "byDateRange", "group": "hitting",
                         "startDate": start.strftime("%Y-%m-%d"),
                         "endDate": end.strftime("%Y-%m-%d")})
        for s in d.get("stats", []):
            for spl in s.get("splits", []):
                st = spl.get("stat", {})
                prof["recent_ops"] = _to_float(st.get("ops"))
                prof["recent_avg"] = _to_float(st.get("avg"))
                prof["recent_pa"]  = int(st.get("plateAppearances") or 0)
    except Exception as e:
        print(f"[recency] {pid} error: {e}")

    _player_cache[key] = prof
    return prof

def get_bvp(bid, pid):
    """Career batter-vs-pitcher line. Cached persistently (changes rarely).
    Returns {ab, avg, ops}. BvP samples are small/noisy — used at low weight."""
    key = f"bvp_{bid}_{pid}"
    if key in _player_cache:
        return _player_cache[key]
    res = {"ab": 0, "avg": None, "ops": None}
    try:
        d = _get(f"{API}/people/{bid}/stats",
                 params={"stats": "vsPlayerTotal", "opposingPlayerId": pid, "group": "hitting"})
        for s in d.get("stats", []):
            for spl in s.get("splits", []):
                st = spl.get("stat", {})
                res = {"ab": int(st.get("atBats") or 0),
                       "avg": _to_float(st.get("avg")),
                       "ops": _to_float(st.get("ops"))}
    except Exception as e:
        print(f"[bvp] {bid} vs {pid} error: {e}")
    _player_cache[key] = res
    return res

def _to_float(x):
    try:
        return float(x)
    except Exception:
        return None

# ── recent pinch-hit scan (one pass powers BOTH team tendency AND player history) ─
def _scan_recent_subs(date_str, window_days):
    """Single pass over final games in the last `window_days` (ending the day before
    date_str). Returns {"teams": {tid: {subs,games}}, "players": {"tid|last": {...}}}.
    Cached per (date, window) so the day's first run pays the cost once. This is the
    shared scanner behind manager_tendency() and pinch_hit_history()."""
    if window_days <= 0:
        return {"teams": {}, "players": {}, "pitchers": {}}
    cache = _load(SUBS_CACHE_PATH)
    ck = f"{date_str}:{window_days}"
    if ck in cache:
        return cache[ck]

    end   = datetime.strptime(date_str, "%Y-%m-%d").date() - timedelta(days=1)
    start = end - timedelta(days=window_days - 1)
    print(f"[scan] pinch-hit subs {start}..{end} (first run may take a minute)...")
    pk_date = {}
    try:
        sched = _get(f"{API}/schedule",
                     params={"sportId": 1, "startDate": start.strftime("%Y-%m-%d"),
                             "endDate": end.strftime("%Y-%m-%d"), "gameType": "R"})
        game_pks = []
        for d in sched.get("dates", []):
            for g in d.get("games", []):
                if g.get("status", {}).get("abstractGameState") == "Final":
                    game_pks.append(g["gamePk"])
                    pk_date[g["gamePk"]] = g.get("officialDate") or d.get("date")
    except Exception as e:
        print(f"[scan] schedule error: {e}")
        return {"teams": {}, "players": {}, "pitchers": {}}

    teams = {}     # tid -> {"subs":int, "games":set}
    players = {}   # "tid|last" -> {"name","team","team_id","count"}
    pitchers = {}  # pid_str -> [dates pitched]  (#2 reliever-rest)
    for pk in game_pks:
        try:
            live = _get(f"{API11}/game/{pk}/feed/live", timeout=20)
            box = live.get("liveData", {}).get("boxscore", {}).get("teams", {})
            gdate = pk_date.get(pk)
            side_team, side_abbr = {}, {}
            for side in ("home", "away"):
                t = box.get(side, {}).get("team", {})
                side_team[side] = t.get("id")
                side_abbr[side] = t.get("abbreviation") or t.get("triCode") or str(t.get("id"))
                if t.get("id") is not None:
                    teams.setdefault(t["id"], {"subs": 0, "games": set()})["games"].add(pk)
                for ppid in box.get(side, {}).get("pitchers", []):     # who pitched, for rest
                    if gdate:
                        pitchers.setdefault(str(ppid), []).append(gdate)
            for play in live.get("liveData", {}).get("plays", {}).get("allPlays", []):
                half = play.get("about", {}).get("halfInning")
                bside = "away" if half == "top" else "home"
                bt = side_team.get(bside)
                for ev in play.get("playEvents", []):
                    det = ev.get("details", {})
                    desc = det.get("description") or ""
                    is_sub = ev.get("isSubstitution", False) or det.get("event") == "Offensive Substitution"
                    if is_sub and "pinch-hitter" in desc.lower() and bt is not None:
                        teams[bt]["subs"] += 1
                        m = re.search(r"replaces\s+(.+?)[.\n]", desc, re.IGNORECASE)
                        if m:
                            full = m.group(1).strip()
                            key = f"{bt}|{_lastname(full)}"
                            e = players.setdefault(key, {"name": full, "team": side_abbr.get(bside),
                                                         "team_id": bt, "count": 0})
                            e["count"] += 1
        except Exception as e:
            print(f"[scan] game {pk} error: {e}")

    result = {"teams": {str(tid): {"subs": t["subs"], "games": len(t["games"])}
                        for tid, t in teams.items()},
              "players": players,
              "pitchers": {p: sorted(set(ds)) for p, ds in pitchers.items()}}
    cutoff = (datetime.now(ET_TZ).date() - timedelta(days=3)).strftime("%Y-%m-%d")
    cache = {k: v for k, v in cache.items() if k.split(":")[0] >= cutoff}
    cache[ck] = result
    _save(SUBS_CACHE_PATH, cache)
    return result

def unavailable_relievers(date_str):
    """Pitchers who appeared on BOTH of the last two game days (back-to-back) — a
    reasonable 'likely gassed / down today' proxy. Returns a set of pitcher ids (#2)."""
    pit = _scan_recent_subs(date_str, PINCH_HIST_DAYS).get("pitchers", {})
    alldates = set()
    for ds in pit.values():
        alldates.update(ds)
    recent = sorted(alldates)[-2:]
    if len(recent) < 2:
        return set()
    d1, d2 = recent[-1], recent[-2]
    return {int(pid) for pid, ds in pit.items() if d1 in ds and d2 in ds}

def manager_tendency(date_str):
    """{team_id: {subs,games,rate,tier}} over MANAGER_LOOKBACK_DAYS. Tiers are relative
    to the league distribution. Derived from the shared scan."""
    teams = _scan_recent_subs(date_str, MANAGER_LOOKBACK_DAYS).get("teams", {})
    if not teams:
        return {}
    result, rates = {}, []
    for tid, t in teams.items():
        g = max(1, t["games"])
        rate = t["subs"] / g
        result[tid] = {"subs": t["subs"], "games": g, "rate": round(rate, 2)}
        rates.append(rate)
    rates.sort()
    def pct(p):
        return rates[min(len(rates) - 1, int(p * len(rates)))] if rates else 0
    hi, lo = pct(0.66), pct(0.33)
    for tid, r in result.items():
        r["tier"] = "high" if r["rate"] >= hi else ("low" if r["rate"] <= lo else "med")
    return result

_TEAM_ABBR = {}
def _team_abbr(tid):
    """team_id -> abbreviation (e.g. 116 -> DET). Lazy-loaded once from /teams."""
    if not _TEAM_ABBR:
        try:
            for t in _get(f"{API}/teams", params={"sportId": 1}).get("teams", []):
                _TEAM_ABBR[t["id"]] = t.get("abbreviation") or t.get("teamCode") or str(t["id"])
        except Exception:
            pass
    return _TEAM_ABBR.get(tid, str(tid))

def pinch_hit_history(date_str, window_days=None):
    """Ranked list of players pinch-hit for in the window (most-pulled first):
    [{name, team, team_id, count, key}]. Powers the player signal + --trend board."""
    window_days = window_days or PINCH_HIST_DAYS
    players = _scan_recent_subs(date_str, window_days).get("players", {})
    rows = [{"name": e["name"], "team": _team_abbr(e.get("team_id")), "team_id": e.get("team_id"),
             "count": e["count"], "key": k} for k, e in players.items()]
    rows.sort(key=lambda r: r["count"], reverse=True)
    return rows

# ── scoring ───────────────────────────────────────────────────────────────────
def classify_matchup(prof, sp_hand):
    """Which pull scenario this starter fits:
      'disadvantage' — SCENARIO 2: same-handed weak-side start. Poor matchup; pulled
                       if early ABs sour (live trigger) or the pen stays his weak hand.
      'flip'         — SCENARIO 1: opposite-handed platoon start. Started FOR the
                       favorable starter handedness; likely pinch-hit when the opposing
                       pen brings his weak hand and flips the edge away. Only counts if
                       he's a platoon-profile part-timer with a real strong-side edge.
      'none'         — no strong platoon pull angle.
    """
    bats = prof.get("bats")
    if bats not in ("L", "R"):
        return "none"
    faced = "vl" if sp_hand == "L" else "vr"
    opp   = "vr" if sp_hand == "L" else "vl"
    fo = prof.get(faced, {}).get("ops")
    oo = prof.get(opp,   {}).get("ops")
    total_pa = (prof.get(faced, {}).get("pa") or 0) + (prof.get(opp, {}).get("pa") or 0)
    if bats == sp_hand:
        return "disadvantage" if (fo is not None and oo is not None and fo < oo) else "none"
    gap = (fo - oo) if (fo is not None and oo is not None) else None
    if 0 < total_pa <= 250 and gap is not None and gap > 0.080:
        return "flip"
    return "none"

def score_starter(prof, sp_hand, sp_name, order, mgr_tier, bench_note, scenario,
                  pen_mix=None, bvp=None, player_ph_count=0, sp_len=None, arsenal=None):
    """Return (score 0-100, reasons[list]). Higher = more likely lifted = better under."""
    reasons = []
    score = 0.0
    bats  = prof.get("bats")
    faced = "vl" if sp_hand == "L" else "vr"     # split vs the starter's hand
    opp   = "vr" if sp_hand == "L" else "vl"
    fo    = prof.get(faced, {}).get("ops")
    oo    = prof.get(opp,   {}).get("ops")
    fpa   = prof.get(faced, {}).get("pa", 0)
    opa   = prof.get(opp,   {}).get("pa", 0)
    # effective (blended) sample drives the noise guard; current PA is for display
    fpa_eff = prof.get(faced, {}).get("pa_eff", fpa)
    blended = prof.get(faced, {}).get("blended", False)
    total_pa = fpa + opa

    if scenario == "disadvantage":
        # SCENARIO 2: same-handed weak-side start. Poor matchup; pulled if early ABs
        # sour (that's the LIVE trigger) or the opposing pen stays his weak hand.
        gap = oo - fo
        pts = min(35.0, gap / 0.200 * 35.0)
        if fo < 0.680:
            pts += min(10.0, (0.680 - fo) / 0.200 * 10.0)
        if fpa_eff < 30:                   # thin even after blending — damp confidence
            pts *= 0.5
        score += pts
        if fpa_eff >= 30:
            samp = " (blended w/ prior yr)" if blended and fpa < 30 else ""
        else:
            samp = f", small sample {fpa_eff} PA vs {sp_hand}HP"
        reasons.append(f"Poor matchup — bats {bats} into {sp_hand}HP: {_fmt(fo)} OPS vs "
                       f"{sp_hand}HP (vs {_fmt(oo)} opposite){samp}")
        if pen_mix and pen_mix.get("total", 0) >= 4:
            same = pen_mix.get(sp_hand, 0); tot = pen_mix["total"]; share = same / tot
            if share >= 0.60:
                score += min(12.0, (share - 0.50) / 0.40 * 12.0)
                reasons.append(f"Opposing pen {same}/{tot} {sp_hand}HP ({share:.0%}) — "
                               f"weak matchup persists after the starter")
            elif share <= 0.40:
                reasons.append(f"Opposing pen only {same}/{tot} {sp_hand}HP ({share:.0%}) — "
                               f"a favorable reliever is likely later (lower certainty)")
            else:
                reasons.append(f"Opposing pen {same}/{tot} {sp_hand}HP ({share:.0%})")

    elif scenario == "flip":
        # SCENARIO 1: opposite-handed platoon start FOR the matchup. Likely pinch-hit
        # when the opposing pen brings his weak hand (== bats) and flips the edge away.
        gap = fo - oo
        pts = min(20.0, gap / 0.250 * 20.0)
        if oo is not None and oo < 0.680:
            pts += min(10.0, (0.680 - oo) / 0.200 * 10.0)
        score += pts
        reasons.append(f"Platoon start — {bats}HH started vs {sp_hand}HP for the edge "
                       f"({_fmt(fo)} vs {sp_hand}HP, {_fmt(oo)} vs {bats}HP)")
        if pen_mix and pen_mix.get("total", 0) >= 4:
            fn = pen_mix.get(bats, 0); tot = pen_mix["total"]; fshare = fn / tot
            if fshare >= 0.55:
                score += min(14.0, (fshare - 0.45) / 0.45 * 14.0)
                reasons.append(f"Opposing pen {fn}/{tot} {bats}HP ({fshare:.0%}) — matchup "
                               f"likely FLIPS to his weak side → pinch-hit trigger")
            else:
                reasons.append(f"Opposing pen only {fn}/{tot} {bats}HP ({fshare:.0%}) — "
                               f"flip less likely (lower certainty)")

    elif bats == "S":
        reasons.append("Switch hitter — no platoon disadvantage")
    else:
        reasons.append(f"No strong platoon pull angle ({bats} vs {sp_hand}HP)")

    # Recent form: slumping hitters get benched/lifted; hot hitters get ridden.
    r_ops = prof.get("recent_ops")
    r_pa  = prof.get("recent_pa") or 0
    if r_pa >= 15 and r_ops is not None:
        if r_ops < 0.620:
            score += min(10.0, (0.620 - r_ops) / 0.220 * 10.0)
            reasons.append(f"Cold last {RECENCY_DAYS}d: {_fmt(r_ops)} OPS over {r_pa} PA — bench/pull risk up")
        elif r_ops > 0.820:
            score *= 0.85
            reasons.append(f"Hot last {RECENCY_DAYS}d: {_fmt(r_ops)} OPS over {r_pa} PA — hot hand, managers ride it (dampened)")
        else:
            reasons.append(f"Recent form: {_fmt(r_ops)} OPS last {RECENCY_DAYS}d ({r_pa} PA)")
    elif 0 < r_pa < 15:
        reasons.append(f"Barely played last {RECENCY_DAYS}d ({r_pa} PA) — bench/platoon role")

    # Historical matchup vs THIS starter (career). Small samples — low weight, show N.
    if bvp and bvp.get("ab", 0) >= 10:
        b_ops = bvp.get("ops")
        ab = bvp["ab"]
        if b_ops is not None and b_ops < 0.600:
            score += 5
            reasons.append(f"Career vs {sp_name}: {_fmt(bvp.get('avg'))} AVG / {_fmt(b_ops)} OPS ({ab} AB) — has struggled")
        elif b_ops is not None and b_ops > 0.900:
            score -= 5
            reasons.append(f"Career vs {sp_name}: {_fmt(bvp.get('avg'))} AVG / {_fmt(b_ops)} OPS ({ab} AB) — has hit him well")
        else:
            reasons.append(f"Career vs {sp_name}: {_fmt(bvp.get('avg'))} AVG ({ab} AB)")
    elif bvp and bvp.get("ab", 0) > 0:
        reasons.append(f"Career vs {sp_name}: limited history ({bvp['ab']} AB)")

    if pen_mix and pen_mix.get("rested_out") and scenario in ("disadvantage", "flip"):
        reasons.append(f"({pen_mix['rested_out']} opp reliever(s) likely down — pitched back-to-back)")

    # Starter length (#1): short-leash/opener → bullpen enters early → pull happens
    # sooner; workhorse → starter stays in → flip is less likely.
    if sp_len is not None and scenario in ("disadvantage", "flip"):
        if sp_len < 4.2:
            score += 6
            reasons.append(f"Opp starter avgs {sp_len:.1f} IP/start — early bullpen, matchup flips sooner")
        elif sp_len > 5.5 and scenario == "flip":
            score *= 0.90
            reasons.append(f"Opp starter avgs {sp_len:.1f} IP/start — goes deep, flip less likely")

    # Pitch-arsenal matchup (#2): does the hitter handle this pitcher's mix? PRODUCTION
    # signal (does the under cash if he bats), not pull-risk.
    if arsenal and arsenal.get("wrv") is not None:
        wrv = arsenal["wrv"]
        score += max(-8.0, min(8.0, -wrv * 6.0))   # negative wrv (struggles) → boosts under
        pt = _PT_NAMES.get(arsenal.get("top_pt"), arsenal.get("top_pt", ""))
        if wrv <= -0.4:
            reasons.append(f"Tough vs {sp_name}'s arsenal ({wrv:+.1f} RV/100; heavy {pt}) — struggles vs his mix")
        elif wrv >= 0.4:
            reasons.append(f"Handles {sp_name}'s arsenal well ({wrv:+.1f} RV/100) — favorable, less under value")

    # Statcast overall quality (#1): weak bats are more pull-prone; strong bats get kept.
    xw = prof.get("xwoba")
    if xw is not None:
        if xw < 0.300:
            score += 6
            reasons.append(f"Weak overall bat ({_fmt(xw)} xwOBA, Statcast) — pull-prone")
        elif xw > 0.360:
            score *= 0.90
            reasons.append(f"Strong overall bat ({_fmt(xw)} xwOBA, Statcast) — managers keep him")

    # Player's OWN recent pinch-hit-for history — has THIS guy been getting pulled?
    if player_ph_count >= 2:
        score += min(10.0, player_ph_count * 3.0)
        reasons.append(f"Pinch-hit for {player_ph_count}× in last {PINCH_HIST_DAYS}d — "
                       f"repeatedly pulled lately")
    elif player_ph_count == 1:
        reasons.append(f"Pinch-hit for once in last {PINCH_HIST_DAYS}d")

    if bench_note:
        score += 18
        reasons.append(f"Bench upgrade available: {bench_note}")

    add = {"high": 20, "med": 10, "low": 3}.get(mgr_tier, 10)
    score += add
    reasons.append(f"Manager pinch-hit tendency: {mgr_tier.upper()}")

    if order and order >= 7:
        score += 10
        reasons.append(f"Batting {order} — bottom third, common pull spot")
    elif order and order <= 3:
        score -= 6

    # Role: everyday regulars get ridden through platoon spots; part-timers get
    # pulled. Total PA is a clean proxy this far into the season.
    if total_pa >= 300:
        score *= 0.70
        reasons.append(f"Everyday regular (~{total_pa} PA) — managers ride regulars (dampened)")
    elif 0 < total_pa <= 150:
        score *= 1.10
        reasons.append(f"Part-time/platoon profile (~{total_pa} PA) — higher pull risk")

    return max(0, min(100, round(score))), reasons

def find_bench_upgrade(ref_ops, plan_hand, bench_players):
    """Is there a bench bat OPPOSITE-handed to `plan_hand` (so it holds the platoon
    edge vs a `plan_hand` pitcher) whose OPS vs `plan_hand` clearly beats ref_ops?
    Used by both scenarios: Scenario 2 plans around the same-handed starter; Scenario 1
    plans around the opposing pen's incoming flip-hand reliever."""
    faced = "vl" if plan_hand == "L" else "vr"
    ref = ref_ops or 0.0
    best = None
    for bp in bench_players:
        bprof = get_hitter_profile(bp["id"], bp["name"])
        b_bats = bprof.get("bats")
        if b_bats == plan_hand:            # want the opposite-handed platoon edge
            continue
        b_fo = bprof.get(faced, {}).get("ops")
        if b_fo is not None and b_fo >= ref + 0.060:
            if best is None or b_fo > best[1]:
                best = (bprof.get("name") or bp["name"], b_fo, b_bats)
    if best:
        return f"{best[0]} ({best[2]}, {_fmt(best[1])} OPS vs {plan_hand}HP)"
    return None

# ── slate ─────────────────────────────────────────────────────────────────────
def get_slate(date_str, limit=None):
    """Games with probable pitchers. Returns list of dicts."""
    sched = _get(f"{API}/schedule",
                 params={"sportId": 1, "date": date_str,
                         "hydrate": "probablePitcher,team"})
    games = [g for d in sched.get("dates", []) for g in d.get("games", [])]
    if limit:
        games = games[:limit]
    return games

def confirmed_lineup(game_pk):
    """The OFFICIAL lineup from the boxscore batting-order card — authoritative,
    unlike the schedule's `lineups` hydrate which can carry a PREDICTED lineup
    that later changes (this is what wrongly included a non-starter). Returns
    {"away":[{id,fullName}], "home":[...]} in batting order; empty until posted."""
    out = {"away": [], "home": []}
    try:
        data = _get(f"{API}/game/{game_pk}/boxscore", timeout=20)   # lighter than feed/live
    except Exception:
        return out
    box = data.get("teams", {})
    for side in ("away", "home"):
        players = box.get(side, {}).get("players", {})
        for pid in box.get(side, {}).get("battingOrder", []):
            pd = players.get(f"ID{pid}", {})
            out[side].append({"id": pid, "fullName": pd.get("person", {}).get("fullName", "")})
    return out

def pitcher_hand(pid):
    """Cached L/R for a pitcher (used for the probable SP and each reliever)."""
    key = "ph_" + str(pid)
    if key in _player_cache:
        return _player_cache[key]
    hand = None
    try:
        person = _get(f"{API}/people/{pid}").get("people", [{}])[0]
        hand = person.get("pitchHand", {}).get("code")   # L / R / (rarely S)
    except Exception:
        pass
    _player_cache[key] = hand
    return hand

def opposing_bullpen_mix(team_id, exclude_sp_id, unavailable=None):
    """L/R counts of the opposing team's AVAILABLE bullpen (active-roster pitchers
    minus today's probable starter, minus back-to-back arms that are likely down
    today — #2). The L/R ratio is what the model uses."""
    unavailable = unavailable or set()
    mix = {"L": 0, "R": 0, "total": 0, "rested_out": 0}
    try:
        data = _get(f"{API}/teams/{team_id}/roster", params={"rosterType": "active"})
    except Exception as e:
        print(f"[bullpen] team {team_id} error: {e}")
        return mix
    for p in data.get("roster", []):
        if p.get("position", {}).get("type") != "Pitcher":
            continue
        ppid = p["person"]["id"]
        if ppid == exclude_sp_id:
            continue
        if ppid in unavailable:               # threw back-to-back — likely unavailable today
            mix["rested_out"] += 1
            continue
        h = pitcher_hand(ppid)
        if h in ("L", "R"):
            mix[h] += 1
            mix["total"] += 1
    return mix

def active_hitters(team_id):
    """Active-roster position players (id,name) for bench-upgrade checks."""
    out = []
    try:
        data = _get(f"{API}/teams/{team_id}/roster", params={"rosterType": "active"})
        for p in data.get("roster", []):
            pos = p.get("position", {}).get("type", "")
            if pos and pos != "Pitcher":
                out.append({"id": p["person"]["id"], "name": p["person"]["fullName"]})
    except Exception as e:
        print(f"[roster] team {team_id} error: {e}")
    return out

def analyze_game(g, mgr, lineup=None, ph_hist=None, unavailable=None):
    """Score every starter in a single game. Uses the OFFICIAL batting-order lineup
    (confirmed_lineup) — not the schedule's predicted feed — so non-starters can't
    slip in. `ph_hist` is {"tid|last": count} of recent pinch-hits; `unavailable` is
    the set of gassed relievers to drop from the pen. Returns candidates sorted by score."""
    candidates = []
    ph_hist = ph_hist or {}
    if lineup is None:
        lineup = confirmed_lineup(g.get("gamePk"))
    for side, opp_side in (("away", "home"), ("home", "away")):
        starters = lineup.get(side, [])
        if len(starters) < 9:
            continue
        team = g["teams"][side]["team"]
        opp_team = g["teams"][opp_side]["team"]
        sp = g["teams"][opp_side].get("probablePitcher", {})
        sp_id = sp.get("id")
        sp_name = sp.get("fullName", "TBD")
        sp_hand = pitcher_hand(sp_id) if sp_id else None
        if sp_hand not in ("L", "R"):
            continue  # can't score platoon without SP handedness

        tier = mgr.get(str(team["id"]), {}).get("tier", "med")
        mgr_meta = mgr.get(str(team["id"]), {})

        starter_ids = {s["id"] for s in starters}
        bench = [p for p in active_hitters(team["id"]) if p["id"] not in starter_ids]
        pen_mix = opposing_bullpen_mix(opp_team["id"], sp_id, unavailable)
        sp_len  = starter_length(sp_id)          # opposing starter's avg IP/start (#1)

        for order, s in enumerate(starters, start=1):
            prof = get_hitter_profile(s["id"], s.get("fullName"))
            scenario = classify_matchup(prof, sp_hand)
            bats  = prof.get("bats")
            faced = "vl" if sp_hand == "L" else "vr"
            opp   = "vr" if sp_hand == "L" else "vl"
            bench_note = None
            if scenario == "disadvantage":
                bench_note = find_bench_upgrade(prof.get(faced, {}).get("ops"), sp_hand, bench)
            elif scenario == "flip":
                bench_note = find_bench_upgrade(prof.get(opp, {}).get("ops"), bats, bench)
            bvp = get_bvp(s["id"], sp_id) if sp_id else None
            ph_count = ph_hist.get(f"{team['id']}|{_lastname(prof.get('name') or s.get('fullName'))}", 0)
            arsenal = arsenal_matchup(s["id"], sp_id) if sp_id else None
            score, reasons = score_starter(prof, sp_hand, sp_name, order, tier, bench_note,
                                           scenario, pen_mix, bvp, ph_count, sp_len, arsenal)
            if score >= MIN_SCORE:
                candidates.append({
                    "score": score, "name": prof.get("name") or s.get("fullName"),
                    "pid": s["id"], "gamePk": g.get("gamePk"),
                    "bats": prof.get("bats"), "order": order, "scenario": scenario,
                    "team": team.get("abbreviation") or team.get("name"),
                    "opp": opp_team.get("abbreviation") or opp_team.get("name"),
                    "sp_name": sp_name, "sp_hand": sp_hand,
                    "reasons": reasons,
                    "mgr_rate": mgr_meta.get("rate"), "mgr_games": mgr_meta.get("games"),
                })
    candidates.sort(key=lambda c: c["score"], reverse=True)
    return candidates

def build_board(date_str, limit=None):
    """Whole-slate board (manual/--print mode)."""
    games = get_slate(date_str, limit)
    mgr   = manager_tendency(date_str)
    ph_hist = {r["key"]: r["count"] for r in pinch_hit_history(date_str)}
    unavail = unavailable_relievers(date_str)
    print(f"[slate] {date_str}: {len(games)} game(s) considered")
    candidates = []
    for g in games:
        candidates.extend(analyze_game(g, mgr, ph_hist=ph_hist, unavailable=unavail))
    _save(PLAYER_CACHE_PATH, _player_cache)
    candidates.sort(key=lambda c: c["score"], reverse=True)
    return candidates[:TOP_N]

# ── optional Claude one-liner per pick ────────────────────────────────────────
def claude_summary(cand):
    if not ANTHROPIC_API_KEY:
        return None
    try:
        bullets = "; ".join(cand["reasons"])
        r = _session.post(
            "https://api.anthropic.com/v1/messages",
            headers={"x-api-key": ANTHROPIC_API_KEY,
                     "anthropic-version": "2023-06-01", "content-type": "application/json"},
            json={"model": "claude-haiku-4-5-20251001", "max_tokens": 80,
                  "system": "You summarize an MLB pull-risk pick in ONE punchy sentence "
                            "for a bettor shopping unders (hits / H+R+RBI). No preamble.",
                  "messages": [{"role": "user",
                                "content": f"{cand['name']} ({cand['team']} vs {cand['sp_hand']}HP "
                                           f"{cand['sp_name']}). Signals: {bullets}"}]},
            timeout=8)
        r.raise_for_status()
        return r.json()["content"][0]["text"].strip()
    except Exception as e:
        print(f"[claude] {e}")
        return None

# ── render ────────────────────────────────────────────────────────────────────
def _emoji(score):
    return "🔴" if score >= 80 else "🟠" if score >= 60 else "🟡" if score >= 45 else "⚪"

def render_board(date_str, board):
    lines = [f"🎯 **PREGAME PULL-RISK BOARD — {date_str}**",
             "_Ranked most→least likely to be lifted early / limited ABs. "
             "Bet unders (H+R+RBI / hits), shop best odds._\n"]
    if not board:
        lines.append("No qualifying pull-risk candidates on this slate "
                     "(lineups may not be posted yet, or no strong platoon spots).")
        return "\n".join(lines)
    tag = {"flip": " · 🔄 platoon-flip start", "disadvantage": " · ⚠️ poor matchup"}
    for i, c in enumerate(board, 1):
        head = (f"**#{i}  {_emoji(c['score'])} {c['score']}  {c['name']} "
                f"({c['bats']}, batting {c['order']})** — {c['team']} vs "
                f"{c['sp_hand']}HP {c['sp_name']}{tag.get(c.get('scenario'), '')}")
        lines.append(head)
        for rz in c["reasons"]:
            lines.append(f"     • {rz}")
        summ = claude_summary(c)
        if summ:
            lines.append(f"     → {summ}")
        lines.append("")
    return "\n".join(lines)

def post_discord(content):
    if not PREGAME_WEBHOOK_URL:
        print("[discord] PREGAME_WEBHOOK_URL not set — printing instead:\n")
        print(content)
        return
    for i in range(0, len(content), 1900):
        try:
            _session.post(PREGAME_WEBHOOK_URL, json={"content": content[i:i+1900]}, timeout=10)
            time.sleep(0.3)
        except Exception as e:
            print(f"[discord] {e}")

# ── Discord embed (per-game, serve mode) ──────────────────────────────────────
def _color(score):
    return 0xE74C3C if score >= 80 else 0xE67E22 if score >= 60 else 0xF1C40F if score >= 45 else 0x95A5A6

def _ord(n):
    if 10 <= n % 100 <= 20:
        suf = "th"
    else:
        suf = {1: "st", 2: "nd", 3: "rd"}.get(n % 10, "th")
    return f"{n}{suf}"

def _fmt_local(dt_utc):
    lt = dt_utc.astimezone(ET_TZ)
    h = lt.hour % 12 or 12
    return f"{h}:{lt.minute:02d} {'AM' if lt.hour < 12 else 'PM'} ET"

def _post_embed(embed):
    if not PREGAME_WEBHOOK_URL:
        print("[discord] PREGAME_WEBHOOK_URL not set — embed preview:")
        print(json.dumps(embed, indent=2)[:1800])
        return
    try:
        _session.post(PREGAME_WEBHOOK_URL, json={"embeds": [embed]}, timeout=10)
    except Exception as e:
        print(f"[discord embed] {e}")

def _conf_label(s):
    return "Elite" if s >= 80 else "High" if s >= 65 else "Lean" if s >= 55 else "Low"

# Plain-language matchup type — no emoji, no caution sign.
_MATCHUP = {
    "disadvantage": "weak-side matchup",
    "flip": "platoon-flip (good vs starter, weak vs same-side relievers later)",
}

def post_game_embed(g, cands, when_str):
    away = g["teams"]["away"]["team"].get("abbreviation") or g["teams"]["away"]["team"]["name"]
    home = g["teams"]["home"]["team"].get("abbreviation") or g["teams"]["home"]["team"]["name"]
    title = f"Pinch-hit board — {away} @ {home}"
    if not cands:
        return
    top = cands[:GAME_TOP_N]
    fields = []
    for i, c in enumerate(top, 1):
        matchup = _MATCHUP.get(c.get("scenario"), "")
        nm = f"#{i}  {c['name']} ({c['bats']}, {_ord(c['order'])})  —  {c['score']}/100 {_conf_label(c['score'])}"
        val = (f"_{matchup}_\n" if matchup else "")
        val += "\n".join(f"• {r}" for r in c["reasons"])
        summ = claude_summary(c)
        if summ:
            val += f"\n→ {summ}"
        fields.append({"name": nm[:256], "value": val[:1024], "inline": False})
    _post_embed({
        "title": title,
        "description": (f"First pitch **{when_str}** · **Confidence /100** = the model's read on how "
                        f"likely each starter is lifted early / limited to few at-bats. "
                        f"Higher = better under (H+R+RBI / hits)."),
        "color": 0x5865F2,
        "fields": fields,
        "footer": {"text": "Pinch-hit model · review and forward your picks"},
    })

def trend_board(date_str, to_discord=True):
    """Leaderboard of players most often pinch-hit for over the last PINCH_HIST_DAYS.
    Snapshots to the DB and (optionally) posts a Discord embed."""
    rows = pinch_hit_history(date_str)
    db_snapshot_pinch_history(date_str, PINCH_HIST_DAYS, rows)
    rows = rows[:TREND_TOP_N]
    if not rows:
        print(f"[trend] no pinch-hit history in the last {PINCH_HIST_DAYS} days.")
        return
    lines = [f"{i}. **{r['name']}** ({r['team']}) — pinch-hit for **{r['count']}×**"
             for i, r in enumerate(rows, 1)]
    body = "\n".join(lines)
    if to_discord and PREGAME_WEBHOOK_URL:
        _post_embed({
            "title": f"📋 Pinch-hit trend — last {PINCH_HIST_DAYS} days",
            "description": "Players most often lifted for a pinch-hitter (repeat under targets).\n\n" + body[:3800],
            "color": 0x5865F2,
            "footer": {"text": f"Rolling {PINCH_HIST_DAYS}-day window"},
        })
        print(f"[trend] posted leaderboard ({len(rows)} players).")
    else:
        print(f"Pinch-hit trend — last {PINCH_HIST_DAYS}d\n" + body)

# ── per-game scheduler (serve mode) ───────────────────────────────────────────
def _mark_posted(date_str, pk):
    d = _load(POSTED_STATE_PATH)
    cutoff = (datetime.now(ET_TZ).date() - timedelta(days=2)).strftime("%Y-%m-%d")
    d = {k: v for k, v in d.items() if k >= cutoff}   # prune old days
    d.setdefault(date_str, [])
    if pk not in d[date_str]:
        d[date_str].append(pk)
    _save(POSTED_STATE_PATH, d)

def run_scheduler():
    print(f"[serve] Pregame scheduler up — analyzes each game the moment MLB posts its "
          f"official lineup (both batting orders). Polls every {POLL_MINUTES}m. "
          f"Pings only when top confidence ≥ {POST_MIN_SCORE}.")
    if not PREGAME_WEBHOOK_URL:
        print("[serve][warn] PREGAME_WEBHOOK_URL not set — embeds will print to console.")
    # Persistence check — surfaces DATA_DIR problems in the deploy logs.
    if DATA_DIR:
        try:
            os.makedirs(DATA_DIR, exist_ok=True)
            _t = os.path.join(DATA_DIR, ".write_test")
            open(_t, "w").close(); os.remove(_t)
            print(f"[serve] DATA_DIR={DATA_DIR} (writable) · results DB → {RESULTS_DB_PATH}")
        except Exception as e:
            print(f"[serve][warn] DATA_DIR={DATA_DIR} is NOT writable ({e}) — check the volume mount path.")
    else:
        print("[serve][warn] DATA_DIR unset — state saves to the working dir and WIPES on redeploy. "
              "Set DATA_DIR to your volume mount (e.g. /data).")
    while True:
        try:
            today = datetime.now(ET_TZ).strftime("%Y-%m-%d")
            sched = _get(f"{API}/schedule", params={"sportId": 1, "date": today,
                        "hydrate": "probablePitcher,team"})
            games = [g for d in sched.get("dates", []) for g in d.get("games", [])]
            mgr = manager_tendency(today)                 # cached per date
            db_snapshot_manager(today, mgr)               # historical coach-tendency snapshot
            ph_rows = pinch_hit_history(today)            # player pinch-hit history (shared scan)
            ph_hist = {r["key"]: r["count"] for r in ph_rows}
            unavail = unavailable_relievers(today)        # gassed relievers (shared scan)
            posted = set(_load(POSTED_STATE_PATH).get(today, []))
            now = datetime.now(timezone.utc)

            for g in games:
                pk = g.get("gamePk")
                if pk in posted:
                    continue
                state = g.get("status", {}).get("abstractGameState")
                if state in ("Live", "Final"):           # started before we could post — skip
                    _mark_posted(today, pk); posted.add(pk)
                    continue
                gd = g.get("gameDate")
                try:
                    first = datetime.fromisoformat(gd.replace("Z", "+00:00")) if gd else None
                except Exception:
                    first = None
                # Fire AS SOON AS the official lineup drops — no fixed lead time. MLB
                # posts these ~2-4h out; acting immediately gives the most time to shop.
                lineup = confirmed_lineup(pk)
                if len(lineup["away"]) < 9 or len(lineup["home"]) < 9:
                    continue                             # official card not posted yet — retry next poll
                when = _fmt_local(first) if first else "TBD"
                cands = analyze_game(g, mgr, lineup, ph_hist, unavail)
                _save(PLAYER_CACHE_PATH, _player_cache)
                _mark_posted(today, pk); posted.add(pk)  # analyzed — don't repeat this game
                a = g["teams"]["away"]["team"].get("abbreviation")
                h = g["teams"]["home"]["team"].get("abbreviation")
                top = cands[0]["score"] if cands else 0
                if top >= POST_MIN_SCORE:                # only PING when there's a real candidate
                    post_game_embed(g, cands, when)
                    record_predictions(today, cands[:GAME_TOP_N])
                    print(f"[serve] posted {a} @ {h} — top {top}, {len(cands)} pick(s)")
                else:
                    print(f"[serve] skipped {a} @ {h} — top {top} < {POST_MIN_SCORE} (no ping)")

            # Daily grading + trend leaderboard: once past RESULTS_HOUR_ET (once/day).
            now_et = datetime.now(ET_TZ)
            if now_et.hour >= RESULTS_HOUR_ET:
                yday = (now_et.date() - timedelta(days=1)).strftime("%Y-%m-%d")
                graded = {d["date"] for d in _load(ACCURACY_PATH).get("days", [])}
                if yday not in graded:
                    print(f"[results] grading {yday}...")
                    grade_day(yday)
                    trend_board(today)          # post the daily "most pinch-hit-for" leaderboard
        except Exception as e:
            print(f"[serve] loop error: {e}")
        time.sleep(POLL_MINUTES * 60)

# ── RESULTS / DAILY GRADING ───────────────────────────────────────────────────
# The betting thesis is "fewer ABs than the market assumes." So a pick "HITS" if
# the player was pinch-hit for OR got <= 3 PA (the under had a real shot). We also
# record actual PA / H / R / RBI so you can see whether the lines would have cashed.
# ── SQLite results database ───────────────────────────────────────────────────
# A queryable record of every pick: the prediction (score, scenario, WHY it was
# flagged) plus the actual outcome — so "why are we losing" is answerable with SQL.
# Plus a daily snapshot of coach pinch-hit tendencies for historical analysis.
def _db():
    conn = sqlite3.connect(RESULTS_DB_PATH, timeout=10)
    conn.execute("""CREATE TABLE IF NOT EXISTS picks(
        date TEXT, game_pk INTEGER, pid INTEGER, name TEXT, team TEXT, opp TEXT,
        sp_name TEXT, sp_hand TEXT, score INTEGER, scenario TEXT, bats TEXT,
        batting_order INTEGER, reasons TEXT, posted_at TEXT,
        graded INTEGER DEFAULT 0, pa INTEGER, ab INTEGER, h INTEGER, r INTEGER,
        rbi INTEGER, hrr INTEGER, pulled INTEGER, hit INTEGER, outcome_note TEXT,
        PRIMARY KEY(date, game_pk, pid))""")
    conn.execute("""CREATE TABLE IF NOT EXISTS manager_tendency(
        date TEXT, team_id INTEGER, subs INTEGER, games INTEGER, rate REAL, tier TEXT,
        PRIMARY KEY(date, team_id))""")
    conn.execute("""CREATE TABLE IF NOT EXISTS pinch_hit_history(
        date TEXT, window_days INTEGER, name TEXT, team TEXT, team_id INTEGER, count INTEGER,
        PRIMARY KEY(date, window_days, name, team_id))""")
    return conn

def db_snapshot_pinch_history(date_str, window_days, rows):
    if not rows:
        return
    try:
        conn = _db()
        for r in rows:
            conn.execute(
                """INSERT OR REPLACE INTO pinch_hit_history(date,window_days,name,team,team_id,count)
                   VALUES(?,?,?,?,?,?)""",
                (date_str, window_days, r["name"], r.get("team"), r.get("team_id"), r["count"]))
        conn.commit(); conn.close()
    except Exception as e:
        print(f"[db] pinch-history snapshot error: {e}")

def db_record_picks(date_str, cands):
    if not cands:
        return
    try:
        conn = _db()
        now = datetime.now(timezone.utc).isoformat()
        for c in cands:
            conn.execute(
                """INSERT OR IGNORE INTO picks
                   (date,game_pk,pid,name,team,opp,sp_name,sp_hand,score,scenario,
                    bats,batting_order,reasons,posted_at)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (date_str, c.get("gamePk"), c.get("pid"), c.get("name"), c.get("team"),
                 c.get("opp"), c.get("sp_name"), c.get("sp_hand"), c.get("score"),
                 c.get("scenario"), c.get("bats"), c.get("order"),
                 json.dumps(c.get("reasons", [])), now))
        conn.commit(); conn.close()
    except Exception as e:
        print(f"[db] record picks error: {e}")

def db_record_outcome(date_str, game_pk, pid, a, hit, note):
    try:
        conn = _db()
        hrr = (a.get("h", 0) + a.get("r", 0) + a.get("rbi", 0))
        conn.execute(
            """UPDATE picks SET graded=1,pa=?,ab=?,h=?,r=?,rbi=?,hrr=?,pulled=?,hit=?,outcome_note=?
               WHERE date=? AND game_pk=? AND pid=?""",
            (a.get("pa"), a.get("ab"), a.get("h"), a.get("r"), a.get("rbi"), hrr,
             1 if a.get("pulled") else 0, 1 if hit else 0, note, date_str, game_pk, pid))
        conn.commit(); conn.close()
    except Exception as e:
        print(f"[db] outcome error: {e}")

def db_snapshot_manager(date_str, mgr):
    if not mgr:
        return
    try:
        conn = _db()
        for tid, m in mgr.items():
            conn.execute(
                """INSERT OR REPLACE INTO manager_tendency(date,team_id,subs,games,rate,tier)
                   VALUES(?,?,?,?,?,?)""",
                (date_str, int(tid), m.get("subs"), m.get("games"), m.get("rate"), m.get("tier")))
        conn.commit(); conn.close()
    except Exception as e:
        print(f"[db] manager snapshot error: {e}")

def db_stats():
    """Print hit-rate breakdowns from the results DB (the payoff of storing it)."""
    try:
        conn = _db(); cur = conn.cursor()
        cur.execute("SELECT COUNT(*), COALESCE(SUM(hit),0) FROM picks WHERE graded=1")
        tot, hits = cur.fetchone()
        if not tot:
            print("No graded picks in the database yet."); conn.close(); return
        print(f"OVERALL: {hits}/{tot} hit ({hits/tot*100:.0f}%)  [hit = pinch-hit for or ≤3 PA]\n")
        print("By scenario:")
        for s, n, h in cur.execute(
                "SELECT scenario,COUNT(*),COALESCE(SUM(hit),0) FROM picks WHERE graded=1 GROUP BY scenario"):
            print(f"  {s:<14} {h}/{n} ({h/n*100:.0f}%)")
        print("\nBy confidence band:")
        for lo, hi in ((80, 100), (70, 79), (60, 69), (55, 59)):
            cur.execute("SELECT COUNT(*),COALESCE(SUM(hit),0) FROM picks WHERE graded=1 AND score BETWEEN ? AND ?", (lo, hi))
            n, h = cur.fetchone()
            if n:
                print(f"  {lo}-{hi}:  {h}/{n} ({h/n*100:.0f}%)")
        conn.close()
    except Exception as e:
        print(f"[db] stats error: {e}")

def record_predictions(date_str, cands):
    """Persist the picks the board posted, so the end-of-day job can grade them."""
    db_record_picks(date_str, cands)   # full detail (reasons + prediction) → SQLite
    if not cands:
        return
    d = _load(PREDICTIONS_PATH)
    cutoff = (datetime.now(ET_TZ).date() - timedelta(days=30)).strftime("%Y-%m-%d")
    d = {k: v for k, v in d.items() if k >= cutoff}
    lst = d.setdefault(date_str, [])
    seen = {(p.get("gamePk"), p.get("pid")) for p in lst}
    for c in cands:
        k = (c.get("gamePk"), c.get("pid"))
        if k in seen or None in k:
            continue
        lst.append({"gamePk": c.get("gamePk"), "pid": c.get("pid"), "name": c["name"],
                    "team": c["team"], "score": c["score"], "scenario": c.get("scenario"),
                    "order": c["order"]})
        seen.add(k)
    _save(PREDICTIONS_PATH, d)

_SUFFIX_RE = re.compile(r'\b(jr\.?|sr\.?|ii|iii|iv)\s*$', re.IGNORECASE)

def _lastname(name):
    n = _SUFFIX_RE.sub('', (name or '')).strip().lower()
    return n.split()[-1] if n else ''

def fetch_actuals(game_pk):
    """From a final game: {pid: {pa,ab,h,r,rbi,pulled,name}}. `pulled` = the player
    was replaced by a pinch-hitter (detected from play-by-play)."""
    out = {}
    try:
        live = _get(f"{API11}/game/{game_pk}/feed/live", timeout=20)
    except Exception as e:
        print(f"[results] game {game_pk} error: {e}")
        return out
    replaced_last = set()
    for play in live.get("liveData", {}).get("plays", {}).get("allPlays", []):
        for ev in play.get("playEvents", []):
            desc = (ev.get("details", {}).get("description") or "").lower()
            if "pinch-hitter" in desc and "replaces" in desc:
                m = re.search(r"replaces\s+(.+?)[.\n]", desc, re.IGNORECASE)
                if m:
                    replaced_last.add(_lastname(m.group(1)))
    box = live.get("liveData", {}).get("boxscore", {}).get("teams", {})
    for side in ("home", "away"):
        for _, pdata in box.get(side, {}).get("players", {}).items():
            pid = pdata.get("person", {}).get("id")
            nm  = pdata.get("person", {}).get("fullName", "")
            bat = pdata.get("stats", {}).get("batting", {})
            if not bat or bat.get("plateAppearances") is None:
                continue
            last = _lastname(nm)
            out[pid] = {"pa": bat.get("plateAppearances", 0), "ab": bat.get("atBats", 0),
                        "h": bat.get("hits", 0), "r": bat.get("runs", 0),
                        "rbi": bat.get("rbi", 0), "name": nm,
                        "pulled": last in replaced_last}
    return out

def grade_day(date_str, to_discord=True):
    preds = _load(PREDICTIONS_PATH).get(date_str, [])
    if not preds:
        # Mark the day done so the scheduler doesn't re-check (and re-post) every poll.
        # Don't spam Discord with an empty card — just log it.
        acc = _load(ACCURACY_PATH)
        if not any(d.get("date") == date_str for d in acc.get("days", [])):
            acc.setdefault("days", []).append({"date": date_str, "total": 0, "hits": 0})
            _save(ACCURACY_PATH, acc)
        print(f"📊 Results — {date_str}: no predictions recorded (nothing to grade).")
        return

    by_game = {}
    for p in preds:
        by_game.setdefault(p["gamePk"], []).append(p)

    lines, total, hits = [], 0, 0
    tiers = {"80+": [0, 0], "60-79": [0, 0], "45-59": [0, 0]}
    for pk, ps in by_game.items():
        acts = fetch_actuals(pk)
        for p in sorted(ps, key=lambda x: x["score"], reverse=True):
            a = acts.get(p["pid"])
            if not a:
                lines.append(f"⚪ {p['name']} ({p['team']}, {p['score']}) — scratched / no data")
                continue
            hrr = a["h"] + a["r"] + a["rbi"]
            limited = a["pulled"] or a["pa"] <= 3
            total += 1
            hits += 1 if limited else 0
            band = "80+" if p["score"] >= 80 else "60-79" if p["score"] >= 60 else "45-59"
            tiers[band][1] += 1
            tiers[band][0] += 1 if limited else 0
            v = "✅" if limited else "❌"
            tag = "pinch-hit for, " if a["pulled"] else ""
            note = f"{tag}{a['pa']} PA {a['h']}H {a['r']}R {a['rbi']}RBI (H+R+RBI={hrr})"
            db_record_outcome(date_str, p["gamePk"], p["pid"], a, limited, note)  # → SQLite
            lines.append(f"{v} **{p['name']}** ({p['team']}, {p['score']}) — {tag}{a['pa']} PA · "
                         f"{a['h']}H {a['r']}R {a['rbi']}RBI (H+R+RBI={hrr})")

    rate = (hits / total * 100) if total else 0
    acc = _load(ACCURACY_PATH)
    days = [d for d in acc.get("days", []) if d.get("date") != date_str]
    days.append({"date": date_str, "total": total, "hits": hits})
    acc["days"] = days
    _save(ACCURACY_PATH, acc)
    ctot = sum(d["total"] for d in days)
    chits = sum(d["hits"] for d in days)
    crate = (chits / ctot * 100) if ctot else 0

    tier_str = " · ".join(f"{b} {t[0]}/{t[1]}" for b, t in tiers.items() if t[1])
    header = (f"**{hits}/{total} hit ({rate:.0f}%)** — a 'hit' = pinch-hit for or ≤3 PA "
              f"(under thesis worked).\nBy score tier: {tier_str}\n"
              f"All-time: **{chits}/{ctot} ({crate:.0f}%)**")
    body = header + "\n\n" + "\n".join(lines)
    color = 0x2ECC71 if rate >= 55 else 0xE67E22 if rate >= 40 else 0xE74C3C
    if to_discord:
        _post_embed({"title": f"📊 Results — {date_str}", "description": body[:4000],
                     "color": color, "footer": {"text": "Pregame Pull-Risk — daily grading"}})
    else:
        print(body)

# ── main ──────────────────────────────────────────────────────────────────────
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--date", default=datetime.now(ET_TZ).strftime("%Y-%m-%d"))
    ap.add_argument("--print", dest="print_only", action="store_true",
                    help="compute + print the whole-slate board to console (no Discord)")
    ap.add_argument("--serve", action="store_true",
                    help="run the per-game scheduler: fires ~LEAD_MINUTES before each game "
                         "once its lineup posts, one Discord embed per game")
    ap.add_argument("--games", type=int, default=None, help="limit to first N games (quick test)")
    ap.add_argument("--results", nargs="?", const="__today__", default=None,
                    metavar="DATE", help="grade a day's posted picks vs actual results "
                                         "(default: yesterday). Posts to Discord.")
    ap.add_argument("--stats", action="store_true",
                    help="print hit-rate breakdowns from the results DB (by scenario, confidence band)")
    ap.add_argument("--trend", nargs="?", const="__today__", default=None, metavar="DATE",
                    help="leaderboard of players most pinch-hit for over the last PINCH_HIST_DAYS; "
                         "posts to Discord (default date: today)")
    args = ap.parse_args()

    if args.stats:
        db_stats()
        return

    if args.trend is not None:
        date = args.trend if args.trend != "__today__" else datetime.now(ET_TZ).strftime("%Y-%m-%d")
        trend_board(date, to_discord=not args.print_only)
        return

    if args.results is not None:
        date = args.results
        if date == "__today__":
            date = (datetime.now(ET_TZ).date() - timedelta(days=1)).strftime("%Y-%m-%d")
        grade_day(date, to_discord=not args.print_only)
        return

    if args.serve:
        run_scheduler()
        return

    board = build_board(args.date, limit=args.games)
    content = render_board(args.date, board)
    if args.print_only:
        print("\n" + content)
    else:
        post_discord(content)
        record_predictions(args.date, board)

if __name__ == "__main__":
    main()
