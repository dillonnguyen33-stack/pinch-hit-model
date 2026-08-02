"""
MLB Pinch Hit Alert Bot — v17.9

Changes from v17.8:
- LATENCY (transport): the filtered stream now requests identity encoding
  (Accept-Encoding: identity) via a stream-only STREAM_HEADERS. On a long-lived
  NDJSON stream, gzip means the client can't decode a tweet line until the
  compressor flushes a block — adding buffering latency to the twitter-delivery
  segment. Disabling it attacks that segment and costs nothing. (Search/user
  lookups still use the normal gzipped TWITTER_HEADERS — only the stream benefits.)
- LATENCY (discord): all Discord POST/PATCH now reuse ONE keep-alive
  requests.Session (_discord_session), so each alert skips a fresh TLS handshake
  (~100-300ms) to Discord instead of opening a new connection every time.
- MEASURE: _log_timing keeps a rolling window (last 200) of the twitter-delivery
  segment and prints min/median/max alongside each alert. This turns a
  region/tier change into a real before/after on the DISTRIBUTION, not a single
  noisy alert. Watch the median across a night of games.
  NOTE: west->east region move and gzip-off both attack the twitter segment but
  are expected to be small (~100-300ms) — the dominant ~5s is Twitter's internal
  index->push, which no host/transport change touches. If the rolling median
  stays flat, that's your evidence the delay is structural (and a paid API tier
  is an unproven bet against it).

Changes from v17.7:
- (v17.8) Footer edit after POST completes to include discord-post duration.

Changes from v17.6:
- LATENCY INSTRUMENTATION: every alert now separates OUR processing time from
  Twitter's stream-delivery lag. A timestamp is captured the instant a tweet
  arrives off the stream (before any processing); when the Discord POST
  completes we log:
      total (now - tweet.created_at) = twitter-delivery + internal
  Console line: "[reporter] @X: total 6.40s = twitter-delivery ~5.80s + internal 0.60s"
  Plus a threaded "Timing breakdown" reply on each alert.
  WHY: the existing single number bundled both, so it was impossible to tell
  whether 5-6s alerts were our code or Twitter. If internal is consistently
  <1s, the bottleneck is Twitter delivery and no code change will help; if it's
  2-3s, something in our pipeline is worth fixing. Note the general path's
  internal time INCLUDES the Claude round-trip (Claude is on the critical path
  for non-reporters); the reporter path's does not.

Changes from v17.5:
- FIRE-LOG NOW DISK-BACKED (fixes false "Alerted: 0" / real alerts flagged as
  missed). The fire-log was in-memory only, so a manual `--audit` run — a
  separate process — always saw it empty and reported everything as missed,
  even players the live bot did alert on (e.g. Max Muncy on 2026-07-20, which
  fired correctly but showed as a miss). It's now persisted to FIRE_LOG_PATH
  (default fire_log.json), date-keyed by ET day with 2-day pruning, so any
  process (live or manual audit) reads what was actually fired. was_fired_today
  now takes the audit's date so it checks the right day.
  NOTE: on ephemeral cloud filesystems, put FIRE_LOG_PATH on a persistent volume
  if you want the live process's fires to survive a restart between game time and
  the 3am audit.

Changes from v17.4:
- BUGFIX (false rejects): result_comes_after_ph and has_reject_phrase now match
  on WORD BOUNDARIES instead of raw substrings. The old substring match killed
  valid alerts whose player names contained a keyword substring — e.g. "Corbin"
  contains "rbi", so "Barrosa is pinch hitting for Corbin Carroll..." was wrongly
  dropped as "already happened". This class of bug silently ate every pre-event
  tweet involving affected names. Verified real already-happened tweets are still
  caught.
- CLAUDE EXAMPLES: added valid/boundary cases to the classifier prompt, most
  importantly that a past-tense clause explaining WHY a sub happens ("grimaced
  after a swing in his last AB", "left the game") does NOT make the sub itself
  past-tense. Sharpens the pre-event vs already-happened boundary.

Changes from v17.3:
- STREAM RULES (via recall audit): added "to pinch hit" (live). The 2026-07-18
  audit flagged a missed Jarren Duran pinch hit where a reporter wrote
  "...to pinch hit here for Jones" — the word "here" split the contiguous
  "pinch hit for" match. "to pinch hit" alone would have caught that miss.
- WATCH (tracked, not live): "will hit for" was left OUT of live rules and
  instead added to UNPROMOTED_PHRASES so future audits count how often it
  appears in genuinely catchable misses (Claude-confirmed) before committing.
  Reason: "will hit for power/average" is common chatter; the tracker measures
  real-catch rate so promotion is evidence-based. Total live stream rules: 16.

Changes from v17.2:
- RECALL AUDIT: New end-of-day job (runs ~AUDIT_HOUR_ET, default 3am ET) that
  pulls the official MLB pinch-hit substitutions from StatsAPI play-by-play,
  checks each against a lightweight in-memory fire-log of what the bot alerted
  on that day, and for every MISS does a medium-breadth Twitter *recent search*
  (player name + pinch/hit/PH terms, bounded window + low max_results to protect
  quota) and asks Claude whether any surfaced tweet was a catchable pre-event
  alert and why the live stream would have missed it. Results post to a
  dedicated audit webhook (DISCORD_AUDIT_WEBHOOK_URL), falling back to the log
  webhook. Only misses trigger a search, so cost stays low.
  Manual run: `python3 pinch_hit_bot_v17_9.py --audit YYYY-MM-DD`
- FIRE-LOG: minimal record_fire/was_fired_today added at both fire points so the
  audit knows what was caught. Cleared on daily reset.
- PHRASE TRACKER: for each catchable miss, the audit attributes which *unpromoted*
  second-tier phrase (in PINCH_HIT_PHRASES_LOWER but not a live STREAM_RULE — i.e.
  "phing for", "up to hit for", "in to hit for", "batting for", "ph for") would
  have caught it, and keeps a persistent tally (PHRASE_ROLLUP_PATH) with risk
  tiers. Posted at the end of each audit so you can promote medium/high-risk
  phrases to live rules on evidence, not guesswork. Nothing auto-promotes.

Changes from v17.1: expanded stream rules (4 phrasings that were in
PINCH_HIT_PHRASES_LOWER but never requested from Twitter).

Changes from v17.0:
- DEDUPE: When two different (non-reporter) tweets report the SAME event
          (same pinch hitter + same player replaced) within a short window,
          the second+ tweet is now posted as a THREADED REPLY on the original
          alert ("✅ Corroborated by @handle") instead of firing a brand new
          @everyone embed. Prevents duplicate "BET THE UNDER NOW" pings for
          one real-world sub.
- Event key is last-name-based (pinch_hitter_last|replaced_last) so minor
  name-formatting differences between tweets ("Luis Lara" vs "L. Lara")
  still match.
- CORROBORATION_WINDOW_SEC = 600 (10 min) — tune as needed.
- recent_fired_events is cleared on the daily reset, same as other state.

Changes from v16.0 (carried forward):
- LATENCY: Reporters now fire IMMEDIATELY on cheap-filter pass (trusted source);
           Claude runs ASYNC only to fill in structured names / retract if it
           turns out invalid. Removes the ~1-2s Anthropic round-trip from the
           critical path for the alerts that matter most.
- LATENCY: Claude timeout 15s -> 5s (a slow Anthropic call could silently delay
           an alert past any bettable window).
- MEASURE: Every alert now logs pipeline latency (now - tweet.created_at) in the
           footer, e.g. "fired 2.3s after tweet". Tells you how much of a miss is
           your pipeline vs. the reporter being slow.
- Non-reporter path unchanged (still gates on Claude + roster).
- ANTHROPIC_API_KEY now optional: reporters fire without it; only non-reporters
  are disabled if it's missing.
"""

import os
import re
import time
import json
import threading
import unicodedata
import requests
import statistics
from collections import deque
from datetime import datetime, timezone, timedelta
from zoneinfo import ZoneInfo

# ── CONFIG ────────────────────────────────────────────────────────────────────
TWITTER_BEARER_TOKEN = os.environ.get("TWITTER_BEARER_TOKEN")
DISCORD_WEBHOOK_URL  = os.environ.get("PINCH_HIT_WEBHOOK_URL")
DISCORD_CHANNEL_ID   = os.environ.get("DISCORD_CHANNEL_ID")
ANTHROPIC_API_KEY    = os.environ.get("ANTHROPIC_API_KEY")
DISCORD_LOG_WEBHOOK  = os.environ.get("DISCORD_LOG_WEBHOOK_URL")
# v17.3: end-of-day recall audit (separate channel/webhook)
DISCORD_AUDIT_WEBHOOK = os.environ.get("DISCORD_AUDIT_WEBHOOK_URL")
AUDIT_HOUR_ET        = int(os.environ.get("AUDIT_HOUR_ET", "3"))   # 3am ET: after all games final
AUDIT_SEARCH_MAX     = int(os.environ.get("AUDIT_SEARCH_MAX", "10"))  # max tweets pulled per missed PH (quota guard)
AUDIT_WINDOW_MIN     = int(os.environ.get("AUDIT_WINDOW_MIN", "20"))  # +/- minutes around the sub to search
PHRASE_ROLLUP_PATH   = os.environ.get("PHRASE_ROLLUP_PATH", "phrase_attribution.json")  # v17.3: persistent tally
ET_TZ                = ZoneInfo("America/New_York")
PLAYER_COOLDOWN_SEC  = 7200
PLAYER_MAX_ALERTS    = 2
LINEUP_REFRESH_SECS  = 300
CORROBORATION_WINDOW_SEC = 600  # v17.1: window to treat a 2nd report as corroboration, not a new alert

# ── ASCII NORMALIZATION ───────────────────────────────────────────────────────
def normalize(text):
    return unicodedata.normalize("NFD", text).encode("ascii", "ignore").decode("ascii").lower()

