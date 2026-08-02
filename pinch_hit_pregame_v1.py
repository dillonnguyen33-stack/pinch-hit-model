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
import argparse
import requests
from datetime import datetime, timezone, timedelta
from zoneinfo import ZoneInfo

ET_TZ   = ZoneInfo("America/New_York")
API     = "https://statsapi.mlb.com/api/v1"
API11   = "https://statsapi.mlb.com/api/v1.1"

PREGAME_WEBHOOK_URL   = os.environ.get("PREGAME_WEBHOOK_URL")
ANTHROPIC_API_KEY     = os.environ.get("ANTHROPIC_API_KEY")
MANAGER_LOOKBACK_DAYS = int(os.environ.get("MANAGER_LOOKBACK_DAYS", "14"))
RECENCY_DAYS          = int(os.environ.get("RECENCY_DAYS", "14"))   # window for recent-form signal
LEAD_MINUTES          = int(os.environ.get("LEAD_MINUTES", "60"))    # serve mode: fire this many min before each game's first pitch
POLL_MINUTES          = int(os.environ.get("POLL_MINUTES", "10"))    # serve mode: how often the scheduler checks
GAME_TOP_N            = int(os.environ.get("GAME_TOP_N", "6"))       # max picks per per-game embed
POSTED_STATE_PATH     = os.environ.get("POSTED_STATE_PATH", "pregame_posted.json")
RESULTS_HOUR_ET       = int(os.environ.get("RESULTS_HOUR_ET", "3"))   # serve mode: grade the prior day at ~this hour
PREDICTIONS_PATH      = os.environ.get("PREDICTIONS_PATH", "pregame_predictions.json")
ACCURACY_PATH         = os.environ.get("ACCURACY_PATH", "pregame_accuracy.json")
TOP_N                 = int(os.environ.get("TOP_N", "10"))
MIN_SCORE             = int(os.environ.get("MIN_SCORE", "35"))
SEASON                = int(os.environ.get("SEASON", str(datetime.now(ET_TZ).year)))

# Disk caches so re-runs (and bench lookups) don't re-hit the API.
PLAYER_CACHE_PATH  = os.environ.get("PLAYER_CACHE_PATH",  f"pregame_players_{SEASON}.json")
MANAGER_CACHE_PATH = os.environ.get("MANAGER_CACHE_PATH", "pregame_manager_tendency.json")

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

# ── player profile: handedness + platoon splits (cached) ──────────────────────
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

    try:
        data = _get(f"{API}/people/{pid}/stats",
                    params={"stats": "statSplits", "sitCodes": "vl,vr",
                            "group": "hitting", "season": SEASON})
        for s in data.get("stats", []):
            for spl in s.get("splits", []):
                code = spl.get("split", {}).get("code")     # 'vl' or 'vr'
                st   = spl.get("stat", {})
                if code in ("vl", "vr"):
                    prof[code] = {
                        "ops": _to_float(st.get("ops")),
                        "avg": _to_float(st.get("avg")),
                        "pa":  int(st.get("plateAppearances") or 0),
                    }
    except Exception as e:
        print(f"[profile] splits error {pid}: {e}")

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

# ── manager / team recent pinch-hit tendency (reuses live-bot sub parsing) ─────
def manager_tendency(date_str):
    """Returns {team_id: {'subs':int,'games':int,'rate':float,'tier':str}} over the
    last MANAGER_LOOKBACK_DAYS ending the day before date_str. Cached per date so
    the first run of a day pays the cost once. Set MANAGER_LOOKBACK_DAYS=0 to skip."""
    if MANAGER_LOOKBACK_DAYS <= 0:
        return {}

    cache = _load(MANAGER_CACHE_PATH)
    ck = f"{date_str}:{MANAGER_LOOKBACK_DAYS}"
    if ck in cache:
        return cache[ck]

    end   = datetime.strptime(date_str, "%Y-%m-%d").date() - timedelta(days=1)
    start = end - timedelta(days=MANAGER_LOOKBACK_DAYS - 1)
    print(f"[manager] scanning subs {start}..{end} (first run today may take a minute)...")

    try:
        sched = _get(f"{API}/schedule",
                     params={"sportId": 1, "startDate": start.strftime("%Y-%m-%d"),
                             "endDate": end.strftime("%Y-%m-%d"), "gameType": "R"})
        game_pks = [g["gamePk"]
                    for d in sched.get("dates", [])
                    for g in d.get("games", [])
                    if g.get("status", {}).get("abstractGameState") == "Final"]
    except Exception as e:
        print(f"[manager] schedule error: {e}")
        return {}

    tally = {}  # team_id -> {'subs':int,'games':set}
    for pk in game_pks:
        try:
            live = _get(f"{API11}/game/{pk}/feed/live", timeout=20)
            box  = live.get("liveData", {}).get("boxscore", {}).get("teams", {})
            side_team = {"home": None, "away": None}
            for side in ("home", "away"):
                tid = box.get(side, {}).get("team", {}).get("id")
                side_team[side] = tid
                if tid is not None:
                    tally.setdefault(tid, {"subs": 0, "games": set()})["games"].add(pk)
            plays = live.get("liveData", {}).get("plays", {}).get("allPlays", [])
            for play in plays:
                half = play.get("about", {}).get("halfInning")   # 'top'/'bottom'
                batting_side = "away" if half == "top" else "home"
                bt = side_team.get(batting_side)
                for ev in play.get("playEvents", []):
                    det = ev.get("details", {})
                    desc = (det.get("description") or "").lower()
                    is_sub = ev.get("isSubstitution", False) or det.get("event") == "Offensive Substitution"
                    if is_sub and "pinch-hitter" in desc and bt is not None:
                        tally[bt]["subs"] += 1
        except Exception as e:
            print(f"[manager] game {pk} error: {e}")

    # convert to rate + tier. Tiers are relative to the league distribution.
    result = {}
    rates = []
    for tid, t in tally.items():
        g = max(1, len(t["games"]))
        rate = t["subs"] / g
        result[str(tid)] = {"subs": t["subs"], "games": g, "rate": round(rate, 2)}
        rates.append(rate)
    rates.sort()
    def pct(p):
        if not rates:
            return 0
        return rates[min(len(rates) - 1, int(p * len(rates)))]
    hi, lo = pct(0.66), pct(0.33)
    for tid, r in result.items():
        r["tier"] = "high" if r["rate"] >= hi else ("low" if r["rate"] <= lo else "med")

    cache[ck] = result
    _save(MANAGER_CACHE_PATH, cache)
    return result

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

