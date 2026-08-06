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
POLL_MINUTES          = int(os.environ.get("POLL_MINUTES", "5"))     # serve mode: how often the scheduler checks for new lineups
GAME_TOP_N            = int(os.environ.get("GAME_TOP_N", "5"))       # max picks per per-game embed
POST_MIN_SCORE        = int(os.environ.get("POST_MIN_SCORE", "60"))  # serve mode: only ping a game if its top pick >= this
RESCAN_ON_BOOT        = os.environ.get("RESCAN_ON_BOOT", "").strip().lower() in ("1", "true", "yes", "on")
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
PITCHER_XW_CACHE_PATH = os.environ.get("PITCHER_XW_CACHE_PATH", _p("pregame_pitcher_xwoba.json"))

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

# ── Pitcher quality — xwOBA-ALLOWED (Savant) → starter & bullpen "grade" ──────────
# Powers the batter-vs-pitcher mismatch (starter's grade vs the batter's xwOBA) and
# league bullpen quality (averaged over the available pen). Lower = tougher pitcher.
_pitcher_xw = None
def _load_pitcher_xwoba():
    global _pitcher_xw
    if _pitcher_xw is not None:
        return _pitcher_xw
    today = datetime.now(ET_TZ).strftime("%Y-%m-%d")
    disk = _load(PITCHER_XW_CACHE_PATH)
    if disk.get("date") == today and disk.get("data"):
        _pitcher_xw = {int(k): v for k, v in disk["data"].items()}
        return _pitcher_xw
    data = {}
    try:
        r = _session.get("https://baseballsavant.mlb.com/leaderboard/expected_statistics",
                         params={"type": "pitcher", "year": SEASON, "min": "1", "csv": "true"},
                         headers={"User-Agent": "Mozilla/5.0 (pinch-hit-model)"}, timeout=30)
        r.raise_for_status()
        for row in csv.DictReader(io.StringIO(r.content.decode("utf-8-sig"))):
            try:
                pid = int(row.get("player_id"))
                xw = _to_float(row.get("est_woba"))
                if pid and xw is not None:
                    data[pid] = xw
            except Exception:
                continue
        print(f"[pitcher-xw] loaded xwOBA-allowed for {len(data)} pitchers")
    except Exception as e:
        print(f"[pitcher-xw] load error: {e}")
    _pitcher_xw = data
    _save(PITCHER_XW_CACHE_PATH, {"date": today, "data": {str(k): v for k, v in data.items()}})
    return _pitcher_xw

def pitcher_xwoba(pid):
    if not pid:
        return None
    return _load_pitcher_xwoba().get(int(pid))

# ── Starter length (#1) — avg innings per start = how early the bullpen enters ────
def _parse_ip(s):
    """MLB innings are 'X.Y' where Y is OUTS (thirds), not decimal — '4.2' = 4⅔."""
    try:
        w, _, f = str(s).partition(".")
        return int(w) + (int(f or 0)) / 3.0
    except Exception:
        return None

def starter_length(pid):
    """Avg IP over the pitcher's actual STARTS this season, from the game log. Must
    exclude relief outings — a swingman's total IP / starts hugely overstates length
    (this bug once showed a 4.4-IP starter as 8.2). None if too few starts. Cached daily."""
    if not pid:
        return None
    key = "len_v2_" + str(pid)     # v2: bust stale cache from the old total-IP/GS bug
    today = datetime.now(ET_TZ).strftime("%Y-%m-%d")
    c = _player_cache.get(key)
    if isinstance(c, dict) and c.get("_date") == today:
        return c.get("ip_gs")
    ip_gs = None
    try:
        d = _get(f"{API}/people/{pid}/stats", params={"stats": "gameLog", "group": "pitching", "season": SEASON})
        starts = []
        for s in d.get("stats", []):
            for spl in s.get("splits", []):
                st = spl.get("stat", {})
                if int(st.get("gamesStarted") or 0) == 1:      # only games he started
                    ip = _parse_ip(st.get("inningsPitched"))
                    if ip is not None:
                        starts.append(ip)
        if len(starts) >= 3:                                   # need a few starts to be reliable
            ip_gs = round(sum(starts) / len(starts), 2)
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
def _ph_pos(players_box, pid):
    """A player's FIELDING position from a boxscore players dict (the last real position he
    played); falls back to 'PH' when he only pinch-hit and never took the field (a last-chance
    at-bat). `players_box` is boxscore.teams[side].players."""
    pl = players_box.get(f"ID{pid}", {})
    fld = [p.get("abbreviation") for p in pl.get("allPositions", [])
           if p.get("abbreviation") not in (None, "PH", "PR")]
    if fld:
        return fld[-1]
    return pl.get("position", {}).get("abbreviation") or "?"