# ── SUFFIX STRIPPING ──────────────────────────────────────────────────────────
_SUFFIXES = re.compile(r'\b(jr\.?|sr\.?|ii|iii|iv)\s*$', re.IGNORECASE)

def strip_suffix(name):
    return _SUFFIXES.sub('', normalize(name)).strip()

# ── TWEET LATENCY ─────────────────────────────────────────────────────────────
def _tweet_latency(created_at):
    """Seconds from tweet creation to now (your pipeline latency). None if unknown.
    Twitter created_at is ISO 8601, e.g. '2024-06-01T23:15:30.000Z'."""
    if not created_at:
        return None
    try:
        t = datetime.fromisoformat(created_at.replace("Z", "+00:00"))
        return (datetime.now(timezone.utc) - t).total_seconds()
    except Exception:
        return None

def _latency_str(latency):
    return f"{latency:.1f}s" if latency is not None else "n/a"

# ── BEAT REPORTERS ────────────────────────────────────────────────────────────
REPORTERS = [
    {"handle": "JakeDRill",       "team": "Orioles"},
    {"handle": "masnRoch",        "team": "Orioles"},
    {"handle": "IanMBrowne",      "team": "Red Sox"},
    {"handle": "alexspeier",      "team": "Red Sox"},
    {"handle": "BryanHoch",       "team": "Yankees"},
    {"handle": "GJoyce9",         "team": "Yankees"},
    {"handle": "adamdberry",      "team": "Rays"},
    {"handle": "TBTimes_Rays",    "team": "Rays"},
    {"handle": "KeeganMatheson",  "team": "Blue Jays"},
    {"handle": "ShiDavidi",       "team": "Blue Jays"},
    {"handle": "scottmerkin",     "team": "White Sox"},
    {"handle": "JRFegan",         "team": "White Sox"},
    {"handle": "ZackMeisel",      "team": "Guardians"},
    {"handle": "beckjason",       "team": "Tigers"},
    {"handle": "CodyStavenhagen", "team": "Tigers"},
    {"handle": "alec_lewis",      "team": "Royals"},
    {"handle": "DanHayesMLB",     "team": "Twins"},
    {"handle": "dohyoungpark",    "team": "Twins"},
    {"handle": "brianmctaggart",  "team": "Astros"},
    {"handle": "Chandler_Rome",   "team": "Astros"},
    {"handle": "RhettBollinger",  "team": "Angels"},
    {"handle": "JeffFletcherOCR", "team": "Angels"},
    {"handle": "MartinJGallegos", "team": "Athletics"},
    {"handle": "DKramer_",        "team": "Mariners"},
    {"handle": "RyanDivish",      "team": "Mariners"},
    {"handle": "kennlandry",      "team": "Rangers"},
    {"handle": "Evan_P_Grant",    "team": "Rangers"},
    {"handle": "mlbbowman",       "team": "Braves"},
    {"handle": "DOBrienATL",      "team": "Braves"},
    {"handle": "AnthonyDiComo",   "team": "Mets"},
    {"handle": "TimBritton",      "team": "Mets"},
    {"handle": "ToddZolecki",     "team": "Phillies"},
    {"handle": "MattGelb",        "team": "Phillies"},
    {"handle": "CDeNicola13",     "team": "Marlins"},
    {"handle": "J_McPherson1126", "team": "Marlins"},
    {"handle": "JessicaCamerato", "team": "Nationals"},
    {"handle": "MarkZuckerman",   "team": "Nationals"},
    {"handle": "MLBastian",       "team": "Cubs"},
    {"handle": "sahadevsharma",   "team": "Cubs"},
    {"handle": "m_sheldon",       "team": "Reds"},
    {"handle": "AdamMcCalvy",     "team": "Brewers"},
    {"handle": "Todd_Rosiak",     "team": "Brewers"},
    {"handle": "justdelossantos", "team": "Pirates"},
    {"handle": "katiejwoo",       "team": "Cardinals"},
    {"handle": "JohnDenton555",   "team": "Cardinals"},
    {"handle": "SteveGilbertMLB", "team": "Diamondbacks"},
    {"handle": "nickpiecoro",     "team": "Diamondbacks"},
    {"handle": "harding_at_mlb",  "team": "Rockies"},
    {"handle": "psaundersdp",     "team": "Rockies"},
    {"handle": "juanctoribio",    "team": "Dodgers"},
    {"handle": "billplunkettocr", "team": "Dodgers"},
    {"handle": "AJCassavell",     "team": "Padres"},
    {"handle": "dennistlin",      "team": "Padres"},
    {"handle": "extrabaggs",      "team": "Giants"},
    {"handle": "mi_guardado",     "team": "Giants"},
]

REPORTER_HANDLES   = {r["handle"].lower() for r in REPORTERS}
REPORTER_BY_HANDLE = {r["handle"].lower(): r for r in REPORTERS}

TEAM_ALIASES = {
    "orioles": "Orioles", "baltimore": "Orioles",
    "red sox": "Red Sox", "boston": "Red Sox",
    "yankees": "Yankees", "new york yankees": "Yankees",
    "rays": "Rays", "tampa bay": "Rays",
    "blue jays": "Blue Jays", "toronto": "Blue Jays",
    "white sox": "White Sox", "chicago white sox": "White Sox",
    "guardians": "Guardians", "cleveland": "Guardians",
    "tigers": "Tigers", "detroit": "Tigers",
    "royals": "Royals", "kansas city": "Royals",
    "twins": "Twins", "minnesota": "Twins",
    "astros": "Astros", "houston": "Astros",
    "angels": "Angels", "los angeles angels": "Angels",
    "athletics": "Athletics", "oakland": "Athletics",
    "mariners": "Mariners", "seattle": "Mariners",
    "rangers": "Rangers", "texas": "Rangers",
    "braves": "Braves", "atlanta": "Braves",
    "marlins": "Marlins", "miami": "Marlins",
    "mets": "Mets", "new york mets": "Mets",
    "phillies": "Phillies", "philadelphia": "Phillies",
    "nationals": "Nationals", "washington": "Nationals",
    "cubs": "Cubs", "chicago cubs": "Cubs",
    "reds": "Reds", "cincinnati": "Reds",
    "brewers": "Brewers", "milwaukee": "Brewers",
    "pirates": "Pirates", "pittsburgh": "Pirates",
    "cardinals": "Cardinals", "st. louis": "Cardinals", "st louis": "Cardinals",
    "diamondbacks": "Diamondbacks", "arizona": "Diamondbacks", "dbacks": "Diamondbacks",
    "rockies": "Rockies", "colorado": "Rockies",
    "dodgers": "Dodgers", "los angeles dodgers": "Dodgers",
    "padres": "Padres", "san diego": "Padres",
    "giants": "Giants", "san francisco": "Giants",
}

# ── STREAM RULES ──────────────────────────────────────────────────────────────
STREAM_RULES = [
    {"value": '"pinch hit for" -is:retweet lang:en',          "tag": "pinch_hit_for"},
    {"value": '"pinch-hit for" -is:retweet lang:en',          "tag": "pinch_hit_for_hyph"},
    {"value": '"on deck to pinch hit" -is:retweet lang:en',   "tag": "on_deck"},
    {"value": '"slated to pinch hit" -is:retweet lang:en',    "tag": "slated"},
    {"value": '"will pinch hit" -is:retweet lang:en',         "tag": "will_ph"},
    {"value": '"will ph for" -is:retweet lang:en',            "tag": "will_ph_abbrev"},
    {"value": '"on deck to ph" -is:retweet lang:en',          "tag": "on_deck_ph_abbrev"},
    {"value": '"sent up to hit for" -is:retweet lang:en',     "tag": "sent_up"},
    {"value": '"hitting in place of" -is:retweet lang:en',    "tag": "in_place_of"},
    {"value": '"sent up for" -is:retweet lang:en',            "tag": "sent_up_short"},
    {"value": '"called upon to hit for" -is:retweet lang:en', "tag": "called_upon"},
    # v17.1: added — these already existed in PINCH_HIT_PHRASES_LOWER (the
    # post-arrival string check) but were never actually requested from
    # Twitter, so matching tweets were silently never delivered at all.
    {"value": '"pinch hitting for" -is:retweet lang:en',      "tag": "pinch_hitting_for"},
    {"value": '"pinch-hitting for" -is:retweet lang:en',      "tag": "pinch_hitting_for_hyph"},
    {"value": '"will be ph for" -is:retweet lang:en',         "tag": "will_be_ph_for"},
    {"value": '"coming up to hit for" -is:retweet lang:en',   "tag": "coming_up_to_hit_for"},
    # v17.4: surfaced by the recall audit (missed Jarren Duran, 2026-07-18).
    # "to pinch hit" catches cases where a word splits "pinch hit ... for"
    #   (e.g. "will go with Duran to pinch hit here for Jones") — safe, since
    #   "pinch hit" is already a specific baseball term. This alone would have
    #   caught the 2026-07-18 miss.
    # ("will hit for" was considered but left OUT of live rules — instead it's
    #  in the audit's WATCH tracker to measure how often it'd catch a real sub
    #  before committing to the noise. See UNPROMOTED_PHRASES.)
    {"value": '"to pinch hit" -is:retweet lang:en',           "tag": "to_pinch_hit"},
]

# ── CHEAP STRING PRE-FILTERS (run before Claude to save API calls) ────────────
REJECT_PHRASES = [
    "last night", "yesterday",
    "college", "university", "high school", "ncaa",
    "minor league", "minors", "triple-a", "double-a",
    "softball", "little league",
    "just pinch hit", "just pinch-hit",
    "just ph'd", "just phd",
    "pinch hit earlier", "pinch-hit earlier",
    "already pinch hit", "already pinch-hit",
    "has been pinch hit", "has been pinch-hit",
    "was pinch hit", "was pinch-hit",
    "have been pinch hit", "have been pinch-hit",
    "got pinch hit", "got pinch-hit",
    "after being pinch hit", "after being pinch-hit",
    "after pinch hit", "after ph",
    "pinched",
]