def score_starter(prof, sp_hand, sp_name, order, mgr_tier, bench_note, scenario, pen_mix=None, bvp=None):
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
    total_pa = fpa + opa

    if scenario == "disadvantage":
        # SCENARIO 2: same-handed weak-side start. Poor matchup; pulled if early ABs
        # sour (that's the LIVE trigger) or the opposing pen stays his weak hand.
        gap = oo - fo
        pts = min(35.0, gap / 0.200 * 35.0)
        if fo < 0.680:
            pts += min(10.0, (0.680 - fo) / 0.200 * 10.0)
        if fpa < 30:                       # thin split = noisy; damp confidence
            pts *= 0.5
        score += pts
        samp = "" if fpa >= 30 else f", small sample {fpa} PA vs {sp_hand}HP"
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
    """Games with posted lineups + probable pitchers. Returns list of dicts."""
    sched = _get(f"{API}/schedule",
                 params={"sportId": 1, "date": date_str,
                         "hydrate": "probablePitcher,lineups,team"})
    games = [g for d in sched.get("dates", []) for g in d.get("games", [])]
    if limit:
        games = games[:limit]
    return games

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

def opposing_bullpen_mix(team_id, exclude_sp_id):
    """L/R counts of the opposing team's AVAILABLE bullpen (active-roster pitchers
    minus today's probable starter). Approximate — includes other rotation arms —
    but the L/R ratio is what the model uses. Computed once per team per run."""
    mix = {"L": 0, "R": 0, "total": 0}
    try:
        data = _get(f"{API}/teams/{team_id}/roster", params={"rosterType": "active"})
    except Exception as e:
        print(f"[bullpen] team {team_id} error: {e}")
        return mix
    for p in data.get("roster", []):
        if p.get("position", {}).get("type") != "Pitcher":
            continue
        if p["person"]["id"] == exclude_sp_id:
            continue
        h = pitcher_hand(p["person"]["id"])
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

def analyze_game(g, mgr):
    """Score every starter in a single game dict. Returns candidates sorted by score."""
    candidates = []
    lu = g.get("lineups", {})
    for side, opp_side in (("away", "home"), ("home", "away")):
        starters = lu.get(f"{side}Players", [])
        if not starters:
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
        pen_mix = opposing_bullpen_mix(opp_team["id"], sp_id)

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
            score, reasons = score_starter(prof, sp_hand, sp_name, order, tier,
                                           bench_note, scenario, pen_mix, bvp)
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
    print(f"[slate] {date_str}: {len(games)} game(s) considered")
    candidates = []
    for g in games:
        candidates.extend(analyze_game(g, mgr))
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