def _scan_recent_subs(date_str, window_days):
    """Single pass over final games in the last `window_days` (ending the day before
    date_str). Returns {"teams": {tid: {subs,games}}, "players": {"tid|last": {...}}}.
    Cached per (date, window) so the day's first run pays the cost once. This is the
    shared scanner behind manager_tendency() and pinch_hit_history()."""
    if window_days <= 0:
        return {"teams": {}, "players": {}, "pitchers": {}, "pen_usage": {}, "ph_events": {}, "team_dates": {}}
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
        return {"teams": {}, "players": {}, "pitchers": {}, "pen_usage": {}, "ph_events": {}, "team_dates": {}}

    teams = {}     # tid -> {"subs":int, "games":set}
    players = {}   # "tid|last" -> {"name","team","team_id","count"}
    pitchers = {}  # pid_str -> {date: pitch_count}  (#2 reliever-rest)
    pen_usage = {} # pid_str -> {"team":tid, "apps":{date:{pitches,inning,was_last}}}  (bullpen projection)
    ph_events  = {} # tid_str -> [{date,out_pos,in_pos,out_hand,in_hand,out_name,in_name}]  (recent PH profile)
    team_dates = {} # tid_str -> set of game-dates (to slice a coach's last N games)
    for pk in game_pks:
        try:
            live = _get(f"{API11}/game/{pk}/feed/live", timeout=20)
            box = live.get("liveData", {}).get("boxscore", {}).get("teams", {})
            gdate = pk_date.get(pk)
            side_team, side_abbr = {}, {}
            entry_inn = {}   # pid_str -> first inning he appears THIS game (from play-by-play below)
            for side in ("home", "away"):
                t = box.get(side, {}).get("team", {})
                side_team[side] = t.get("id")
                side_abbr[side] = t.get("abbreviation") or t.get("triCode") or str(t.get("id"))
                if t.get("id") is not None:
                    teams.setdefault(t["id"], {"subs": 0, "platoon": 0, "games": set()})["games"].add(pk)
                    if gdate:
                        team_dates.setdefault(str(t["id"]), set()).add(gdate)
                plist = box.get(side, {}).get("pitchers", [])           # in appearance order
                for ppid in plist:                                      # who pitched + pitch load
                    if not gdate:
                        continue
                    pd = box.get(side, {}).get("players", {}).get(f"ID{ppid}", {})
                    pc = pd.get("stats", {}).get("pitching", {})
                    n = int(pc.get("numberOfPitches") or pc.get("pitchesThrown") or 0)
                    day = pitchers.setdefault(str(ppid), {})
                    day[gdate] = day.get(gdate, 0) + n
                    # bullpen projection: team, pitch load, and who FINISHED the game (closer tell)
                    pu = pen_usage.setdefault(str(ppid), {"team": side_team[side], "apps": {}})
                    pu["team"] = side_team[side]
                    pu["apps"][gdate] = {"pitches": n, "inning": None,
                                         "was_last": bool(plist) and ppid == plist[-1]}
            for play in live.get("liveData", {}).get("plays", {}).get("allPlays", []):
                half = play.get("about", {}).get("halfInning")
                bside = "away" if half == "top" else "home"
                bt = side_team.get(bside)
                # entry inning: first inning each pitcher appears (plays are chronological)
                onpid = play.get("matchup", {}).get("pitcher", {}).get("id")
                oinn  = play.get("about", {}).get("inning")
                if onpid and oinn and str(onpid) not in entry_inn:
                    entry_inn[str(onpid)] = oinn
                for ev in play.get("playEvents", []):
                    det = ev.get("details", {})
                    desc = det.get("description") or ""
                    is_sub = ev.get("isSubstitution", False) or det.get("event") == "Offensive Substitution"
                    if is_sub and "pinch-hitter" in desc.lower() and bt is not None:
                        teams[bt]["subs"] += 1
                        # PLATOON move? compare handedness of the incoming PH vs the man out.
                        in_id  = ev.get("player", {}).get("id")
                        out_id = ev.get("replacedPlayer", {}).get("id")
                        ih = batter_hand(in_id) if in_id else None
                        oh = batter_hand(out_id) if out_id else None
                        if ih in ("L", "R", "S") and oh in ("L", "R", "S") and ih != oh:
                            teams[bt]["platoon"] = teams[bt].get("platoon", 0) + 1
                        pbox = box.get(bside, {}).get("players", {})
                        out_pos = _ph_pos(pbox, out_id) if out_id else "?"   # position pulled
                        in_pos  = _ph_pos(pbox, in_id) if in_id else "?"     # what the sub plays
                        mo = re.search(r"pinch-hitter\s+(.+?)\s+replaces\s+(.+?)[.\n]", desc, re.IGNORECASE)
                        in_name  = mo.group(1).strip() if mo else None
                        out_name = mo.group(2).strip() if mo else None
                        if gdate:                            # per-event PH profile (recent-N + swaps)
                            ph_events.setdefault(str(bt), []).append(
                                {"date": gdate, "out_pos": out_pos, "in_pos": in_pos,
                                 "out_hand": oh, "in_hand": ih, "out_name": out_name, "in_name": in_name})
                        if out_name:
                            key = f"{bt}|{_lastname(out_name)}"
                            e = players.setdefault(key, {"name": out_name, "team": side_abbr.get(bside),
                                                         "team_id": bt, "count": 0})
                            e["count"] += 1
            if gdate:                                   # stamp entry innings onto this game's apps
                for pid_s, inn in entry_inn.items():
                    ap = pen_usage.get(pid_s, {}).get("apps", {}).get(gdate)
                    if ap is not None:
                        ap["inning"] = inn
        except Exception as e:
            print(f"[scan] game {pk} error: {e}")

    result = {"teams": {str(tid): {"subs": t["subs"], "platoon": t.get("platoon", 0),
                                   "games": len(t["games"])}
                        for tid, t in teams.items()},
              "players": players,
              "pitchers": pitchers,    # {pid: {date: pitch_count}} for load-based availability
              "pen_usage": pen_usage,  # {pid: {team, apps:{date:{pitches,inning,was_last}}}} for projection
              "ph_events": ph_events,  # {tid: [PH events w/ positions+hands]} for recent-N coach profile
              "team_dates": {tid: sorted(ds) for tid, ds in team_dates.items()}}
    cutoff = (datetime.now(ET_TZ).date() - timedelta(days=3)).strftime("%Y-%m-%d")
    cache = {k: v for k, v in cache.items() if k.split(":")[0] >= cutoff}
    cache[ck] = result
    _save(SUBS_CACHE_PATH, cache)
    return result

def unavailable_relievers(date_str):
    """Relievers likely DOWN today, from recent pitch LOAD (MLB boxscores) — the same
    idea as a bullpen-usage grid. A pitcher is flagged when he: threw 3+ straight days,
    OR threw a heavy single outing yesterday (>=30 pitches), OR piled up a heavy last-3
    load (>=45). A normal light back-to-back stays available. Returns set of pitcher ids."""
    pit = _scan_recent_subs(date_str, PINCH_HIST_DAYS).get("pitchers", {})
    # normalize: tolerate the old list-of-dates format
    norm = {}
    for pid, usage in pit.items():
        norm[pid] = usage if isinstance(usage, dict) else {d: 0 for d in usage}
    alldates = set()
    for u in norm.values():
        alldates.update(u)
    if not alldates:
        return set()
    latest = max(alldates)
    base = datetime.strptime(latest, "%Y-%m-%d").date()
    last3days = {(base - timedelta(days=i)).strftime("%Y-%m-%d") for i in range(3)}
    out = set()
    for pid, u in norm.items():
        if latest not in u:                    # didn't pitch the most recent day → rested
            continue
        streak = 1
        while (base - timedelta(days=streak)).strftime("%Y-%m-%d") in u:
            streak += 1
        yday_pitches = u.get(latest, 0) or 0
        last3 = sum(v or 0 for d, v in u.items() if d in last3days)
        if streak >= 3 or yday_pitches >= 30 or last3 >= 45:
            out.add(int(pid))
    return out

def manager_tendency(date_str):
    """{team_id: {subs,platoon,games,rate,tier,platoon_rate,platoon_tier}} over
    MANAGER_LOOKBACK_DAYS. Tiers are relative to the league distribution. `platoon_*` is
    the PLATOON-specific pinch-hit rate (opposite-handed swaps only) — the tier the model
    uses, since every pick here is a platoon spot. Derived from the shared scan."""
    teams = _scan_recent_subs(date_str, MANAGER_LOOKBACK_DAYS).get("teams", {})
    if not teams:
        return {}
    result, rates, prates = {}, [], []
    for tid, t in teams.items():
        g = max(1, t["games"])
        rate = t["subs"] / g
        prate = t.get("platoon", 0) / g
        result[tid] = {"subs": t["subs"], "platoon": t.get("platoon", 0), "games": g,
                       "rate": round(rate, 2), "platoon_rate": round(prate, 2)}
        rates.append(rate); prates.append(prate)
    def tier_of(val, dist):
        d = sorted(dist)
        if not d:
            return "med"
        hi = d[min(len(d) - 1, int(0.66 * len(d)))]
        lo = d[min(len(d) - 1, int(0.33 * len(d)))]
        return "high" if val >= hi else ("low" if val <= lo else "med")
    for tid, r in result.items():
        r["tier"] = tier_of(r["rate"], rates)                 # overall (kept for context)
        r["platoon_tier"] = tier_of(r["platoon_rate"], prates)  # what the model scores on
    return result