RESULT_WORDS = [
    "home run", "homerun", "homered", "homer",
    "singled", "doubled", "tripled",
    "struck out", "strikeout", "walked", "grounded out", "flied out", "lined out",
    "drove in", "rbi", "scored",
]

PINCH_HIT_PHRASES_LOWER = [
    "pinch hit for", "pinch-hit for",
    "pinch hitting for", "pinch-hitting for",
    "will pinch hit", "will ph for", "ph for", "phing for",
    "on deck to pinch hit", "slated to pinch hit",
    "will be ph for", "on deck to ph",
    "sent up to hit for", "sent up for",
    "hitting in place of", "batting for",
    "up to hit for", "in to hit for",
    "called upon to hit for", "coming up to hit for",
    "to pinch hit", "will hit for",   # v17.4: 'to pinch hit' live; 'will hit for' tracked-only (see UNPROMOTED_PHRASES)
]

def has_core_phrase(text):
    tl = normalize(text)
    return any(phrase in tl for phrase in PINCH_HIT_PHRASES_LOWER)

# v17.3: phrases that exist in PINCH_HIT_PHRASES_LOWER but are NOT yet promoted
# to live STREAM_RULES — i.e. the ones the live stream can't catch. The audit
# attributes each catchable miss to whichever of these would have caught it, so
# you can see over time which are worth promoting. Risk tier is informational
# only (shown in the rollup) — nothing auto-promotes.
UNPROMOTED_PHRASES = {
    # phrase         : risk tier (informational only — nothing auto-promotes)
    "phing for":       "low",
    "up to hit for":   "medium",
    "in to hit for":   "medium",
    "batting for":     "high",   # common English idiom ("batting for the team")
    "ph for":          "high",   # collides with pH / unrelated short strings
    "will hit for":    "medium", # v17.4: WATCH — "will hit for power/average" is
                                 # chatter, but "will hit for [Name]" is a real sub.
                                 # Tracked (not live) to measure real-catch rate first.
}

# Phrases that ARE live stream rules (derived from PINCH_HIT_PHRASES_LOWER minus
# the unpromoted set) — used to avoid falsely crediting an unpromoted phrase for
# a tweet the live stream would already have caught (e.g. "coming up to hit for"
# contains the substring "up to hit for").
PROMOTED_PHRASES = [p for p in PINCH_HIT_PHRASES_LOWER if p not in UNPROMOTED_PHRASES]

def attributable_phrases(text):
    """Which unpromoted second-tier phrases appear in this tweet AND would have
    been the reason it was caught — i.e. only if no already-promoted phrase
    already covers the tweet (otherwise the live stream would've caught it)."""
    tl = normalize(text)
    if any(p in tl for p in PROMOTED_PHRASES):
        return []  # a live rule already covers this tweet; not a promotion case
    return [p for p in UNPROMOTED_PHRASES if p in tl]

def has_reject_phrase(text):
    tl = normalize(text)
    # v17.5: word-boundary match (same fix as result_comes_after_ph). Prevents
    # short reject words like "minors"/"college"/"pinched" from matching as
    # substrings inside player names or unrelated words and killing valid alerts.
    for phrase in REJECT_PHRASES:
        if re.search(r'\b' + re.escape(phrase) + r'\b', tl):
            return True, phrase
    return False, None

def is_question(text):
    stripped = text.strip()
    if re.search(r"\?[\s\W]*$", stripped):
        return True
    if re.match(r"(did|does|would|should|could|can)\b", stripped.lower()):
        return True
    return False

def result_comes_after_ph(text):
    tl = normalize(text)
    ph_pos = -1
    for phrase in PINCH_HIT_PHRASES_LOWER:
        pos = tl.find(phrase)
        if pos != -1 and (ph_pos == -1 or pos < ph_pos):
            ph_pos = pos
    if ph_pos == -1:
        return False
    after_text = tl[ph_pos:]
    # v17.5: word-boundary match, NOT raw substring. Previously "rbi" matched
    # inside "Corbin", "scored" could match inside longer tokens, etc. — killing
    # valid pre-event alerts whose player names happened to contain a result
    # word as a substring. Multi-word result phrases ("struck out") are matched
    # as phrases with boundaries on each end.
    for word in RESULT_WORDS:
        if re.search(r'\b' + re.escape(word) + r'\b', after_text):
            return True
    return False

# ── STATE ─────────────────────────────────────────────────────────────────────
seen_tweet_ids       = set()
posted_alert_keys    = set()
last_reset_date      = None
player_alert_count   = {}
player_alert_time    = {}
daily_lineup_map     = {}
last_lineup_refresh  = 0
_name_index          = {}
_name_lock           = threading.Lock()
_lineup_lock         = threading.Lock()

# v17.1: tracks the most recent alert fired for a given (pinch_hitter, replaced)
# event so a second corroborating tweet can be posted as a reply instead of a
# brand new @everyone alert.
recent_fired_events  = {}
_events_lock         = threading.Lock()

# v17.3: minimal fire-log — just the info the end-of-day audit needs to know
# which pinch hits the bot actually alerted on today.
# v17.6: now DISK-BACKED and date-keyed. Previously in-memory only, so a manual
# `--audit` run (a separate process) always saw an empty log and reported
# "Alerted: 0" — flagging real alerts (e.g. Max Muncy) as missed. Persisting to
# disk lets the audit read what the live bot actually fired, regardless of
# process. Keyed by ET date so each day is isolated and stale days don't leak in.
fired_today          = {}   # last_name -> {"handle","full_name","time","path"}
_fired_log_lock      = threading.Lock()
last_audit_date      = None
FIRE_LOG_PATH        = os.environ.get("FIRE_LOG_PATH", "fire_log.json")

TWITTER_HEADERS = {
    "Authorization": f"Bearer {TWITTER_BEARER_TOKEN}",
    "Content-Type":  "application/json",
}

# v17.9: stream-only headers. Accept-Encoding: identity disables gzip on the
# long-lived NDJSON filtered stream so tweet lines aren't held in a gzip
# decompression buffer before we can read them off the socket. Do NOT reuse this
# for search/user-lookup calls — only the persistent stream benefits, and those
# one-shot calls are perfectly fine gzipped.
STREAM_HEADERS = {
    "Authorization":   f"Bearer {TWITTER_BEARER_TOKEN}",
    "Accept-Encoding": "identity",
}

# v17.9: single keep-alive session reused for ALL Discord POST/PATCH calls, so
# each alert reuses the warm TLS connection instead of paying a fresh handshake
# (~100-300ms) to Discord on every post.
_discord_session = requests.Session()

# v17.9: rolling window (last 200) of the twitter-delivery segment in seconds,
# so _log_timing can print min/median/max. Lets a region/tier change be judged
# on the distribution, not one noisy alert.
_twitter_seg = deque(maxlen=200)

def _today_et_str():
    return datetime.now(ET_TZ).strftime("%Y-%m-%d")

def _load_fire_log():
    """Returns {date_str: {last_name: {...}}}. Empty dict if missing/corrupt."""
    try:
        with open(FIRE_LOG_PATH) as f:
            return json.load(f)
    except Exception:
        return {}

def _save_fire_log(data):
    try:
        with open(FIRE_LOG_PATH, "w") as f:
            json.dump(data, f, default=str)
    except Exception as e:
        print(f"[fire-log save error] {e}")

def record_fire(full_name, handle, path):
    """path: 'reporter' or 'general'. Records that the bot alerted on this
    pinch hitter today, for the recall audit to check against. Disk-backed so
    a separate audit process can read it."""
    if not full_name:
        return
    key = strip_suffix(full_name).split()[-1]
    day = _today_et_str()
    with _fired_log_lock:
        data = _load_fire_log()
        # prune anything older than ~2 days so the file stays small
        cutoff = (datetime.now(ET_TZ) - timedelta(days=2)).strftime("%Y-%m-%d")
        data = {d: v for d, v in data.items() if d >= cutoff}
        day_map = data.setdefault(day, {})
        day_map[key] = {
            "handle":    handle,
            "full_name": full_name,
            "time":      time.time(),
            "path":      path,
        }
        _save_fire_log(data)
        fired_today[key] = day_map[key]  # keep in-memory copy in sync too

def was_fired_today(full_name, date_str=None):
    """Check whether the bot alerted on this pinch hitter on date_str (default:
    today ET). Reads from disk so it works from a separate audit process."""
    if not full_name:
        return None
    key = strip_suffix(full_name).split()[-1]
    day = date_str or _today_et_str()
    data = _load_fire_log()
    return data.get(day, {}).get(key)

# ── GAME HOURS / RESET ────────────────────────────────────────────────────────
def is_game_hours():
    hour = datetime.now(ET_TZ).hour
    return hour >= 12 or hour == 0

def maybe_reset_daily():
    global seen_tweet_ids, last_reset_date, posted_alert_keys
    global player_alert_count, player_alert_time, daily_lineup_map, last_lineup_refresh
    global recent_fired_events
    today = datetime.now(ET_TZ).date()
    if last_reset_date is None:
        last_reset_date = today
        return
    if today > last_reset_date:
        print("[reset] New day — clearing all state")
        seen_tweet_ids      = set()
        posted_alert_keys   = set()
        player_alert_count  = {}
        player_alert_time   = {}
        daily_lineup_map    = {}
        last_lineup_refresh = 0
        _name_index.clear()
        with _events_lock:
            recent_fired_events.clear()
        with _fired_log_lock:
            fired_today.clear()
        last_reset_date     = today