def post_game_embed(g, cands, when_str):
    away = g["teams"]["away"]["team"].get("abbreviation") or g["teams"]["away"]["team"]["name"]
    home = g["teams"]["home"]["team"].get("abbreviation") or g["teams"]["home"]["team"]["name"]
    title = f"🎯 Pull-Risk — {away} @ {home}"
    tagmap = {"flip": "🔄 platoon-flip", "disadvantage": "⚠️ poor matchup"}
    if not cands:
        _post_embed({"title": title, "color": 0x95A5A6,
                     "description": f"First pitch **{when_str}** — no qualifying pull-risk starters "
                                    f"(no strong platoon spots)."})
        return
    top = cands[:GAME_TOP_N]
    fields = []
    for i, c in enumerate(top, 1):
        nm = (f"#{i}  {_emoji(c['score'])} {c['score']}  {c['name']} "
              f"({c['bats']}, {_ord(c['order'])}) · {tagmap.get(c.get('scenario'), '')}")
        val = "\n".join(f"• {r}" for r in c["reasons"])
        summ = claude_summary(c)
        if summ:
            val += f"\n→ {summ}"
        fields.append({"name": nm[:256], "value": val[:1024], "inline": False})
    _post_embed({
        "title": title,
        "description": f"First pitch **{when_str}** · ranked most→least likely to be lifted "
                       f"early. Bet unders (H+R+RBI / hits), shop best odds.",
        "color": _color(top[0]["score"]),
        "fields": fields,
        "footer": {"text": "Pregame Pull-Risk Board v1 · review & forward your picks"},
    })

# ── per-game scheduler (serve mode) ───────────────────────────────────────────
def _lineups_both(g):
    lu = g.get("lineups", {})
    return len(lu.get("awayPlayers", [])) >= 9 and len(lu.get("homePlayers", [])) >= 9

def _lineups_any(g):
    lu = g.get("lineups", {})
    return len(lu.get("awayPlayers", [])) >= 9 or len(lu.get("homePlayers", [])) >= 9

def _mark_posted(date_str, pk):
    d = _load(POSTED_STATE_PATH)
    cutoff = (datetime.now(ET_TZ).date() - timedelta(days=2)).strftime("%Y-%m-%d")
    d = {k: v for k, v in d.items() if k >= cutoff}   # prune old days
    d.setdefault(date_str, [])
    if pk not in d[date_str]:
        d[date_str].append(pk)
    _save(POSTED_STATE_PATH, d)

def run_scheduler():
    print(f"[serve] Pregame scheduler up — fires ~{LEAD_MINUTES}m before each game's first "
          f"pitch, once its lineup posts. Polls every {POLL_MINUTES}m.")
    if not PREGAME_WEBHOOK_URL:
        print("[serve][warn] PREGAME_WEBHOOK_URL not set — embeds will print to console.")
    while True:
        try:
            today = datetime.now(ET_TZ).strftime("%Y-%m-%d")
            sched = _get(f"{API}/schedule", params={"sportId": 1, "date": today,
                        "hydrate": "probablePitcher,lineups,team"})
            games = [g for d in sched.get("dates", []) for g in d.get("games", [])]
            mgr = manager_tendency(today)                 # cached per date
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
                if not gd:
                    continue
                try:
                    first = datetime.fromisoformat(gd.replace("Z", "+00:00"))
                except Exception:
                    continue
                mins_to = (first - now).total_seconds() / 60.0
                if mins_to > LEAD_MINUTES:               # not within the lead window yet
                    continue
                # within the window: fire once both lineups are up (or one, if <10m to go)
                if _lineups_both(g) or (mins_to <= 10 and _lineups_any(g)):
                    cands = analyze_game(g, mgr)
                    post_game_embed(g, cands, _fmt_local(first))
                    record_predictions(today, cands[:GAME_TOP_N])   # for end-of-day grading
                    _save(PLAYER_CACHE_PATH, _player_cache)
                    _mark_posted(today, pk); posted.add(pk)
                    a = g["teams"]["away"]["team"].get("abbreviation")
                    h = g["teams"]["home"]["team"].get("abbreviation")
                    print(f"[serve] posted {a} @ {h} — {len(cands)} pick(s), first pitch {_fmt_local(first)}")
                # else: in window but lineups not posted yet — retry next poll

            # Daily grading: once past RESULTS_HOUR_ET, grade the prior day (once).
            now_et = datetime.now(ET_TZ)
            if now_et.hour >= RESULTS_HOUR_ET:
                yday = (now_et.date() - timedelta(days=1)).strftime("%Y-%m-%d")
                graded = {d["date"] for d in _load(ACCURACY_PATH).get("days", [])}
                if yday not in graded:
                    print(f"[results] grading {yday}...")
                    grade_day(yday)
        except Exception as e:
            print(f"[serve] loop error: {e}")
        time.sleep(POLL_MINUTES * 60)

# ── RESULTS / DAILY GRADING ───────────────────────────────────────────────────
# The betting thesis is "fewer ABs than the market assumes." So a pick "HITS" if
# the player was pinch-hit for OR got <= 3 PA (the under had a real shot). We also
# record actual PA / H / R / RBI so you can see whether the lines would have cashed.
def record_predictions(date_str, cands):
    """Persist the picks the board posted, so the end-of-day job can grade them."""
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
        msg = f"📊 Results — {date_str}: no predictions were recorded for this day."
        if to_discord:
            _post_embed({"title": f"📊 Results — {date_str}", "description": msg, "color": 0x95A5A6})
        else:
            print(msg)
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
    args = ap.parse_args()

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