def manager_recent_ph(date_str, team_id, games=5):
    """A coach's RECENT pinch-hit profile over his last `games` games — far more current than
    the 14-day team rate at the deadline/call-up churn, when a specific player's pull tendency
    swings. Returns {games, n_ph, n_platoon, swaps, events}: `n_ph`/`n_platoon` = pinch-hit
    (and opposite-handed platoon) count in the window; `swaps` = the position moves he made,
    e.g. ['LF→CF', 'C→DH'] (out position → what the sub plays); `events` = the raw list."""
    scan = _scan_recent_subs(date_str, MANAGER_LOOKBACK_DAYS)
    tid = str(team_id)
    dates = scan.get("team_dates", {}).get(tid, [])
    window = set(sorted(dates, reverse=True)[:games])          # this team's most recent N game-dates
    evs = [e for e in scan.get("ph_events", {}).get(tid, []) if e.get("date") in window]
    n_platoon = sum(1 for e in evs
                    if e.get("out_hand") in ("L", "R", "S") and e.get("in_hand") in ("L", "R", "S")
                    and e["out_hand"] != e["in_hand"])
    swaps = [f"{e.get('out_pos','?')}→{e.get('in_pos','?')}" for e in evs]
    return {"games": len(window), "n_ph": len(evs), "n_platoon": n_platoon,
            "swaps": swaps, "events": evs}

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