# ── PLAYER COOLDOWN ───────────────────────────────────────────────────────────
def is_player_on_cooldown(name):
    if not name:
        return False
    key = strip_suffix(name).split()[-1]
    now = time.time()
    if key in player_alert_time:
        if now - player_alert_time[key] > PLAYER_COOLDOWN_SEC:
            player_alert_count[key] = 0
    return player_alert_count.get(key, 0) >= PLAYER_MAX_ALERTS

def record_player_alert(name):
    if not name:
        return
    key = strip_suffix(name).split()[-1]
    player_alert_count[key] = player_alert_count.get(key, 0) + 1
    player_alert_time[key]  = time.time()

# ── EVENT DEDUPE / CORROBORATION (v17.1) ──────────────────────────────────────
def _event_key(pinch_hitter, replaced):
    """Last-name-based key so 'Luis Lara' and 'L. Lara' still match."""
    ph_last  = strip_suffix(pinch_hitter).split()[-1] if pinch_hitter else ""
    out_last = strip_suffix(replaced).split()[-1]     if replaced     else ""
    return f"{ph_last}|{out_last}"

def _get_recent_event(key):
    now = time.time()
    with _events_lock:
        entry = recent_fired_events.get(key)
        if entry and (now - entry["time"]) <= CORROBORATION_WINDOW_SEC:
            return entry
    return None

def _record_event(key, message_id):
    with _events_lock:
        recent_fired_events[key] = {"message_id": message_id, "time": time.time()}

# ── ROSTER / NAME INDEX ───────────────────────────────────────────────────────
def _add_player_to_map(target_map, full_name, team_name):
    if not full_name:
        return
    norm  = strip_suffix(full_name)
    parts = norm.split()
    target_map[norm] = team_name
    if len(parts) >= 2:
        target_map[parts[0] + " " + parts[-1]] = team_name
    if len(parts) >= 1 and len(parts[-1]) >= 3:
        target_map["_last_" + parts[-1]] = team_name
    if len(parts) >= 1 and len(parts[0]) >= 5:
        target_map["_first_" + parts[0]] = team_name

def rebuild_index(new_entries):
    with _name_lock:
        _name_index.update(new_entries)

def _levenshtein(a, b):
    if abs(len(a) - len(b)) > 1:
        return 2
    if a == b:
        return 0
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a):
        curr = [i + 1]
        for j, cb in enumerate(b):
            curr.append(min(prev[j] + (ca != cb), prev[j+1] + 1, curr[j] + 1))
        prev = curr
    return prev[-1]

def _fuzzy_lookup(key, index):
    if key in index:
        return index[key]
    if len(key) < 5:
        return None
    for ikey, team in index.items():
        if ikey.startswith("_"):
            continue
        if abs(len(ikey) - len(key)) > 1:
            continue
        if _levenshtein(key, ikey) == 1:
            return team
    return None

def _resolve_name(name):
    if not name:
        return None
    with _name_lock:
        idx = dict(_name_index)
    norm  = strip_suffix(name)
    parts = norm.split()
    if norm in idx:
        return idx[norm]
    if len(parts) >= 2:
        fl = parts[0] + " " + parts[-1]
        if fl in idx:
            return idx[fl]
    if parts and len(parts[-1]) >= 3:
        key = "_last_" + parts[-1]
        if key in idx:
            return idx[key]
    if parts and len(parts[0]) >= 5:
        key = "_first_" + parts[0]
        if key in idx:
            return idx[key]
    if len(norm) >= 5:
        result = _fuzzy_lookup(norm, idx)
        if result:
            return result
    if parts and len(parts[-1]) >= 5:
        last_idx = {k[6:]: v for k, v in idx.items() if k.startswith("_last_")}
        result = _fuzzy_lookup(parts[-1], last_idx)
        if result:
            return result
    return None

def lookup_player_team(name):
    return _resolve_name(name)

def infer_team_from_text(text):
    tl = normalize(text)
    for alias, team in TEAM_ALIASES.items():
        if alias in tl:
            return team
    return None

# ── ROSTER PREFETCH ───────────────────────────────────────────────────────────
def prefetch_full_rosters():
    def _run():
        try:
            teams_r = requests.get(
                "https://statsapi.mlb.com/api/v1/teams",
                params={"sportId": 1}, timeout=10
            )
            teams_r.raise_for_status()
            team_ids = [t["id"] for t in teams_r.json().get("teams", [])]
        except Exception as e:
            print(f"[roster] Teams fetch error: {e}")
            return

        new_entries = {}
        for tid in team_ids:
            try:
                r = requests.get(
                    f"https://statsapi.mlb.com/api/v1/teams/{tid}/roster",
                    params={"rosterType": "active"}, timeout=10
                )
                r.raise_for_status()
                for p in r.json().get("roster", []):
                    full      = p.get("person", {}).get("fullName", "")
                    team_name = p.get("team", {}).get("name", "Unknown")
                    _add_player_to_map(new_entries, full, team_name)
            except Exception as e:
                print(f"[roster] Team {tid} error: {e}")

        with _lineup_lock:
            daily_lineup_map.update(new_entries)
        rebuild_index(new_entries)
        print(f"[roster] Full rosters loaded — {len(new_entries)} entries\n")

    threading.Thread(target=_run, daemon=True).start()

def start_lineup_refresh_thread():
    def _do_refresh():
        global last_lineup_refresh
        print("[lineup] Refreshing today's MLB lineups...")
        new_map = {}
        try:
            today_str = datetime.now(ET_TZ).strftime("%Y-%m-%d")
            sched = requests.get(
                "https://statsapi.mlb.com/api/v1/schedule",
                params={"sportId": 1, "date": today_str, "gameType": "R",
                        "hydrate": "roster,lineups"},
                timeout=10
            )
            sched.raise_for_status()
            game_pks = [
                g["gamePk"]
                for d in sched.json().get("dates", [])
                for g in d.get("games", [])
            ]
        except Exception as e:
            print(f"[lineup] Schedule error: {e}")
            return

        for game_pk in game_pks:
            try:
                r = requests.get(
                    f"https://statsapi.mlb.com/api/v1.1/game/{game_pk}/feed/live",
                    timeout=10
                )
                r.raise_for_status()
                boxscore = r.json().get("liveData", {}).get("boxscore", {})
                for side in ["home", "away"]:
                    team_data = boxscore.get("teams", {}).get(side, {})
                    team_name = team_data.get("team", {}).get("name", "")
                    team_mapped = None
                    for alias, mapped in TEAM_ALIASES.items():
                        if alias in team_name.lower():
                            team_mapped = mapped
                            break
                    for pid, pdata in team_data.get("players", {}).items():
                        full = pdata.get("person", {}).get("fullName", "")
                        team = team_mapped or team_name
                        _add_player_to_map(new_map, full, team)
            except Exception as e:
                print(f"[lineup] Game {game_pk} error: {e}")

        with _lineup_lock:
            daily_lineup_map.update(new_map)
            last_lineup_refresh = time.time()
        rebuild_index(new_map)
        print(f"[lineup] {len([k for k in new_map if not k.startswith('_')])} players loaded\n")

    def loop():
        while True:
            try:
                _do_refresh()
            except Exception as e:
                print(f"[lineup] Refresh error: {e}")
            time.sleep(LINEUP_REFRESH_SECS)

    threading.Thread(target=loop, daemon=True).start()
    print("[lineup] Background refresh thread started (every 5 min)\n")

# ── CLAUDE — NAME EXTRACTOR + VALIDATOR ───────────────────────────────────────
CLAUDE_SYSTEM = """You are an MLB pinch hit alert parser. Extract player names and classify tweets.

Given a tweet, respond with ONLY a JSON object (no markdown, no explanation):
{
  "is_valid": true or false,
  "pinch_hitter": "Full Name" or null,
  "replaced": "Full Name" or null,
  "reason": "one short sentence"
}

is_valid = true ONLY if ALL of these are true:
1. A specific named player is ABOUT TO pinch hit (pre-event, imminent or confirmed)
2. A specific named player is being replaced (named or clearly identified)
3. This is an MLB game (not college, minor league, high school, softball, or non-baseball usage)
4. The at-bat has NOT happened yet (no past-tense results like singled, homered, struck out)
5. It is not a question, speculation, fan demand, or hypothetical

Extract real player names even from complex sentence structures. Examples:
- "Brendan Donovan has come on deck to pinch hit for Cal Raleigh" → pinch_hitter: "Brendan Donovan", replaced: "Cal Raleigh", is_valid: true
- "Casey Schmitt needs to pinch hit for Chapman" → is_valid: false (fan demand, not confirmed)
- "He'll pinch hit for Kelenic when a lefty is in" → is_valid: false (conditional/hypothetical)
- "Soto was pinch hit for in the 7th" → is_valid: false (already happened)
- "Should they pinch hit for him?" → is_valid: false (question)
- "warming up to pinch hit for Leon" → is_valid: true (imminent pre-event)
- "Tonight Brian returns to pinch-hit for Laura on the IngrahamAngle" → is_valid: false (not baseball)
- "pinch hit for Lutterman… #Hokies" → is_valid: false (college baseball)
- "Barrosa is pinch hitting for Corbin Carroll who grimaced after a swing in his last AB" → pinch_hitter: "Barrosa", replaced: "Corbin Carroll", is_valid: true (the past-tense clause "grimaced...last AB" explains WHY the sub is happening; it does NOT mean the pinch-hit at-bat already occurred — the sub itself is present/imminent)
- "Will go with Duran to pinch hit here for Jones vs the righty" → pinch_hitter: "Duran", replaced: "Jones", is_valid: true (confirmed pre-event; a word between "pinch hit" and "for" does not change validity)
- "Pinch hitter Segura, who homered earlier, is due up in the 9th" → is_valid: false (describes a player who already batted; not a new pre-event sub being announced now)

IMPORTANT: A past-tense clause describing WHY a player is being replaced (injury, "grimaced", "left the game", "who struck out last inning") does NOT make the pinch-hit invalid. Judge whether the SUBSTITUTION ITSELF is pre-event (about to happen) vs already completed.

For is_valid=true, both pinch_hitter and replaced must be non-null."""