def score_starter(prof, sp_hand, sp_name, order, mgr_tier, bench_res, scenario,
                  pen_mix=None, bvp=None, player_ph_count=0, sp_len=None, arsenal=None,
                  sp_xw=None, starter_pos=None, pen_pred=None,
                  recent_pulls=0, recent_pos_pulls=0):
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
    flip_share = None       # set in the flip pen block; gates the end-of-function dampener
    dis_share  = None       # disadvantage: usage-wtd share of pen that KEEPS him weak (sp_hand)

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
        # Does the weak matchup PERSIST after the starter? Prefer the projected bullpen: the
        # usage-weighted share of the arms likely to pitch that throw his WEAK hand (== sp_hand).
        # High share → he keeps facing his weak side → pull-risk holds/boosts. LOW share → a
        # FAVORABLE reliever is likely → the starter leaves and he gets bailed out (rarely pulled,
        # better late ABs) → the end-of-function dampener cuts the score. Fall back to flat count.
        wshare, wseq, nweak, ntot = pen_weak_share(pen_pred, sp_hand)
        if wshare is not None:
            dis_share = wshare
            src = f"Proj bullpen {nweak}/{ntot} {sp_hand}HP, usage-wtd {wshare:.0%} ({wseq})"
        elif pen_mix and pen_mix.get("total", 0) >= 4:
            same = pen_mix.get(sp_hand, 0); tot = pen_mix["total"]; dis_share = same / tot
            src = f"Opposing pen {same}/{tot} {sp_hand}HP ({dis_share:.0%})"
        else:
            src = None
        if src is not None:
            if dis_share >= 0.60:
                score += min(12.0, (dis_share - 0.50) / 0.40 * 12.0)
                reasons.append(f"{src} — weak matchup persists after the starter")
            elif dis_share >= 0.45:
                reasons.append(f"{src} — some relief possible later")
            # dis_share < 0.45 → favorable reliever likely; the end-of-function dampener cuts the
            # whole score (and prints the reason) rather than a cosmetic note here.

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
        # Flip trigger strength. PREFER the arm-specific projected bullpen: the usage-weighted
        # share of the arms likely to pitch the 6th+ leverage innings that throw his OWN hand
        # (== bats) — those are the arms that neutralize his edge and cue the pinch-hit. Fall
        # back to the flat available-pen L/R count only when we can't project (early season,
        # no recent usage). A gassed closer contributes ~0 to the weighted share automatically.
        pshare, pseq, nweak, ntot = pen_weak_share(pen_pred, bats)
        if pshare is not None:
            flip_share = pshare
            src = f"Proj bullpen {nweak}/{ntot} {bats}HP, usage-wtd {pshare:.0%} ({pseq})"
        elif pen_mix and pen_mix.get("total", 0) >= 4:
            fn = pen_mix.get(bats, 0); tot = pen_mix["total"]; flip_share = fn / tot
            src = f"Opposing pen {fn}/{tot} {bats}HP ({flip_share:.0%})"
        else:
            src = None
        if src is not None:
            if flip_share >= 0.55:
                score += min(14.0, (flip_share - 0.45) / 0.45 * 14.0)
                reasons.append(f"{src} — matchup likely FLIPS to his weak side → pinch-hit trigger")
            elif flip_share >= 0.40:
                reasons.append(f"{src} — flip uncertain")
            # flip_share < 0.40 → the flip rarely triggers; the end-of-function dampener cuts
            # the whole score (and prints the reason) rather than a cosmetic note here.

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
            if recent_pulls >= 2:                # coach is pulling him ANYWAY — don't "ride the hot bat"
                reasons.append(f"Hot last {RECENCY_DAYS}d: {_fmt(r_ops)} OPS over {r_pa} PA — but the coach is pulling him regardless (not ridden)")
            else:
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
        reasons.append(f"({pen_mix['rested_out']} opp reliever(s) likely down — heavy recent pitch load)")

    # Starter length (#1): short-leash/opener → bullpen enters early → pull happens
    # sooner; workhorse → starter stays in → flip is less likely.
    if sp_len is not None and scenario in ("disadvantage", "flip"):
        if sp_len < 4.6:                       # short/opener (league avg ~5.2) → early bullpen
            score += 6
            reasons.append(f"Opp starter avgs {sp_len:.1f} IP/start — short outing, early bullpen, matchup flips sooner")
        elif sp_len > 6.0 and scenario == "flip":
            score *= 0.90
            reasons.append(f"Opp starter avgs {sp_len:.1f} IP/start — goes deep, flip less likely")

    # Pitch-arsenal matchup (#2): does the hitter handle this pitcher's mix? PRODUCTION
    # signal — applies to a weak-side under (he faces the tough arm most of the game).
    # SKIPPED for flip: the flip under is a VOLUME play (1-2 fewer ABs), his production
    # is a coin flip, and the arsenal we measure is vs the starter he was started to hit
    # WELL — so it's both irrelevant and misleading here.
    if scenario != "flip" and arsenal and arsenal.get("wrv") is not None:
        wrv = arsenal["wrv"]
        score += max(-8.0, min(8.0, -wrv * 6.0))   # negative wrv (struggles) → boosts under
        pt = _PT_NAMES.get(arsenal.get("top_pt"), arsenal.get("top_pt", ""))
        if wrv <= -0.4:
            reasons.append(f"Tough vs {sp_name}'s arsenal ({wrv:+.1f} RV/100; heavy {pt}) — struggles vs his mix")
        elif wrv >= 0.4:
            reasons.append(f"Handles {sp_name}'s arsenal well ({wrv:+.1f} RV/100) — favorable, less under value")

    # PRODUCTION: how tough is the pitching he'll face? (batter-vs-pitcher mismatch +
    # league bullpen quality). Strong pitching → he produces less → better under.
    # Skipped for flip (volume thesis; he faces his good matchup only briefly).
    if scenario == "disadvantage":
        if sp_xw is not None:                  # starter grade (xwOBA-allowed): the mismatch
            if sp_xw <= 0.295:
                score += 5
                reasons.append(f"Tough starter — {sp_name} allows just {_fmt(sp_xw)} xwOBA")
            elif sp_xw >= 0.345:
                score -= 5
                reasons.append(f"Hittable starter — {sp_name} allows {_fmt(sp_xw)} xwOBA (less under value)")
        pen_xw = pen_mix.get("pen_xwoba") if pen_mix else None
        if pen_xw is not None:                  # league bullpen quality (available arms)
            if pen_xw <= 0.305:
                score += 4
                reasons.append(f"Strong opposing pen ({_fmt(pen_xw)} xwOBA-allowed) — tough late at-bats too")
            elif pen_xw >= 0.335:
                score -= 4
                reasons.append(f"Weak opposing pen ({_fmt(pen_xw)} xwOBA-allowed) — easier late at-bats")

    # Statcast overall quality: weak bats are more pull-prone (applies to both scenarios).
    # The strong-bat dampener is SKIPPED for flip — a good platoon hitter still gets
    # flipped out; his quality doesn't keep him in when the handedness turns.
    xw = prof.get("xwoba")
    if xw is not None:
        if xw < 0.300:
            score += 6
            reasons.append(f"Weak overall bat ({_fmt(xw)} xwOBA, Statcast) — pull-prone")
        elif xw > 0.360 and scenario != "flip":
            score *= 0.90
            reasons.append(f"Strong overall bat ({_fmt(xw)} xwOBA, Statcast) — managers keep him")

    # Player's OWN recent pinch-hit-for history — has THIS guy been getting pulled?
    # 2× in the window is already a solid flag (not a half-strength one).
    if player_ph_count >= 2:
        score += 8.0 if player_ph_count == 2 else 10.0
        reasons.append(f"Pinch-hit for {player_ph_count}× in last {PINCH_HIST_DAYS}d — "
                       f"repeatedly pulled lately")
    elif player_ph_count == 1:
        reasons.append(f"Pinch-hit for once in last {PINCH_HIST_DAYS}d")

    # Bench upgrade — a finite lever. The manager has ONE of a given bench bat and can
    # pinch-hit for at most one starter with him, so the bonus is resolved by 1:1 matching
    # upstream (analyze_side). "full" = this starter is the best-fit target and owns the bat;
    # "contested" = another starter is the more likely target, so only residual risk remains.
    if bench_res:
        if bench_res["kind"] == "full":
            score += 18
            reasons.append(f"Bench upgrade available: {bench_res['note']}")
        else:  # contested
            score += 6
            reasons.append(f"Bench upgrade contested — {bench_res['note']}")

    add = {"high": 20, "med": 10, "low": 3}.get(mgr_tier, 10)
    score += add
    reasons.append(f"Manager platoon pinch-hit tendency: {mgr_tier.upper()}")

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

    # Positional realism: a starting CATCHER is rarely pinch-hit for before the 9th — it
    # burns the only backup and leaves the team a catcher short. Strong dampener.
    if starter_pos == "C":
        if recent_pulls >= 2:                    # coach demonstrably pulls THIS catcher — prior is wrong for him
            reasons.append(f"Catcher — but this coach has pinch-hit for him {recent_pulls}× in his last 5g "
                           f"(a defensive backup is clearly on the bench); catcher prior overridden")
        else:
            score *= 0.55
            reasons.append("Catcher — rarely pinch-hit for before the 9th (burns the backup); low early-pull risk")

    # Flip-likelihood gate. A flip pick's ENTIRE thesis is the opposing pen turning his
    # platoon edge to the weak side. How stacked the pen is toward his weak hand scales the
    # WHOLE score (manager tendency, order, platoon base — all of it presupposes the flip),
    # applied last so it moves every additive signal, not just the platoon points:
    #   • scarce weak-hand pen  → trigger rarely fires → heavy cut (a "High" makes no sense)
    #   • stacked weak-hand pen → trigger highly likely → premium ON TOP of the additive bonus
    # The top end is gentler than the bottom: a dead thesis kills the pick, but a confirmed
    # one only ENABLES the pull — the manager still has to make the move.
    if scenario == "flip" and flip_share is not None:
        if flip_share < 0.30:
            score *= 0.45
            reasons.append(f"Flip unlikely — only {flip_share:.0%} of the projected bullpen throws "
                           f"{bats}HP to turn his edge; the pinch-hit thesis rarely triggers (heavy dampen)")
        elif flip_share < 0.40:
            score *= 0.65
            reasons.append(f"Flip unlikely — only {flip_share:.0%} of the projected bullpen throws "
                           f"{bats}HP to turn his edge; low trigger odds (dampened)")
        elif flip_share >= 0.80:
            score *= 1.10
            reasons.append(f"Flip near-locked — {flip_share:.0%} of the projected bullpen throws "
                           f"{bats}HP; the weak-side trigger is highly likely (boosted)")
        elif flip_share >= 0.70:
            score *= 1.05
            reasons.append(f"Flip likely — {flip_share:.0%} of the projected bullpen throws "
                           f"{bats}HP; strong weak-side trigger odds (boosted)")

    # Disadvantage: the PULL leg needs his weak hand (== sp_hand) to keep coming. If the pen is
    # mostly his FAVORABLE hand, the starter leaves and a good matchup bails him out — he's
    # rarely pinch-hit and even hits better late. Dampen the whole score. GENTLER than flip: a
    # disadvantage pick keeps some PRODUCTION value (he still faces the weak-side STARTER early),
    # so this scales it down, doesn't gut it.
    if scenario == "disadvantage" and dis_share is not None:
        if dis_share < 0.30:
            score *= 0.60
            reasons.append(f"Favorable pen — only {dis_share:.0%} of the projected bullpen throws "
                           f"{sp_hand}HP; a favorable reliever bails him out, unlikely to be pulled (heavy dampen)")
        elif dis_share < 0.45:
            score *= 0.75
            reasons.append(f"Favorable pen — {dis_share:.0%} of the projected bullpen throws "
                           f"{sp_hand}HP; relief likely after the starter, lower pull odds (dampened)")
        elif dis_share < 0.60:
            score *= 0.90
            reasons.append(f"Pen only {dis_share:.0%} {sp_hand}HP — some relief likely, mild dampen")

    # Recent BEHAVIORAL evidence — the most current, most direct pull signal: this coach has
    # ACTUALLY been pinch-hitting for THIS player (or his exact position) in his last 5 games.
    # Applied LAST so it lifts past the platoon-math and positional priors it directly
    # contradicts, and cuts through late-season roster churn where the 14-day tendency is stale.
    if recent_pulls >= 2:
        score += 15
        reasons.append(f"Coach pinch-hit for HIM {recent_pulls}× in his last 5 games — active pull target (behavioral)")
    elif recent_pulls == 1:
        score += 8
        reasons.append("Coach pinch-hit for him once in his last 5 games — recent pull evidence")
    elif recent_pos_pulls >= 2:
        score += 6
        reasons.append(f"Coach has churned this position ({starter_pos}) {recent_pos_pulls}× in his last 5 games")

    return max(0, min(100, round(score))), reasons

def bench_upgrades(ref_ops, plan_hand, bench_players, starter_pos=None):
    """All bench bats OPPOSITE-handed to `plan_hand` whose OPS vs `plan_hand` clearly beats
    ref_ops — AND are positionally realistic replacements: a starting catcher can only be
    swapped for another catcher, and a bench catcher isn't burned to pinch-hit for a field
    player early. Returns a list of candidate dicts sorted best-first (highest OPS vs the
    plan hand). Each: {id, name, bats, ops, delta, note}. `delta` = how much better this bat
    is than the starter (the swap's value) — drives the 1:1 matching in analyze_side. Empty
    list if none qualify."""
    faced = "vl" if plan_hand == "L" else "vr"
    ref = ref_ops or 0.0
    out = []
    for bp in bench_players:
        bpos = bp.get("pos")
        if starter_pos == "C":
            if bpos != "C":               # only a catcher realistically replaces a catcher
                continue
        elif bpos == "C":                 # don't burn the backup catcher to PH for a field player
            continue
        name = bp.get("fullName") or bp.get("name")
        bprof = get_hitter_profile(bp["id"], name)
        b_bats = bprof.get("bats")
        if b_bats == plan_hand:           # want the opposite-handed platoon edge
            continue
        b_fo = bprof.get(faced, {}).get("ops")
        if b_fo is not None and b_fo >= ref + 0.060:
            out.append({
                "id": bp["id"],
                "name": bprof.get("name") or name,
                "bats": b_bats,
                "ops": b_fo,
                "delta": b_fo - ref,
                "note": f"{bprof.get('name') or name} ({b_bats}, {_fmt(b_fo)} OPS vs {plan_hand}HP)",
            })
    out.sort(key=lambda c: c["ops"], reverse=True)
    return out

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
    """The OFFICIAL lineup from the boxscore batting-order card — authoritative, unlike
    the schedule's predicted feed. Also returns the GAME-DAY bench (the real available
    subs for THIS game — reflects trades/call-ups, unlike the season roster). Each player
    carries their position. Returns {"away":{"starters":[{id,fullName,pos}],"bench":[...]},
    "home":{...}}; starters empty until posted."""
    out = {"away": {"starters": [], "bench": []}, "home": {"starters": [], "bench": []}}
    try:
        data = _get(f"{API}/game/{game_pk}/boxscore", timeout=20)
    except Exception:
        return out
    box = data.get("teams", {})
    for side in ("away", "home"):
        players = box.get(side, {}).get("players", {})
        def entry(pid):
            pd = players.get(f"ID{pid}", {})
            return {"id": pid, "fullName": pd.get("person", {}).get("fullName", ""),
                    "pos": pd.get("position", {}).get("abbreviation")}
        out[side]["starters"] = [entry(pid) for pid in box.get(side, {}).get("battingOrder", [])]
        out[side]["bench"]    = [entry(pid) for pid in box.get(side, {}).get("bench", [])]
    return out

def batter_hand(pid):
    """Cached bat side (L/R/S) — used to tag whether a pinch-hit was a platoon move."""
    key = "bh_" + str(pid)
    if key in _player_cache:
        return _player_cache[key]
    hand = None
    try:
        person = _get(f"{API}/people/{pid}").get("people", [{}])[0]
        hand = person.get("batSide", {}).get("code")
    except Exception:
        pass
    _player_cache[key] = hand
    return hand

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

def pitcher_role(pid):
    """'SP' if this pitcher is a rotation-only starter (won't come out of the pen),
    else 'PEN' (reliever or swingman who DOES relieve). Keeps swingmen in the bullpen.
    Cached daily."""
    key = "role_" + str(pid)
    today = datetime.now(ET_TZ).strftime("%Y-%m-%d")
    c = _player_cache.get(key)
    if isinstance(c, dict) and c.get("_date") == today:
        return c.get("role")
    role = "PEN"
    try:
        d = _get(f"{API}/people/{pid}/stats", params={"stats": "season", "group": "pitching", "season": SEASON})
        for s in d.get("stats", []):
            for spl in s.get("splits", []):
                st = spl.get("stat", {})
                gs = int(st.get("gamesStarted") or 0)
                g  = int(st.get("gamesPlayed") or 0)
                if g > 0 and gs >= 4 and gs / g > 0.65:   # nearly all appearances are starts → rotation
                    role = "SP"
    except Exception:
        pass
    _player_cache[key] = {"role": role, "_date": today}
    return role

def pitcher_pen_role(pid):
    """STABLE bullpen role from SEASON usage — 'CLOSER' / 'SETUP' / 'MIDDLE' — plus the raw
    saves / games-finished / holds. Season (not a 7-game sample) so the label doesn't swing
    on a blowout mop-up or one vulture save; recent availability is handled separately in
    predict_bullpen(). Cached daily. Returns a dict {role,sv,gf,hld,g}."""
    key = "penrole_" + str(pid)
    today = datetime.now(ET_TZ).strftime("%Y-%m-%d")
    c = _player_cache.get(key)
    if isinstance(c, dict) and c.get("_date") == today:
        return c
    sv = gf = hld = g = 0
    try:
        d = _get(f"{API}/people/{pid}/stats", params={"stats": "season", "group": "pitching", "season": SEASON})
        for s in d.get("stats", []):
            for spl in s.get("splits", []):
                st = spl.get("stat", {})
                sv  = int(st.get("saves") or 0);         gf  = int(st.get("gamesFinished") or 0)
                hld = int(st.get("holds") or 0);         g   = int(st.get("gamesPlayed") or 0)
    except Exception:
        pass
    # closer = clear save volume, or a games-finished profile with a handful of saves;
    # setup = a holds specialist; everyone else is middle/long relief.
    if sv >= 8 or (gf >= 15 and sv >= 5):
        role = "CLOSER"
    elif hld >= 8:
        role = "SETUP"
    else:
        role = "MIDDLE"
    out = {"role": role, "sv": sv, "gf": gf, "hld": hld, "g": g, "_date": today}
    _player_cache[key] = out
    return out

def opposing_bullpen_mix(team_id, exclude_sp_id, unavailable=None):
    """L/R counts of the opposing team's AVAILABLE bullpen. Excludes: today's probable
    starter, rotation-only starters who won't relieve (keeps swingmen), and back-to-back
    arms likely down today. The result reflects who can actually pitch in relief."""
    unavailable = unavailable or set()
    mix = {"L": 0, "R": 0, "total": 0, "rested_out": 0, "rotation_out": 0, "pen_xwoba": None}
    try:
        data = _get(f"{API}/teams/{team_id}/roster", params={"rosterType": "active"})
    except Exception as e:
        print(f"[bullpen] team {team_id} error: {e}")
        return mix
    xw_sum, xw_n = 0.0, 0
    for p in data.get("roster", []):
        if p.get("position", {}).get("type") != "Pitcher":
            continue
        ppid = p["person"]["id"]
        if ppid == exclude_sp_id:
            continue
        if pitcher_role(ppid) == "SP":        # rotation starter, not pitching today → not the pen
            mix["rotation_out"] += 1
            continue
        if ppid in unavailable:               # heavy recent load — likely unavailable today
            mix["rested_out"] += 1
            continue
        h = pitcher_hand(ppid)
        if h in ("L", "R"):
            mix[h] += 1
            mix["total"] += 1
            xw = pitcher_xwoba(ppid)           # bullpen QUALITY: avg xwOBA-allowed of available arms
            if xw is not None:
                xw_sum += xw; xw_n += 1
    if xw_n:
        mix["pen_xwoba"] = round(xw_sum / xw_n, 3)
    return mix