def claude_classify(tweet_text, callback):
    if not ANTHROPIC_API_KEY:
        callback(False, None, None, "No API key")
        return

    def _run():
        try:
            r = requests.post(
                "https://api.anthropic.com/v1/messages",
                headers={
                    "x-api-key":         ANTHROPIC_API_KEY,
                    "anthropic-version": "2023-06-01",
                    "content-type":      "application/json",
                },
                json={
                    "model":      "claude-haiku-4-5-20251001",
                    "max_tokens": 150,
                    "system":     CLAUDE_SYSTEM,
                    "messages":   [{"role": "user", "content": f'Tweet: "{tweet_text}"'}],
                },
                timeout=5  # v17: was 15s — a slow call must not delay an alert
            )
            r.raise_for_status()
            raw = r.json()["content"][0]["text"].strip()
            raw = re.sub(r"^```json\s*", "", raw)
            raw = re.sub(r"```\s*$", "", raw)
            data = json.loads(raw)
            callback(
                bool(data.get("is_valid")),
                data.get("pinch_hitter"),
                data.get("replaced"),
                data.get("reason", "")
            )
        except Exception as e:
            print(f"[claude] Error: {e}")
            callback(False, None, None, f"Claude error: {e}")

    threading.Thread(target=_run, daemon=True).start()

# ── DISCORD ───────────────────────────────────────────────────────────────────
def _edit_footer_with_post_time(message_id, embed_payload, post_dur):
    """v17.8: after the POST completes we finally know the discord-post duration.
    Patch the alert's footer so the visible number includes it (the footer was
    built before the POST and couldn't know it). Best-effort; skips silently if
    it can't. Does NOT delay the alert — the alert is already visible; this only
    refines the footer a fraction of a second later."""
    if not (message_id and DISCORD_WEBHOOK_URL and post_dur is not None):
        return
    try:
        embeds = embed_payload.get("embeds", [])
        if not embeds:
            return
        old = embeds[0].get("footer", {}).get("text", "")
        embeds[0]["footer"]["text"] = old + f" + {post_dur:.1f}s discord"
        edit_url = f"{DISCORD_WEBHOOK_URL}/messages/{message_id}"
        _discord_session.patch(edit_url, json={"embeds": embeds}, timeout=10)  # v17.9: keep-alive session
    except Exception as e:
        print(f"[footer edit error] {e}")

def _post_discord_now(payload, return_msg_id=False, return_timing=False):
    if not DISCORD_WEBHOOK_URL:
        print("[discord error] Webhook URL missing!")
        return (None, None) if return_timing else None
    msg_id, post_dur = None, None
    try:
        url = DISCORD_WEBHOOK_URL + ("?wait=true" if return_msg_id else "")
        _t0 = time.monotonic()
        r = _discord_session.post(url, json=payload, timeout=10)  # v17.9: keep-alive session
        post_dur = time.monotonic() - _t0
        r.raise_for_status()
        if return_msg_id:
            msg_id = r.json().get("id")
    except Exception as e:
        print(f"[discord error] {e}")
    return (msg_id, post_dur) if return_timing else msg_id

def post_discord(payload):
    threading.Thread(target=_post_discord_now, args=(payload,), daemon=True).start()

def _post_reply(message_id, content):
    """Post a threaded reply to an existing alert message (name enrichment / retract / corroboration)."""
    if not (message_id and DISCORD_CHANNEL_ID and DISCORD_WEBHOOK_URL):
        return
    payload = {
        "content": content,
        "message_reference": {
            "message_id":         message_id,
            "channel_id":         DISCORD_CHANNEL_ID,
            "fail_if_not_exists": False,
        },
    }
    try:
        _discord_session.post(DISCORD_WEBHOOK_URL + "?wait=true", json=payload, timeout=10)  # v17.9: keep-alive session
    except Exception as e:
        print(f"[reply error] {e}")

# ── DROP LOG ──────────────────────────────────────────────────────────────────
def post_drop_log(handle, reason, text):
    if not DISCORD_LOG_WEBHOOK:
        return
    def _send():
        try:
            timestamp = datetime.now(ET_TZ).strftime("%H:%M:%S ET")
            payload = {
                "content": (
                    f"`{timestamp}` 🚫 **@{handle}** dropped — *{reason}*\n"
                    f">>> {text[:200]}"
                )
            }
            requests.post(DISCORD_LOG_WEBHOOK, json=payload, timeout=8)
        except Exception as e:
            print(f"[log error] {e}")
    threading.Thread(target=_send, daemon=True).start()

# ── ENRICHMENT (general alerts only) ──────────────────────────────────────────
def fetch_twitter_user_info(handle):
    try:
        r = requests.get(
            f"https://api.twitter.com/2/users/by/username/{handle}",
            headers=TWITTER_HEADERS,
            params={"user.fields": "public_metrics,verified,created_at"},
            timeout=8
        )
        r.raise_for_status()
        u       = r.json().get("data", {})
        metrics = u.get("public_metrics", {})
        return {
            "verified":    u.get("verified", False),
            "tweet_count": metrics.get("tweet_count", 0),
            "followers":   metrics.get("followers_count", 0),
        }
    except Exception as e:
        print(f"[user info error] {e}")
        return None

def compute_validity_rating(user_info, is_reporter):
    score   = 5
    reasons = []
    if is_reporter:
        score += 3
        reasons.append("✅ Verified MLB beat reporter")
    elif user_info:
        if user_info["verified"]:
            score += 2
            reasons.append("✅ Verified account")
        followers = user_info["followers"]
        if followers >= 50000:
            score += 2
            reasons.append(f"✅ Large following ({followers:,})")
        elif followers >= 10000:
            score += 1
            reasons.append(f"⚠️ Mid-tier following ({followers:,})")
        else:
            score -= 1
            reasons.append(f"⚠️ Small following ({followers:,})")
        tweets = user_info["tweet_count"]
        if tweets >= 10000:
            reasons.append(f"📊 Active account ({tweets:,} tweets)")
        else:
            score -= 1
            reasons.append(f"📊 Low tweet history ({tweets:,} tweets)")
    else:
        reasons.append("❓ Could not fetch account data")
    score = max(1, min(10, score))
    label = "🔴 LOW" if score <= 4 else "🟡 MEDIUM" if score <= 6 else "🟢 HIGH"
    return score, label, reasons

def post_followup_reply(message_id, handle, is_reporter, team, pinch_hitter, replaced):
    def _run():
        if not DISCORD_CHANNEL_ID:
            return
        user_info = fetch_twitter_user_info(handle)
        score, label, reasons = compute_validity_rating(user_info, is_reporter)
        lines = [f"**📊 Alert Enrichment — @{handle}**\n"]
        lines.append(f"🏟️ **Teams involved:** {team}")
        if user_info:
            lines.append(
                f"👤 **Account:** "
                f"{'✅ Verified' if user_info['verified'] else 'Unverified'} · "
                f"{user_info['followers']:,} followers · "
                f"{user_info['tweet_count']:,} tweets"
            )
        else:
            lines.append("👤 **Account:** Could not fetch info")
        lines.append("🌐 **Source type:** General Twitter user")
        lines.append(f"\n**Validity: {score}/10 — {label}**")
        for reason in reasons:
            lines.append(f"  {reason}")
        _post_reply(message_id, "\n".join(lines))
    threading.Thread(target=_run, daemon=True).start()

# ── ALERT SENDERS ─────────────────────────────────────────────────────────────
# ── LATENCY INSTRUMENTATION (v17.7) ───────────────────────────────────────────
# The existing "fired Xs after tweet" number = now - tweet.created_at, which
# BUNDLES Twitter's stream-delivery lag with our own processing. These helpers
# separate the two:
#   internal = tweet received off stream -> Discord POST completed  (OUR time)
#   twitter  = total - internal                                     (THEIR time)
# If internal is consistently <1s, the bottleneck is Twitter delivery and no
# code change will help. If internal is 2-3s, something in our pipeline is slow.
def _log_timing(path, handle, total, internal, post_dur=None):
    if internal is None:
        return
    post_str = f" + {post_dur:.2f}s discord-post" if post_dur is not None else ""
    if total is not None:
        twitter = max(0.0, total - internal)
        # v17.9: track rolling distribution of the twitter-delivery segment so a
        # region/tier/gzip change can be judged on min/median/max, not one alert.
        _twitter_seg.append(twitter)
        _snap = list(_twitter_seg)  # v17.9: atomic snapshot so stats can never hiccup on concurrent alerts
        roll = (f" | twitter rolling n={len(_snap)} "
                f"min {min(_snap):.1f}/med {statistics.median(_snap):.1f}/"
                f"max {max(_snap):.1f}s")
        e2e = total + (post_dur or 0.0)
        print(f"  ⏱️  [{path}] @{handle}: ~{e2e:.2f}s end-to-end "
              f"(twitter ~{twitter:.2f}s + bot {internal:.2f}s{post_str}){roll}")
    else:
        print(f"  ⏱️  [{path}] @{handle}: internal {internal:.2f}s{post_str} (total n/a)")

def post_reporter_alert_fast(handle, text, url, team, latency, recv_t=None):
    """v17: Fire the reporter alert IMMEDIATELY (trusted source, cheap filters
    already passed). Claude runs ASYNC only to fill in the structured names or
    retract if it turns out invalid — it is NOT on the critical path."""
    # v17.7: internal time measured here, just before the POST. Captures stream
    # receive -> filters -> now. (Excludes the POST round-trip itself, which the
    # footer can't know since it's built before posting.)
    internal = (time.monotonic() - recv_t) if recv_t is not None else None
    footer = f"Beat Reporter · {datetime.now(timezone.utc).strftime('%H:%M UTC')}"
    if latency is not None:
        footer += f" · fired {latency:.1f}s after tweet"
        if internal is not None:
            footer += f" ({max(0.0, latency - internal):.1f}s twitter + {internal:.1f}s bot)"
    elif internal is not None:
        footer += f" · {internal:.1f}s bot"
    embed_payload = {
        "content": "@everyone 🔥 BEAT REPORTER",
        "embeds": [{"title": f"🔥⚾ BEAT REPORTER ALERT — {team}",
            "description": (
                f"**Verified beat reporter — pre-event pinch hit**\n\n"
                f"🎙️ **@{handle}:**\n_{text[:200]}_\n🔗 [View Tweet]({url})\n\n"
                f"💰 **HIGH CONFIDENCE — BET THE UNDER NOW**"
            ),
            "color": 0x00FF00,
            "footer": {"text": footer}}]
    }

    def _send():
        msg_id, post_dur = _post_discord_now(embed_payload, return_msg_id=True, return_timing=True)
        _log_timing("reporter", handle, latency, internal, post_dur)
        _edit_footer_with_post_time(msg_id, embed_payload, post_dur)

        def on_claude(is_valid, ph, out, reason):
            if is_valid and ph and out:
                record_player_alert(ph)
                record_fire(ph, handle, "reporter")
                if msg_id:
                    _record_event(_event_key(ph, out), msg_id)
                _post_reply(msg_id, f"📋 **{ph}** will pinch hit for **{out}**")
            elif reason and not reason.startswith("No API key") \
                    and not reason.startswith("Claude error"):
                _post_reply(msg_id, f"⚠️ Auto-check flagged this as possibly invalid: _{reason}_")

        claude_classify(text, on_claude)

    threading.Thread(target=_send, daemon=True).start()
    print(f"  ⚡🟢 Reporter (fast fire): {team} — @{handle} | latency {_latency_str(latency)}")

def post_general_alert(handle, text, url, team, pinch_hitter, replaced, latency=None, event_key=None, recv_t=None):
    summary = f"**{pinch_hitter}** will pinch hit for **{replaced}**"
    # v17.7: internal time measured before the POST. On this path it INCLUDES
    # the Claude round-trip, since Claude gates non-reporter alerts.
    internal = (time.monotonic() - recv_t) if recv_t is not None else None
    footer = f"General Alert · {datetime.now(timezone.utc).strftime('%H:%M UTC')}"
    if latency is not None:
        footer += f" · fired {latency:.1f}s after tweet"
        if internal is not None:
            footer += f" ({max(0.0, latency - internal):.1f}s twitter + {internal:.1f}s bot)"
    elif internal is not None:
        footer += f" · {internal:.1f}s bot"
    embed_payload = {
        "content": "@everyone",
        "embeds": [{"title": f"⚾🚨 PINCH HIT ALERT — {team}",
            "description": (
                f"**Twitter source — pre-event pinch hit**\n\n"
                f"📋 **{summary}**\n\n"
                f"🌐 **@{handle}:**\n_{text[:200]}_\n🔗 [View Tweet]({url})\n\n"
                f"💰 **BET THE UNDER NOW**"
            ),
            "color": 0xF1C40F,
            "footer": {"text": footer}}]
    }
    def _send():
        msg_id, post_dur = _post_discord_now(embed_payload, return_msg_id=True, return_timing=True)
        _log_timing("general", handle, latency, internal, post_dur)
        _edit_footer_with_post_time(msg_id, embed_payload, post_dur)
        record_fire(pinch_hitter, handle, "general")
        if msg_id:
            if event_key:
                _record_event(event_key, msg_id)
            post_followup_reply(msg_id, handle, False, team, pinch_hitter, replaced)
    threading.Thread(target=_send, daemon=True).start()
    print(f"  🟡 General: {team} — {summary} | latency {_latency_str(latency)}")

def post_corroboration_reply(message_id, handle, text, url, pinch_hitter, replaced):
    """v17.1: A second (or later) tweet reporting the SAME event as an alert
    already fired within CORROBORATION_WINDOW_SEC. Reply on the original
    thread instead of firing a new @everyone alert."""
    content = (
        f"✅ **Corroborated** by @{handle} — {pinch_hitter} for {replaced}\n"
        f"_{text[:200]}_\n🔗 [View Tweet]({url})"
    )
    threading.Thread(target=_post_reply, args=(message_id, content), daemon=True).start()
    print(f"  🔁 Corroboration only: @{handle} confirms {pinch_hitter} for {replaced}")

# ── CORE: FIRE ALERT (non-reporters only) ─────────────────────────────────────
def _fire_alert(handle, text, tid, pinch_hitter, replaced, latency=None, recv_t=None):
    ph_team  = lookup_player_team(pinch_hitter)
    out_team = lookup_player_team(replaced)
    if not ph_team or not out_team:
        missing = []
        if not ph_team:  missing.append(pinch_hitter)
        if not out_team: missing.append(replaced)
        print(f"  🚫 @{handle}: roster miss for {missing}")
        post_drop_log(handle, f"roster miss: {', '.join(missing)}", text)
        return

    url = f"https://twitter.com/{handle}/status/{tid}"

    # v17.1: if this exact event was already alerted recently, corroborate
    # via reply instead of firing a duplicate full alert.
    event_key = _event_key(pinch_hitter, replaced)
    recent    = _get_recent_event(event_key)
    if recent:
        if tid in posted_alert_keys:
            return
        posted_alert_keys.add(tid)
        post_corroboration_reply(recent["message_id"], handle, text, url, pinch_hitter, replaced)
        return

    if is_player_on_cooldown(pinch_hitter):
        print(f"  🔇 '{pinch_hitter}' on cooldown")
        post_drop_log(handle, f"cooldown: {pinch_hitter}", text)
        return

    team = lookup_player_team(pinch_hitter) or lookup_player_team(replaced) \
           or infer_team_from_text(text) or "Unknown Team"

    if tid in posted_alert_keys:
        return
    posted_alert_keys.add(tid)

    print(f"  ✅ VALID: @{handle} (general) team={team} | {pinch_hitter} for {replaced}")
    print(f"     {text[:120]}")

    record_player_alert(pinch_hitter)
    post_general_alert(handle, text, url, team, pinch_hitter, replaced, latency, event_key, recv_t)

# ── CORE: HANDLE SINGLE TWEET ─────────────────────────────────────────────────
def handle_tweet(tid, text, handle, created_at=None, recv_t=None):
    maybe_reset_daily()

    if not is_game_hours():
        return
    if not tid or tid in seen_tweet_ids:
        return
    seen_tweet_ids.add(tid)

    # ── Stage 1: Cheap string pre-filters ────────────────────────────────────
    if not has_core_phrase(text):
        return

    if is_question(text):
        print(f"  🚫 @{handle}: question/speculation — {text[:60]}")
        post_drop_log(handle, "question/speculation", text)
        return

    if result_comes_after_ph(text):
        print(f"  🚫 @{handle}: result after ph (already happened) — {text[:60]}")
        post_drop_log(handle, "already happened", text)
        return

    rejected, phrase = has_reject_phrase(text)
    if rejected:
        print(f"  🚫 @{handle}: '{phrase}' — {text[:60]}")
        post_drop_log(handle, f"reject phrase: {phrase}", text)
        return

    latency = _tweet_latency(created_at)

    # ── REPORTER FAST PATH (v17): fire NOW, Claude enriches async ─────────────
    if handle in REPORTER_HANDLES:
        if tid in posted_alert_keys:
            return
        posted_alert_keys.add(tid)
        reporter = REPORTER_BY_HANDLE.get(handle)
        team = reporter["team"] if reporter else (infer_team_from_text(text) or "Unknown Team")
        url  = f"https://twitter.com/{handle}/status/{tid}"
        print(f"  ⚡ FAST REPORTER FIRE: @{handle} team={team} | {text[:100]}")
        post_reporter_alert_fast(handle, text, url, team, latency, recv_t)
        return

    # ── NON-REPORTER: gate on Claude as before ───────────────────────────────
    if not ANTHROPIC_API_KEY:
        print(f"  🚫 @{handle}: no Claude API key (non-reporter) — {text[:60]}")
        post_drop_log(handle, "no Claude API key", text)
        return

    print(f"  🤖 Sending to Claude: @{handle} — {text[:80]}")

    _tid, _text, _handle, _latency, _recv_t = tid, text, handle, latency, recv_t

    def on_claude_result(is_valid, ph, out, reason):
        if not is_valid:
            print(f"  🚫 @{_handle}: Claude rejected — {reason}")
            post_drop_log(_handle, f"Claude: {reason}", _text)
            return
        if not ph or not out:
            print(f"  🚫 @{_handle}: Claude valid but couldn't extract names")
            post_drop_log(_handle, "Claude: valid but names unclear", _text)
            return
        print(f"  🤖 Claude extracted: ph='{ph}' out='{out}' — {reason}")
        _fire_alert(_handle, _text, _tid, ph, out, _latency, _recv_t)

    claude_classify(text, on_claude_result)

# ── TWITTER STREAM ────────────────────────────────────────────────────────────
def get_stream_rules():
    try:
        r = requests.get(
            "https://api.twitter.com/2/tweets/search/stream/rules",
            headers=TWITTER_HEADERS, timeout=10
        )
        r.raise_for_status()
        return r.json().get("data", [])
    except Exception as e:
        print(f"[stream rules error] {e}")
        return []

def delete_stream_rules(rule_ids):
    if not rule_ids:
        return
    try:
        requests.post(
            "https://api.twitter.com/2/tweets/search/stream/rules",
            headers=TWITTER_HEADERS,
            json={"delete": {"ids": rule_ids}},
            timeout=10
        )
    except Exception as e:
        print(f"[stream rules delete error] {e}")