def _median(xs):
    xs = sorted(x for x in xs if x is not None)
    if not xs:
        return None
    n = len(xs); mid = n // 2
    return xs[mid] if n % 2 else (xs[mid - 1] + xs[mid]) / 2.0

def predict_bullpen(team_id, date_str, exclude_sp_id=None, n_games=7, top=4, unavailable=None):
    """Project the arms most likely to pitch for `team_id` today — each with a ROLE, rest
    state, and a usage-weighted likelihood — from the team's last `n_games` games. Built
    entirely from the shared scan's pen_usage (pitch load + entry inning + who finished the
    game) plus roster/hand; no extra API calls beyond the roster read.

    Returns {"likely":[3-4 arms most likely to pitch, by P(pitch)], "closers":[CLOSER-role
             arms, surfaced even if gassed], "all":[full ranked list], "n_games":k}. Each arm:
      id,name,hand, role(CLOSER|SETUP|MIDDLE), apps, gf(games finished), last_date,
      days_rest, last_pitches, gassed(bool), med_inning, usage(0-1 ~ P today), proj_inning.

    We list WHO is likely to pitch (+ the closer) but do NOT predict the middle-relief ORDER —
    inning-to-inning sequencing is too noisy to stand behind. Two-category logic (per design):
      • CLOSERS are their own bucket — a stable season role (saves/GF), graded by prominence,
        zeroed out when gassed/back-to-back-heavy.
      • SETUP/MIDDLE are down-weighted vs a flat headcount and gated on recent pitch load —
        did the coach keep going back to this arm, and is he rested enough to use again?"""
    scan = _scan_recent_subs(date_str, PINCH_HIST_DAYS)
    pen_usage = scan.get("pen_usage", {})
    if unavailable is None:
        unavailable = unavailable_relievers(date_str)
    today = datetime.strptime(date_str, "%Y-%m-%d").date()

    # this team's most recent n_games game-dates (any of its arms appeared)
    team_dates = sorted({d for pu in pen_usage.values() if pu.get("team") == team_id
                         for d in pu.get("apps", {})}, reverse=True)[:n_games]
    window = set(team_dates)
    denom = len(team_dates) or n_games

    try:
        roster = _get(f"{API}/teams/{team_id}/roster",
                      params={"rosterType": "active"}).get("roster", [])
    except Exception as e:
        print(f"[pen-predict] team {team_id} error: {e}")
        return {"likely": [], "order": [], "all": [], "n_games": len(team_dates)}

    arms = []
    for p in roster:
        if p.get("position", {}).get("type") != "Pitcher":
            continue
        pid = p["person"]["id"]; name = p["person"]["fullName"]
        if pid == exclude_sp_id or pitcher_role(pid) == "SP":
            continue
        apps = {d: a for d, a in pen_usage.get(str(pid), {}).get("apps", {}).items() if d in window}
        # a "reliever" throwing 55+ in an outing is really a starter (e.g. a fresh call-up
        # pitcher_role misses on low games) — keep him out of the pen projection
        if apps and max(a.get("pitches", 0) for a in apps.values()) >= 55:
            continue
        napp = len(apps)
        gf = sum(1 for a in apps.values() if a.get("was_last"))   # recent games finished (display)
        med_inn = _median([a.get("inning") for a in apps.values() if a.get("inning")])
        dates = sorted(apps.keys(), reverse=True)
        last_date = dates[0] if dates else None
        days_rest = (today - datetime.strptime(last_date, "%Y-%m-%d").date()).days if last_date else None
        last_pitches = apps.get(last_date, {}).get("pitches") if last_date else None
        gassed = pid in unavailable

        pr = pitcher_pen_role(pid)                    # STABLE role from season SV/GF/HLD
        role = pr["role"]

        # likelihood he pitches today: blend a ROLE PRIOR (closers/setup pitch ~half of games
        # by nature, so raw frequency alone buries them) with recent appearance rate, then cut
        # for rest/gas. A gassed closer collapses toward 0 — exactly the signal we want.
        role_prior = {"CLOSER": 0.55, "SETUP": 0.50, "MIDDLE": 0.38}[role]
        freq = napp / denom
        if gassed:
            rest_mult = 0.10
        elif days_rest == 0:                          # pitched yesterday
            rest_mult = 0.45 if (last_pitches or 0) >= 25 else 0.70
        else:
            rest_mult = 1.0
        usage = round(min(1.0, 0.5 * role_prior + 0.5 * freq) * rest_mult, 3)

        proj_inn = med_inn if med_inn else {"CLOSER": 9, "SETUP": 8, "MIDDLE": 6}[role]
        arms.append({"id": pid, "name": name, "hand": pitcher_hand(pid) or "?", "role": role,
                     "apps": napp, "gf": gf, "sv": pr["sv"], "last_date": last_date,
                     "days_rest": days_rest, "last_pitches": last_pitches, "gassed": gassed,
                     "med_inning": med_inn, "usage": usage, "proj_inning": proj_inn})

    arms.sort(key=lambda a: a["usage"], reverse=True)
    likely = arms[:top]                                       # 3-4 arms most likely to pitch
    closers = sorted([a for a in arms if a["role"] == "CLOSER"],
                     key=lambda a: a["sv"], reverse=True)     # surfaced even if gassed-out
    return {"likely": likely, "closers": closers, "all": arms, "n_games": len(team_dates)}

def pen_weak_share(pen_pred, weak_hand):
    """From a predict_bullpen() result, the USAGE-WEIGHTED share of the arms MOST LIKELY TO
    PITCH today that throw `weak_hand` — the hand the batter hits POORLY against. For a FLIP
    start weak_hand is the batter's OWN hand (a same-hand reliever flips his edge); for a
    DISADVANTAGE (same-side) start it's the pitcher's hand (the weak matchup only persists if
    that hand keeps coming — otherwise a favorable reliever bails him out). Weighting by
    likelihood means a gassed closer (usage ~0) barely counts and a workhorse arm counts a lot
    — unlike a flat headcount. We do NOT predict the middle-relief ORDER (too noisy) — just who's
    likely to appear. Returns (share, seq, n_weak, n_tot); share is None when nothing to project."""
    if not pen_pred or not pen_pred.get("likely"):
        return None, None, 0, 0
    arms = [a for a in pen_pred["likely"] if a.get("hand") in ("L", "R")]
    tot = sum(a["usage"] for a in arms)
    if not arms or tot <= 0:
        return None, None, 0, 0
    weak = sum(a["usage"] for a in arms if a["hand"] == weak_hand)
    seq = ", ".join(f"{a['name'].split()[-1]}({a['hand']})"
                    for a in sorted(arms, key=lambda a: a["usage"], reverse=True))
    n_weak = sum(1 for a in arms if a["hand"] == weak_hand)
    return weak / tot, seq, n_weak, len(arms)

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