def add_stream_rules():
    try:
        r = requests.post(
            "https://api.twitter.com/2/tweets/search/stream/rules",
            headers=TWITTER_HEADERS,
            json={"add": STREAM_RULES},
            timeout=10
        )
        r.raise_for_status()
        data = r.json()
        print(f"[stream] Added {len(data.get('data', []))} filter rules")
        if data.get("errors"):
            print(f"[stream rule errors] {data['errors']}")
    except Exception as e:
        print(f"[stream rules add error] {e}")

def setup_stream_rules():
    existing = get_stream_rules()
    if existing:
        delete_stream_rules([r["id"] for r in existing])
        time.sleep(1)
    add_stream_rules()

def connect_stream():
    url    = "https://api.twitter.com/2/tweets/search/stream"
    params = {
        "tweet.fields": "created_at,author_id,text",
        "expansions":   "author_id",
        "user.fields":  "username",
    }
    print("[stream] Connecting...")
    # v17.9: STREAM_HEADERS (Accept-Encoding: identity) — no gzip buffering on
    # the long-lived NDJSON stream, so each tweet line is readable the instant
    # it arrives rather than waiting for a compression block to flush.
    r = requests.get(url, headers=STREAM_HEADERS, params=params,
                     stream=True, timeout=30)
    if r.status_code == 429:
        print("[stream] 429 rate limit — waiting 5 min...")
        time.sleep(300)
        return
    if r.status_code != 200:
        print(f"[stream error] HTTP {r.status_code}: {r.text[:200]}")
        return
    print("[stream] Connected! Listening for tweets...\n")
    for line in r.iter_lines():
        if line:
            yield line

def run_stream():
    reconnect_wait = 5
    while True:
        try:
            for raw_line in connect_stream():
                reconnect_wait = 5
                maybe_reset_daily()
                # v17.7: capture the instant the tweet arrives off the stream,
                # BEFORE any processing. Everything after this point is our
                # internal time; everything before it is Twitter's delivery lag.
                recv_t = time.monotonic()
                try:
                    data = json.loads(raw_line)
                except (json.JSONDecodeError, UnicodeDecodeError):
                    continue
                tweet_data = data.get("data", {})
                users      = {u["id"]: u["username"].lower()
                              for u in data.get("includes", {}).get("users", [])}
                handle_tweet(
                    tweet_data.get("id", ""),
                    tweet_data.get("text", ""),
                    users.get(tweet_data.get("author_id", ""), "unknown"),
                    tweet_data.get("created_at"),
                    recv_t,
                )
        except requests.exceptions.ChunkedEncodingError:
            print(f"[stream] Dropped — reconnecting in {reconnect_wait}s...")
        except requests.exceptions.ConnectionError:
            print(f"[stream] Connection error — reconnecting in {reconnect_wait}s...")
        except Exception as e:
            print(f"[stream] Error: {e} — reconnecting in {reconnect_wait}s...")
        time.sleep(reconnect_wait)
        reconnect_wait = min(reconnect_wait * 2, 300)

# ── END-OF-DAY RECALL AUDIT (v17.3) ───────────────────────────────────────────
# Uses MLB's official play-by-play as ground truth for which pinch hits actually
# happened, checks each against the day's fire-log, and for every MISS does a
# medium-breadth Twitter *search* (not stream) to surface the tweets your live
# stream rules never delivered — then asks Claude whether any of them was a
# pre-event alert you could have caught. Output goes to a dedicated audit
# webhook/channel. Runs once per day after games are final.
#
# Cost profile: Twitter *search* (recent search endpoint) is a separate quota
# from your filtered stream. This only searches MISSES (typically a handful per
# slate), bounds each search to AUDIT_SEARCH_MAX tweets and a +/-AUDIT_WINDOW_MIN
# window, and only sends Claude the misses. Designed to stay well within tier
# quotas and add negligible Anthropic spend.

# ── PHRASE ATTRIBUTION ROLLUP (v17.3) ─────────────────────────────────────────
# Persistent tally (survives restarts) of how often each UNPROMOTED phrase would
# have caught a real, Claude-confirmed catchable miss. This is the evidence you
# use to decide whether to promote a medium/high-risk phrase to a live stream
# rule — nothing here auto-promotes.
_rollup_lock = threading.Lock()

def _load_rollup():
    try:
        with open(PHRASE_ROLLUP_PATH) as f:
            return json.load(f)
    except Exception:
        return {}

def _save_rollup(data):
    try:
        with open(PHRASE_ROLLUP_PATH, "w") as f:
            json.dump(data, f, indent=2, default=str)
    except Exception as e:
        print(f"[rollup save error] {e}")

def record_phrase_attribution(phrases, date_str):
    """phrases: list of unpromoted phrases that appeared in a confirmed catchable
    miss. Increments each phrase's all-time and last-7-day counters."""
    if not phrases:
        return
    with _rollup_lock:
        data = _load_rollup()
        for p in phrases:
            entry = data.get(p, {"total": 0, "risk": UNPROMOTED_PHRASES.get(p, "?"), "recent": []})
            entry["total"] += 1
            entry["recent"].append(date_str)
            # keep only last 14 dates of history per phrase
            entry["recent"] = entry["recent"][-14:]
            data[p] = entry
        _save_rollup(data)

def format_rollup_summary():
    data = _load_rollup()
    if not data:
        return "📈 **Phrase-promotion tracker:** no attributable catchable misses recorded yet."
    cutoff = (datetime.now(timezone.utc) - timedelta(days=7)).strftime("%Y-%m-%d")
    lines = ["📈 **Phrase-promotion tracker** — unpromoted phrases that would have caught a real miss:"]
    # sort by last-7-day count desc, then total
    def recent_count(e):
        return sum(1 for d in e.get("recent", []) if d >= cutoff)
    for phrase, e in sorted(data.items(), key=lambda kv: (recent_count(kv[1]), kv[1]["total"]), reverse=True):
        r7 = recent_count(e)
        lines.append(
            f"  • `{phrase}` [{e.get('risk','?')} risk] — "
            f"{r7} in last 7d, {e['total']} all-time"
        )
    lines.append("\n_Promote a phrase to a live stream rule when its count justifies the noise/quota tradeoff. Say the word and it gets added._")
    return "\n".join(lines)

def _audit_post(content):
    hook = DISCORD_AUDIT_WEBHOOK or DISCORD_LOG_WEBHOOK
    if not hook:
        print("[audit] No audit/log webhook set — printing instead:\n" + content)
        return
    try:
        # Discord 2000-char limit; chunk if needed.
        for i in range(0, len(content), 1900):
            requests.post(hook, json={"content": content[i:i+1900]}, timeout=10)
            time.sleep(0.3)
    except Exception as e:
        print(f"[audit post error] {e}")

def fetch_official_pinch_hits(date_str):
    """Return list of dicts: {player, team, game_pk, when_utc (datetime|None)}
    for every official pinch-hit substitution in MLB play-by-play on date_str."""
    results = []
    try:
        sched = requests.get(
            "https://statsapi.mlb.com/api/v1/schedule",
            params={"sportId": 1, "date": date_str, "gameType": "R"},
            timeout=10,
        )
        sched.raise_for_status()
        game_pks = [g["gamePk"]
                    for d in sched.json().get("dates", [])
                    for g in d.get("games", [])
                    if g.get("status", {}).get("abstractGameState") == "Final"]
    except Exception as e:
        print(f"[audit] schedule error: {e}")
        return results

    for game_pk in game_pks:
        try:
            r = requests.get(
                f"https://statsapi.mlb.com/api/v1.1/game/{game_pk}/feed/live",
                timeout=15,
            )
            r.raise_for_status()
            live = r.json()
            all_plays = live.get("liveData", {}).get("plays", {}).get("allPlays", [])
            for play in all_plays:
                for ev in play.get("playEvents", []):
                    # Offensive substitutions show up as player-change events.
                    details = ev.get("details", {})
                    desc = (details.get("description") or "").lower()
                    is_sub = ev.get("isSubstitution", False) or details.get("event") == "Offensive Substitution"
                    if is_sub and "pinch-hitter" in desc:
                        # description e.g. "Offensive Substitution: Pinch-hitter Luis Lara replaces Sal Frelick."
                        m = re.search(r"pinch-hitter\s+(.+?)\s+replaces\s+(.+?)[.\n]", desc, re.IGNORECASE)
                        player = m.group(1).strip() if m else None
                        when = None
                        tstr = ev.get("startTime") or play.get("about", {}).get("startTime")
                        if tstr:
                            try:
                                when = datetime.fromisoformat(tstr.replace("Z", "+00:00"))
                            except Exception:
                                when = None
                        if player:
                            results.append({
                                "player":  player.title(),
                                "game_pk": game_pk,
                                "when_utc": when,
                            })
        except Exception as e:
            print(f"[audit] game {game_pk} error: {e}")
    return results

def search_recent_tweets(player_name, when_utc):
    """Medium-breadth recent search: player name + pinch/hit/PH terms, bounded
    to a small window and low max_results to protect quota. Returns list of
    {text, handle, created_at}."""
    query = f'"{player_name}" (pinch OR "pinch hit" OR "pinch-hit" OR PH OR "hit for") -is:retweet lang:en'
    params = {
        "query":        query,
        "max_results":  max(10, min(AUDIT_SEARCH_MAX, 100)),  # API min is 10
        "tweet.fields": "created_at,author_id,text",
        "expansions":   "author_id",
        "user.fields":  "username",
    }
    if when_utc:
        start = when_utc.astimezone(timezone.utc) - timedelta(minutes=AUDIT_WINDOW_MIN)
        end   = when_utc.astimezone(timezone.utc) + timedelta(minutes=AUDIT_WINDOW_MIN)
        # recent search only covers last 7 days; guard anyway
        params["start_time"] = start.strftime("%Y-%m-%dT%H:%M:%SZ")
        params["end_time"]   = end.strftime("%Y-%m-%dT%H:%M:%SZ")
    try:
        r = requests.get(
            "https://api.twitter.com/2/tweets/search/recent",
            headers=TWITTER_HEADERS, params=params, timeout=15,
        )
        if r.status_code == 429:
            print("[audit] search 429 — quota/rate limited, skipping remaining searches")
            return "RATE_LIMITED"
        r.raise_for_status()
        body = r.json()
        users = {u["id"]: u["username"]
                 for u in body.get("includes", {}).get("users", [])}
        out = []
        for t in body.get("data", []):
            out.append({
                "text":       t.get("text", ""),
                "handle":     users.get(t.get("author_id", ""), "unknown"),
                "created_at": t.get("created_at"),
            })
        # Cap to AUDIT_SEARCH_MAX even if API floor returned more
        return out[:AUDIT_SEARCH_MAX]
    except Exception as e:
        print(f"[audit] search error for {player_name}: {e}")
        return []

def claude_audit_why_missed(player_name, tweets, callback):
    """Ask Claude whether any tweet was a catchable pre-event alert. Returns via
    callback(verdict_text, catchable_bool, catchable_indices). Cheap (one Haiku
    call per missed player)."""
    if not ANTHROPIC_API_KEY or not tweets:
        callback("No tweets found." if not tweets else "No API key.", False, [])
        return
    joined = "\n".join(f'[{i}] @{t["handle"]} ({t.get("created_at","?")}): {t["text"][:220]}'
                       for i, t in enumerate(tweets[:AUDIT_SEARCH_MAX]))
    system = (
        "You audit an MLB pinch-hit alert bot's recall. The bot alerts on tweets "
        "that announce a pinch hit BEFORE the at-bat happens. You're given the "
        "official pinch hitter's name and tweets found afterward mentioning them, "
        "each with an index [N]. Decide whether at least one tweet was a CATCHABLE "
        "pre-event announcement (a real, specific, pre-at-bat pinch-hit statement a "
        "well-tuned bot could have alerted on). "
        'Respond with ONLY a JSON object, no markdown: '
        '{"catchable": true/false, "catchable_indices": [list of tweet indices '
        'that were catchable pre-event], "explanation": "2-3 sentence summary — '
        'if catchable, quote the key phrasing; if not, say why (all post-event, '
        'none specific, etc.)"}'
    )
    try:
        r = requests.post(
            "https://api.anthropic.com/v1/messages",
            headers={"x-api-key": ANTHROPIC_API_KEY,
                     "anthropic-version": "2023-06-01",
                     "content-type": "application/json"},
            json={"model": "claude-haiku-4-5-20251001",
                  "max_tokens": 250,
                  "system": system,
                  "messages": [{"role": "user",
                                "content": f"Pinch hitter: {player_name}\nTweets found:\n{joined}"}]},
            timeout=15,
        )
        r.raise_for_status()
        raw = r.json()["content"][0]["text"].strip()
        raw = re.sub(r"^```json\s*", "", raw)
        raw = re.sub(r"```\s*$", "", raw)
        data = json.loads(raw)
        callback(
            data.get("explanation", ""),
            bool(data.get("catchable")),
            [i for i in data.get("catchable_indices", []) if isinstance(i, int)],
        )
    except Exception as e:
        callback(f"(audit Claude error: {e})", False, [])

def run_daily_audit(date_str):
    """Full audit pass for date_str (YYYY-MM-DD). Blocking; call from a thread."""
    print(f"[audit] Starting recall audit for {date_str}...")
    official = fetch_official_pinch_hits(date_str)
    if not official:
        _audit_post(f"📋 **Recall audit {date_str}** — no final games / no official pinch hits found.")
        return

    caught, missed = [], []
    for ph in official:
        hit = was_fired_today(ph["player"], date_str)
        (caught if hit else missed).append((ph, hit))

    header = (
        f"📋 **Recall Audit — {date_str}**\n"
        f"Official pinch hits: **{len(official)}** · "
        f"Alerted: **{len(caught)}** · Missed: **{len(missed)}**\n"
    )
    if caught:
        header += "\n✅ **Caught:** " + ", ".join(
            f"{ph['player']} ({info['path']})" for ph, info in caught) + "\n"
    _audit_post(header)

    if not missed:
        _audit_post("🎯 No misses — full recall on today's slate.")
        _audit_post(format_rollup_summary())
        print("[audit] Done — full recall.")
        return

    _audit_post(f"❌ **{len(missed)} missed — investigating each below:**")
    for ph, _ in missed:
        player = ph["player"]
        tweets = search_recent_tweets(player, ph.get("when_utc"))
        if tweets == "RATE_LIMITED":
            _audit_post(f"⏸️ **{player}** — Twitter search quota hit; stopping further searches for today.")
            break
        if not tweets:
            _audit_post(f"❌ **{player}** — no tweets found in the window "
                        f"(likely genuinely untweeted, or outside search's 7-day range).")
            continue

        block = [f"❌ **{player}** — {len(tweets)} tweet(s) found the bot didn't act on:"]
        for t in tweets[:5]:
            block.append(f"  • @{t['handle']}: _{t['text'][:160]}_")
        _audit_post("\n".join(block))

        done = threading.Event()
        holder = {}
        def cb(verdict, catchable, indices, _h=holder, _d=done):
            _h["v"] = verdict
            _h["catchable"] = catchable
            _h["indices"] = indices
            _d.set()
        claude_audit_why_missed(player, tweets, cb)
        done.wait(timeout=20)

        verdict = holder.get("v", "(no response)")
        catchable = holder.get("catchable", False)
        flag = "🟠 CATCHABLE" if catchable else "⚪ not catchable"
        _audit_post(f"🤖 **Why missed — {player}:** [{flag}] {verdict}")

        # Attribute: which unpromoted phrase(s) would have caught the catchable tweets?
        if catchable:
            hit_phrases = set()
            for i in holder.get("indices", []):
                if 0 <= i < len(tweets):
                    for p in attributable_phrases(tweets[i]["text"]):
                        hit_phrases.add(p)
            if hit_phrases:
                record_phrase_attribution(list(hit_phrases), date_str)
                _audit_post(
                    "   ↳ would have been caught by unpromoted phrase(s): "
                    + ", ".join(f"`{p}`" for p in sorted(hit_phrases))
                )
            else:
                _audit_post(
                    "   ↳ catchable, but no *unpromoted* phrase matched — the "
                    "phrasing may need a brand-new rule (not just promoting an "
                    "existing second-tier phrase). Worth a look."
                )
        time.sleep(1)  # gentle pacing between players

    # End-of-audit: post the running promotion tracker
    _audit_post(format_rollup_summary())
    print("[audit] Done.")

def start_audit_scheduler():
    """Background thread that fires run_daily_audit once per day at AUDIT_HOUR_ET,
    auditing the day that just ended."""
    def loop():
        global last_audit_date
        while True:
            try:
                now = datetime.now(ET_TZ)
                # audit the calendar day that just finished, once, at/after AUDIT_HOUR_ET
                if now.hour == AUDIT_HOUR_ET:
                    audit_for = (now.date().toordinal() - 1)
                    audit_date = datetime.fromordinal(audit_for).strftime("%Y-%m-%d")
                    if last_audit_date != audit_date:
                        last_audit_date = audit_date
                        run_daily_audit(audit_date)
            except Exception as e:
                print(f"[audit] scheduler error: {e}")
            time.sleep(600)  # check every 10 min
    threading.Thread(target=loop, daemon=True).start()
    print(f"[audit] Scheduler started — runs daily ~{AUDIT_HOUR_ET}:00 ET\n")

# ── MAIN ──────────────────────────────────────────────────────────────────────
def run():
    print("⚾ MLB Pinch Hit Bot v17.9")
    print("   ⚡ Reporters fire IMMEDIATELY (Claude async only for names/retract)")
    print("   🗜️ Stream requests identity encoding (no gzip buffering)")
    print("   ♻️ Discord posts reuse one keep-alive TLS session")
    print("   📊 Rolling min/med/max of the twitter-delivery segment per alert")
    print("   🔁 Duplicate reports of the same event now reply instead of re-alerting")
    print("   📏 Every alert logs pipeline latency (now - tweet.created_at)")
    print("   📋 End-of-day recall audit vs official MLB pinch hits (misses only)")
    print(f"   {len(REPORTERS)} reporters tracked | game hours 12pm-1am ET\n")

    if not TWITTER_BEARER_TOKEN:
        print("[error] TWITTER_BEARER_TOKEN not set!")
        return
    if not DISCORD_WEBHOOK_URL:
        print("[error] PINCH_HIT_WEBHOOK_URL not set!")
        return
    if not DISCORD_CHANNEL_ID:
        print("[warning] DISCORD_CHANNEL_ID not set — follow-up replies skipped")
    if not ANTHROPIC_API_KEY:
        print("[warning] ANTHROPIC_API_KEY not set — reporters still fire; "
              "non-reporter alerts disabled")
    if not DISCORD_LOG_WEBHOOK:
        print("[warning] DISCORD_LOG_WEBHOOK_URL not set — drop logging skipped")
    if not DISCORD_AUDIT_WEBHOOK:
        print("[warning] DISCORD_AUDIT_WEBHOOK_URL not set — recall audit will fall "
              "back to the log webhook (or console if neither is set)")

    start_lineup_refresh_thread()
    prefetch_full_rosters()
    start_audit_scheduler()
    time.sleep(5)

    setup_stream_rules()
    run_stream()

if __name__ == "__main__":
    import sys
    if len(sys.argv) >= 3 and sys.argv[1] == "--audit":
        # Manual one-off audit of a given date, no stream. e.g.:
        #   python3 pinch_hit_bot_v17_9.py --audit 2026-07-17
        # NOTE: fire-log is in-memory, so a manual run on a past date will show
        # everything as "missed" (nothing was recorded as caught in this fresh
        # process). Use it to sanity-check the official-PH pull + search + Claude
        # steps, not the caught/missed split.
        run_daily_audit(sys.argv[2])
    else:
        run()