def _match_bench(rows):
    """Greedy 1:1 assignment of bench bats to starters. Each bench bat can pinch-hit for at
    most one starter, and each starter takes at most one bat, so a single lefty on the bench
    can't be counted as an 'upgrade' behind three RHH platoon starters at once. Richest swaps
    (biggest OPS delta) are locked in first; a starter can fall back to a second qualifying
    bench bat if his top one is taken. Returns {order: {"kind": "full"|"contested", "note"}}.
    'full' = this starter owns a bat (best-fit target). 'contested' = every bat he'd want is
    already spoken for, so only residual risk remains — the note points at who took his top one."""
    name_by_order = {r["order"]: (r["prof"].get("name") or r["s"].get("fullName")) for r in rows}
    pairs = []                                   # (delta, order, bench-candidate)
    for r in rows:
        for c in r["bench_cands"]:
            pairs.append((c["delta"], r["order"], c))
    pairs.sort(key=lambda p: p[0], reverse=True)  # highest-value swap wins the bat first

    res = {}                                     # order -> resolution
    bat_owner = {}                               # bat id -> order that won it
    used_bats = set()
    for delta, order, c in pairs:
        if order in res or c["id"] in used_bats:
            continue
        res[order] = {"kind": "full", "note": c["note"]}
        used_bats.add(c["id"])
        bat_owner[c["id"]] = order

    for r in rows:                               # leftover starters with candidates → contested
        if r["order"] in res or not r["bench_cands"]:
            continue
        top = r["bench_cands"][0]
        owner = bat_owner.get(top["id"])
        won_by = _SUFFIX_RE.sub("", name_by_order.get(owner, "") or "").strip() if owner else ""
        who = won_by.split()[-1] if won_by else ""            # display last name, case preserved
        tail = f" — likely used on {who}" if who else " — shared lever"
        res[r["order"]] = {"kind": "contested", "note": f"{top['name']}{tail}"}
    return res

def analyze_side(g, side, opp_side, starters, mgr, ph_hist=None, unavailable=None, bench=None,
                 date_str=None):
    """Score one team's hitters vs the opposing probable pitcher. Only needs THAT
    team's lineup (+ the opposing SP, known in advance) — so we can fire the instant
    a team's lineup drops. `bench` is the GAME-DAY bench (with positions). `date_str` anchors
    the bullpen projection's last-7-games window (defaults to today, ET)."""
    ph_hist = ph_hist or {}
    date_str = date_str or datetime.now(ET_TZ).strftime("%Y-%m-%d")
    if len(starters) < 9:
        return []
    team = g["teams"][side]["team"]
    opp_team = g["teams"][opp_side]["team"]
    sp = g["teams"][opp_side].get("probablePitcher", {})
    sp_id = sp.get("id")
    sp_name = sp.get("fullName", "TBD")
    sp_hand = pitcher_hand(sp_id) if sp_id else None
    if sp_hand not in ("L", "R"):
        return []  # can't score platoon without SP handedness

    mgr_meta = mgr.get(str(team["id"]), {})
    tier = mgr_meta.get("platoon_tier", "med")   # platoon-specific tendency (our picks are platoon spots)
    recent_ph = manager_recent_ph(date_str, team["id"], 5)   # current 5-game PH profile + position swaps
    starter_ids = {s["id"] for s in starters}
    if bench is None:                       # fallback: season roster (no positions)
        bench = [p for p in active_hitters(team["id"]) if p["id"] not in starter_ids]
    pen_mix = opposing_bullpen_mix(opp_team["id"], sp_id, unavailable)
    pen_pred = predict_bullpen(opp_team["id"], date_str, exclude_sp_id=sp_id, unavailable=unavailable)
    sp_len  = starter_length(sp_id)
    sp_xw   = pitcher_xwoba(sp_id)          # starter quality (xwOBA-allowed) for the mismatch

    # Pass 1: build per-starter context + each starter's ranked bench-upgrade candidates.
    rows = []
    for order, s in enumerate(starters, start=1):
        prof = get_hitter_profile(s["id"], s.get("fullName"))
        scenario = classify_matchup(prof, sp_hand)
        bats  = prof.get("bats")
        pos   = s.get("pos")
        faced = "vl" if sp_hand == "L" else "vr"
        opp   = "vr" if sp_hand == "L" else "vl"
        bench_cands = []
        if scenario == "disadvantage":
            bench_cands = bench_upgrades(prof.get(faced, {}).get("ops"), sp_hand, bench, pos)
        elif scenario == "flip":
            bench_cands = bench_upgrades(prof.get(opp, {}).get("ops"), bats, bench, pos)
        rows.append({"order": order, "s": s, "prof": prof, "scenario": scenario,
                     "pos": pos, "bench_cands": bench_cands})

    # Resolve the finite bench: a given bat can pinch-hit for at most one starter, so award
    # the +18 to its best-fit target only; contested starters keep just residual risk (+6).
    bench_res_by_order = _match_bench(rows)

    # Pass 2: score with the resolved bench lever.
    candidates = []
    for row in rows:
        order, s, prof, scenario, pos = (row["order"], row["s"], row["prof"],
                                         row["scenario"], row["pos"])
        bvp = get_bvp(s["id"], sp_id) if sp_id else None
        name_last = _lastname(prof.get("name") or s.get("fullName"))
        ph_count = ph_hist.get(f"{team['id']}|{name_last}", 0)
        arsenal = arsenal_matchup(s["id"], sp_id) if sp_id else None
        # recent BEHAVIORAL pull evidence (this coach's last 5g): how often he pinch-hit for
        # THIS exact player, and for his POSITION — feeds the behavioral override in scoring.
        recent_pulls = sum(1 for e in recent_ph["events"]
                           if e.get("out_name") and _lastname(e["out_name"]) == name_last)
        recent_pos_pulls = sum(1 for e in recent_ph["events"] if e.get("out_pos") == pos)
        score, reasons = score_starter(prof, sp_hand, sp_name, order, tier,
                                       bench_res_by_order.get(order),
                                       scenario, pen_mix, bvp, ph_count, sp_len, arsenal, sp_xw, pos,
                                       pen_pred=pen_pred,
                                       recent_pulls=recent_pulls, recent_pos_pulls=recent_pos_pulls)
        # Recent (last-5-games) coach PH profile — current read that cuts through late-season
        # roster churn, plus the position swaps he's actually been making. Flag it when he's
        # recently pinch-hit for THIS player's position.
        if recent_ph["n_ph"] > 0:
            swaps_txt = ", ".join(recent_ph["swaps"][:6])
            line = (f"Coach last {recent_ph['games']}g: {recent_ph['n_ph']} PH "
                    f"({recent_ph['n_platoon']} platoon) — recent swaps: {swaps_txt}")
            if pos and any(sw.startswith(f"{pos}→") for sw in recent_ph["swaps"]):
                line += f" — incl. his spot ({pos})"
        else:
            line = f"Coach last {recent_ph['games']}g: 0 pinch-hits — inactive lately"
        reasons = reasons + [line]
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

def analyze_game(g, mgr, lineup=None, ph_hist=None, unavailable=None, date_str=None):
    """Both sides of a game (used by --print / build_board). Serve mode calls
    analyze_side directly, per side, as each lineup drops. `date_str` anchors the bullpen
    projection window (defaults to today, ET)."""
    if lineup is None:
        lineup = confirmed_lineup(g.get("gamePk"))
    cands = []
    for side, opp_side in (("away", "home"), ("home", "away")):
        sd = lineup.get(side, {})
        cands += analyze_side(g, side, opp_side, sd.get("starters", []), mgr,
                              ph_hist, unavailable, sd.get("bench"), date_str=date_str)
    cands.sort(key=lambda c: c["score"], reverse=True)
    return cands

def build_board(date_str, limit=None):
    """Whole-slate board (manual/--print mode)."""
    games = get_slate(date_str, limit)
    mgr   = manager_tendency(date_str)
    ph_hist = {r["key"]: r["count"] for r in pinch_hit_history(date_str)}
    unavail = unavailable_relievers(date_str)
    print(f"[slate] {date_str}: {len(games)} game(s) considered")
    candidates = []
    for g in games:
        candidates.extend(analyze_game(g, mgr, ph_hist=ph_hist, unavailable=unavail, date_str=date_str))
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

def post_game_embed(g, cands, when_str, is_update=False):
    """Posts one team's board (per side). Title reflects the batting team vs the
    opposing starter, since serve fires each side as its lineup drops. `is_update`
    marks a re-post after the lineup changed."""
    if not cands:
        return
    c0 = cands[0]
    prefix = "🔄 Lineup update — " if is_update else ""
    title = f"{prefix}Pinch-hit board — {c0['team']} vs {c0['sp_hand']}HP {c0['sp_name']}"
    fields = []
    for i, c in enumerate(cands[:GAME_TOP_N], 1):
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
        "description": (f"{c0['team']} vs {c0['opp']} · first pitch **{when_str}** · "
                        f"**Confidence /100** = how likely each starter is lifted early / limited "
                        f"to few at-bats. Higher = better under (H+R+RBI / hits)."),
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
# Dedupe is keyed by lineup FINGERPRINT, not just "posted once". A side is re-scanned
# (and re-posted as an adjustment) when its official batting order CHANGES — e.g. a
# late scratch or swap. Unchanged lineups are never re-touched.
def _posted_today(date_str):
    d = _load(POSTED_STATE_PATH).get(date_str, {})
    return d if isinstance(d, dict) else {}   # tolerate old list format

def _mark_handled(date_str, skey, fp, posted):
    d = _load(POSTED_STATE_PATH)
    cutoff = (datetime.now(ET_TZ).date() - timedelta(days=2)).strftime("%Y-%m-%d")
    d = {k: v for k, v in d.items() if k >= cutoff}
    day = d.setdefault(date_str, {})
    if not isinstance(day, dict):
        day = {}
        d[date_str] = day
    day[skey] = {"fp": fp, "posted": posted}
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
    if RESCAN_ON_BOOT:
        today0 = datetime.now(ET_TZ).strftime("%Y-%m-%d")
        d = _load(POSTED_STATE_PATH); d.pop(today0, None); _save(POSTED_STATE_PATH, d)
        # Also drop COMPUTED caches so stale values from a prior build (e.g. a leash
        # computed by buggy code earlier today) don't get served back from cache.
        _player_cache.clear()
        for pth in (PLAYER_CACHE_PATH, SUBS_CACHE_PATH):
            try:
                os.remove(pth)
            except Exception:
                pass
        print(f"[serve] RESCAN_ON_BOOT set — cleared posted-state + computed caches for {today0}; "
              f"every game/side will be re-scanned fresh on the first poll. REMOVE this env var "
              f"afterward so normal restarts don't re-post the whole board.")
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
            posted = _posted_today(today)                 # {skey: {"fp","posted"}}

            for g in games:
                pk = g.get("gamePk")
                state = g.get("status", {}).get("abstractGameState")
                gd = g.get("gameDate")
                try:
                    first = datetime.fromisoformat(gd.replace("Z", "+00:00")) if gd else None
                except Exception:
                    first = None
                when = _fmt_local(first) if first else "TBD"
                # Fire per SIDE the instant that team's official lineup drops. Re-fire as an
                # ADJUSTMENT if the lineup FINGERPRINT changes (late scratch/swap); skip if
                # it's the same lineup we already handled.
                lineup = confirmed_lineup(pk)
                for side, opp_side in (("away", "home"), ("home", "away")):
                    sd = lineup.get(side, {})
                    starters = sd.get("starters", [])
                    if len(starters) < 9:                 # this team's card not posted yet
                        continue
                    skey = f"{pk}:{side}"
                    # fingerprint includes the bench, so a trade/roster move (bench change)
                    # also re-triggers an updated board, not just a lineup change.
                    fp = (",".join(str(s["id"]) for s in starters) + "|" +
                          ",".join(str(b["id"]) for b in sd.get("bench", [])))
                    prev = posted.get(skey)
                    if prev and prev.get("fp") == fp:     # same lineup+bench already handled → skip
                        continue
                    if state in ("Live", "Final"):        # game started — too late
                        _mark_handled(today, skey, fp, False); posted[skey] = {"fp": fp, "posted": False}
                        continue
                    is_update = bool(prev and prev.get("posted"))   # already posted a different state
                    cands = analyze_side(g, side, opp_side, starters, mgr, ph_hist, unavail,
                                         sd.get("bench"), date_str=today)
                    _save(PLAYER_CACHE_PATH, _player_cache)
                    tm = g["teams"][side]["team"].get("abbreviation")
                    top = cands[0]["score"] if cands else 0
                    will_post = top >= POST_MIN_SCORE
                    _mark_handled(today, skey, fp, will_post)
                    posted[skey] = {"fp": fp, "posted": will_post}
                    tag = " [UPDATE]" if is_update else ""
                    if will_post:
                        post_game_embed(g, cands, when, is_update)
                        record_predictions(today, cands[:GAME_TOP_N])
                        print(f"[serve] posted{tag} {tm} — top {top}, {len(cands)} pick(s)")
                    else:
                        print(f"[serve] skipped{tag} {tm} — top {top} < {POST_MIN_SCORE} (no ping)")

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
    ap.add_argument("--clear-posted", action="store_true",
                    help="wipe today's posted-state so serve re-scans every game/side on its next "
                         "poll (use after a mid-slate fix to force fresh boards)")
    args = ap.parse_args()

    if args.clear_posted:
        today = datetime.now(ET_TZ).strftime("%Y-%m-%d")
        d = _load(POSTED_STATE_PATH); d.pop(today, None); _save(POSTED_STATE_PATH, d)
        print(f"[clear] wiped posted-state for {today} — serve will re-scan all games next poll.")
        return

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
